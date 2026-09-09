"""Explicit, no-virtual-driver WebUI ↔ physical N4 test bridge.

This companion is intentionally separate from the Codex Micro sideband
client.  It is useful while the UMDF virtual device is not installed:

* poll the existing WebUI's ``/api/visual/state`` endpoint;
* render that state onto the same physical N4 owned by ``N4NativeAdapter``;
* keep forwarding N4 input reports to ``/api/bridge/report``.

The CLI never opens a USB device unless ``--live`` is supplied explicitly.
``--dry-run``/``--check-only`` perform only HTTP/JSON checks and are safe on a
machine without the WDK, driver, or attached N4.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional, Sequence, TextIO


DEFAULT_WEBUI_URL = "http://127.0.0.1:8787"
WEBUI_SERVER_SCHEMA = "mirabox.codex.micro.webui"
WEBUI_API_VERSION = 2
SAFE_WEBUI_FEATURES = ("visualApi", "transportApi", "eventStream")
LIVE_WEBUI_FEATURES = (
    "bridgeApi",
    "visualApi",
    "transportApi",
    "eventStream",
    "dynamicInputMapping",
    "nativeBridgeStatus",
)


class WebUiBridgeError(RuntimeError):
    """A WebUI endpoint returned an invalid response or could not be reached."""


def _endpoint(base_url: str, route: str) -> str:
    base = str(base_url or "").rstrip("/")
    if not base:
        raise ValueError("WebUI URL cannot be empty")
    return f"{base}/{route.lstrip('/')}"


class WebUiClient:
    """Minimal stdlib HTTP client for the visual/input WebUI endpoints."""

    def __init__(self, base_url: str = DEFAULT_WEBUI_URL, *, timeout: float = 2.0,
                 opener: Optional[Callable[..., Any]] = None) -> None:
        if timeout <= 0:
            raise ValueError("WebUI timeout must be positive")
        self.base_url = str(base_url).rstrip("/")
        self.timeout = float(timeout)
        self.opener = opener or urllib.request.urlopen

    def _get_json(self, route: str) -> dict[str, Any]:
        request = urllib.request.Request(
            _endpoint(self.base_url, route),
            method="GET",
            headers={"accept": "application/json", "user-agent": "mirabox-n4-webui-bridge/1.0"},
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
                status = int(getattr(response, "status", 200))
        except urllib.error.HTTPError as exc:
            raise WebUiBridgeError(f"WebUI GET {route} returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise WebUiBridgeError(f"WebUI GET {route} failed: {exc}") from exc
        if status < 200 or status >= 300:
            raise WebUiBridgeError(f"WebUI GET {route} returned HTTP {status}")
        try:
            parsed = json.loads(body) if body else {}
        except json.JSONDecodeError as exc:
            raise WebUiBridgeError(f"WebUI GET {route} returned invalid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise WebUiBridgeError(f"WebUI GET {route} must return a JSON object")
        if parsed.get("error"):
            raise WebUiBridgeError(f"WebUI GET {route}: {parsed['error']}")
        return parsed

    def _post_json(self, route: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """POST one JSON object to the local WebUI and decode its response."""

        if not isinstance(payload, Mapping):
            raise TypeError("WebUI POST payload must be an object")
        body = json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            _endpoint(self.base_url, route),
            data=body,
            method="POST",
            headers={
                "accept": "application/json",
                "content-type": "application/json",
                "user-agent": "mirabox-n4-webui-bridge/1.0",
            },
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                response_body = response.read().decode("utf-8", errors="replace")
                status = int(getattr(response, "status", 200))
        except urllib.error.HTTPError as exc:
            raise WebUiBridgeError(f"WebUI POST {route} returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise WebUiBridgeError(f"WebUI POST {route} failed: {exc}") from exc
        if status < 200 or status >= 300:
            raise WebUiBridgeError(f"WebUI POST {route} returned HTTP {status}")
        try:
            parsed = json.loads(response_body) if response_body else {}
        except json.JSONDecodeError as exc:
            raise WebUiBridgeError(f"WebUI POST {route} returned invalid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise WebUiBridgeError(f"WebUI POST {route} must return a JSON object")
        if parsed.get("error"):
            raise WebUiBridgeError(f"WebUI POST {route}: {parsed['error']}")
        return parsed

    def visual_state(self) -> dict[str, Any]:
        value = self._get_json("/api/visual/state")
        lighting = value.get("lighting")
        if not isinstance(lighting, Mapping):
            raise WebUiBridgeError("/api/visual/state has no lighting object")
        return value

    def state(self) -> dict[str, Any]:
        """Fetch the server capability envelope without touching USB."""

        return self._get_json("/api/state")

    def require_contract(self, required_features: Sequence[str]) -> dict[str, Any]:
        """Validate the additive WebUI instance contract before live work.

        The browser can serve a newer HTML file from disk while an older Node
        process is still listening on the same port.  Requiring the server
        marker and advertised routes here prevents the native bridge from
        opening the physical N4 against that stale process.
        """

        value = self.state()
        server = value.get("server")
        if not isinstance(server, Mapping):
            raise WebUiBridgeError(
                "WebUI is an old/incomplete instance: /api/state has no server contract; "
                "restart the Node WebUI on the selected port"
            )
        if server.get("schema") != WEBUI_SERVER_SCHEMA or _number(server.get("apiVersion")) < WEBUI_API_VERSION:
            raise WebUiBridgeError(
                "WebUI is an old/incomplete instance: incompatible server contract "
                f"(expected {WEBUI_SERVER_SCHEMA} API v{WEBUI_API_VERSION})"
            )
        features = value.get("features")
        if not isinstance(features, Mapping):
            raise WebUiBridgeError("WebUI server contract has no features map; restart the Node WebUI")
        missing = []
        for feature in required_features:
            advertised = features.get(feature)
            # Current server advertises booleans; accepting positive integer
            # versions keeps this check forward-compatible with versioned
            # capability maps.
            if isinstance(advertised, bool):
                level = 1 if advertised else 0
            else:
                level = _number(advertised)
            if level < 1:
                missing.append(feature)
        if missing:
            raise WebUiBridgeError(
                "WebUI server contract is missing required features: "
                + ", ".join(missing)
                + "; restart the current instance or choose a newer port"
            )
        return value

    def config(self) -> dict[str, Any]:
        return self._get_json("/api/config")

    def native_state(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Report one native-bridge lifecycle/heartbeat snapshot.

        The endpoint is intentionally local and best-effort at the caller:
        failure to report status must never stop the physical N4 reader.
        """

        return self._post_json("/api/native/state", payload)


def utc_now() -> str:
    """Return a compact UTC timestamp suitable for status payloads."""

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _number(value: Any) -> float:
    """Return a finite numeric contract value, or zero for malformed input."""

    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if number == number and number not in (float("inf"), float("-inf")) else 0.0


class NativeStatusReporter:
    """Best-effort native bridge lifecycle/heartbeat reporter.

    The reporter uses a daemon thread and only performs HTTP requests to the
    configured WebUI URL.  Any connection/HTTP failure is recorded and
    optionally printed, but is deliberately swallowed so a status endpoint
    outage cannot interrupt N4 input or visual output.
    """

    def __init__(
        self,
        client: WebUiClient,
        snapshot_factory: Callable[[], Mapping[str, Any]],
        *,
        interval_ms: int = 1000,
        status_stream: Optional[TextIO] = sys.stderr,
    ) -> None:
        if interval_ms < 100:
            raise ValueError("native status interval must be at least 100 ms")
        self.client = client
        self.snapshot_factory = snapshot_factory
        self.interval_ms = int(interval_ms)
        self.status_stream = status_stream
        self.stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._post_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self.posts = 0
        self.failures = 0
        self.last_error: Optional[str] = None
        self.last_post_at: Optional[str] = None
        self._last_error_notice = 0.0

    def _status(self, message: str) -> None:
        if self.status_stream is not None:
            print(message, file=self.status_stream, flush=True)

    def publish(self, payload: Optional[Mapping[str, Any]] = None) -> Optional[dict[str, Any]]:
        """Send one snapshot; return the decoded WebUI response or ``None``."""

        try:
            value = payload if payload is not None else self.snapshot_factory()
            if not isinstance(value, Mapping):
                raise TypeError("native status snapshot must be an object")
            # Serialize posts so an explicit lifecycle update cannot race a
            # heartbeat and arrive out of order at the WebUI cache.
            with self._post_lock:
                response = self.client.native_state(value)
            with self._stats_lock:
                self.posts += 1
                self.last_error = None
                self.last_post_at = utc_now()
            return response
        except Exception as exc:  # best-effort by design
            with self._stats_lock:
                self.failures += 1
                self.last_error = str(exc)
            now = time.monotonic()
            # Avoid writing one line per second when the WebUI is stopped.
            if now - self._last_error_notice >= 10.0:
                self._last_error_notice = now
                self._status(f"[webui] native status deferred: {exc}")
            return None

    def start(self) -> None:
        """Publish immediately and begin periodic heartbeats."""

        if self._thread and self._thread.is_alive():
            return
        self.stop_event.clear()
        self.publish()
        self._thread = threading.Thread(
            target=self._loop,
            name="n4-webui-native-status",
            daemon=True,
        )
        self._thread.start()

    def _loop(self) -> None:
        interval = self.interval_ms / 1000.0
        while not self.stop_event.wait(interval):
            self.publish()

    def stop(self, payload: Optional[Mapping[str, Any]] = None) -> None:
        """Stop heartbeats and optionally publish one final lifecycle state."""

        self.stop_event.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        self._thread = None
        if payload is not None:
            self.publish(payload)

    def snapshot(self) -> dict[str, Any]:
        """Return reporter-local counters for inclusion in diagnostics."""

        with self._stats_lock:
            return {
                "intervalMs": self.interval_ms,
                "posts": self.posts,
                "failures": self.failures,
                "lastError": self.last_error,
                "lastPostAt": self.last_post_at,
                "running": bool(self._thread and self._thread.is_alive()),
            }


def _lighting_digest(payload: Mapping[str, Any]) -> str:
    lighting = payload.get("lighting")
    if lighting is None and isinstance(payload.get("model"), Mapping):
        lighting = payload["model"].get("lighting")
    model = payload.get("model")
    targets = None
    if isinstance(model, Mapping) and isinstance(model.get("buttons"), list):
        targets = [
            {
                "targetKey": button.get("targetKey"),
                "enabled": button.get("enabled", True),
            }
            if isinstance(button, Mapping) else None
            for button in model["buttons"]
        ]
    packed = json.dumps(
        {"lighting": lighting, "targets": targets, "theme": model.get('theme') if isinstance(model, Mapping) else None,
         "strip":{key:model.get(key) for key in ('stripMode','stripBrightness','knobs','screenTest')} if isinstance(model,Mapping) else None},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(packed.encode("utf-8")).hexdigest()


class VisualStatePoller:
    """Poll WebUI visual state and feed only changed snapshots to a renderer."""

    def __init__(self, client: WebUiClient, renderer: Any, *, interval_ms: int = 500,
                 status_stream: Optional[TextIO] = sys.stderr,
                 on_state: Optional[Callable[[Mapping[str, Any]], None]] = None) -> None:
        if interval_ms < 50:
            raise ValueError("visual poll interval must be at least 50 ms")
        self.client = client
        self.renderer = renderer
        self.interval_ms = int(interval_ms)
        self.status_stream = status_stream
        self.on_state = on_state
        self.stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._digest: Optional[str] = None
        self.fetches = 0
        self.updates = 0
        self.errors = 0
        self.last_error: Optional[str] = None

    def _status(self, message: str) -> None:
        if self.status_stream is not None:
            print(message, file=self.status_stream, flush=True)

    def fetch_once(self, *, force: bool = False) -> dict[str, Any]:
        payload = self.client.visual_state()
        self.fetches += 1
        digest = _lighting_digest(payload)
        if force or digest != self._digest:
            self.renderer.apply_snapshot(payload)
            self._digest = digest
            self.updates += 1
            if self.on_state:
                self.on_state(payload)
        self.last_error = None
        return payload

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="n4-webui-visual-poller", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        interval = self.interval_ms / 1000.0
        while not self.stop_event.is_set():
            try:
                self.fetch_once()
            except Exception as exc:
                self.errors += 1
                self.last_error = str(exc)
                self._status(f"[webui] visual poll deferred: {exc}")
            self.stop_event.wait(interval)

    def stop(self) -> None:
        self.stop_event.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        self._thread = None


def _json_line(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="n4-webui-bridge.py",
        description=(
            "Explicit test bridge: poll WebUI /api/visual/state to a physical "
            "N4 and POST N4 input to /api/bridge/report. No virtual driver."
        ),
        epilog=(
            "The process never opens USB unless --live is supplied. "
            "--dry-run and --check-only are hardware-free."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true",
                      help="explicitly open one physical N4 and run the bridge")
    mode.add_argument("--dry-run", action="store_true",
                      help="fetch/validate WebUI state once; never open N4")
    mode.add_argument("--check-only", action="store_true",
                      help="check /api/config and /api/visual/state; never open N4")
    parser.add_argument("--webui-url", default=DEFAULT_WEBUI_URL,
                        help=f"WebUI base URL (default: {DEFAULT_WEBUI_URL})")
    parser.add_argument("--timeout", type=float, default=2.0,
                        help="HTTP timeout in seconds (default: 2)")
    parser.add_argument("--poll-ms", type=int, default=200,
                        help="visual state polling interval (default: 200 ms)")
    parser.add_argument("--once", action="store_true",
                        help="in dry/check mode, fetch one snapshot (default behavior)")
    parser.add_argument("--state-json", metavar="PATH",
                        help="offline snapshot file for --dry-run instead of HTTP")
    parser.add_argument("--no-visuals", action="store_true",
                        help="in live mode, forward N4 input only")
    parser.add_argument("--visual-refresh-ms", type=int, default=250,
                        help="quiet interval after N4 JPEG upload batch (default: 250 ms)")
    parser.add_argument("--visual-target", choices=("both", "screen", "keys"), default="both",
                        help="physical N4 visual destination (default: both)")
    parser.add_argument("--status-interval-ms", type=int, default=1000,
                        help="native WebUI status heartbeat interval (default: 1000 ms)")
    parser.add_argument("--no-status", action="store_true",
                        help="do not POST native lifecycle/heartbeat status to WebUI")
    parser.add_argument("--sdk-path", help="StreamDock Python SDK src directory")
    parser.add_argument(
        "--hidapi-input", "--hidapi",
        dest="hidapi_input",
        action="store_true",
        help=(
            "explicit input-only direct-hidapi fallback; requires --no-visuals "
            "and leaves the default StreamDock SDK path unchanged"
        ),
    )
    parser.add_argument(
        "--hidapi-path", metavar="PATH",
        help="exact verified N4 hidapi path (optional; use --hidapi-input)",
    )
    parser.add_argument(
        "--hidapi-read-timeout-ms", type=int, default=100, metavar="MS",
        help="direct hidapi read timeout (default: 100 ms)",
    )
    parser.add_argument("--n4-path", help="exact N4 SDK device path")
    parser.add_argument("--n4-index", type=int, default=0,
                        help="matching N4 index (default: 0)")
    parser.add_argument("--input-profile", choices=("cpp", "python"), default="cpp",
                        help="N4 input-code profile (default: cpp)")
    parser.add_argument("--no-init", action="store_true", help="skip N4 SDK init commands")
    parser.add_argument("--duration", type=float, help="stop live mode after this many seconds")
    parser.add_argument("--jsonl", action="store_true", help="emit raw N4 JSONL records to stdout")
    parser.add_argument("--quiet", action="store_true", help="suppress status messages")
    return parser


def _load_snapshot_file(path: str) -> dict[str, Any]:
    try:
        value = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WebUiBridgeError(f"unable to read snapshot {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise WebUiBridgeError("snapshot JSON must be an object")
    if "lighting" not in value and isinstance(value.get("visual"), Mapping):
        value = dict(value["visual"])
    if not isinstance(value.get("lighting"), Mapping):
        raise WebUiBridgeError("snapshot JSON has no lighting object")
    return value


def _run_safe(args: argparse.Namespace) -> int:
    client = WebUiClient(args.webui_url, timeout=args.timeout)
    if args.state_json:
        snapshot = _load_snapshot_file(args.state_json)
        contract = None
    else:
        contract = client.require_contract(SAFE_WEBUI_FEATURES)
        snapshot = client.visual_state()
    result: dict[str, Any] = {
        "mode": "dry-run" if args.dry_run else "check-only",
        "webuiUrl": args.webui_url,
        "visual": {
            "hasLighting": isinstance(snapshot.get("lighting"), Mapping),
            "digest": _lighting_digest(snapshot),
            "agents": len(snapshot.get("lighting", {}).get("agents", [])),
        },
        "n4Opened": False,
        "driverTouched": False,
    }
    if contract is not None:
        result["server"] = contract.get("server")
        result["features"] = contract.get("features")
    if args.check_only and not args.state_json:
        config = client.config()
        result["config"] = {"schema": config.get("schema"), "version": config.get("version")}
    _json_line(result)
    return 0


def _run_live(args: argparse.Namespace) -> int:
    if os.name != "nt":
        raise WebUiBridgeError("--live requires Windows; omit --live for hardware-free checks")
    if args.poll_ms < 50 or args.visual_refresh_ms < 20:
        raise WebUiBridgeError("poll/visual refresh intervals are too small")
    status_interval_ms = int(getattr(args, "status_interval_ms", 1000))
    no_status = bool(getattr(args, "no_status", False))
    if status_interval_ms < 100:
        raise WebUiBridgeError("native status interval must be at least 100 ms")
    if args.hidapi_input and not args.no_visuals:
        raise WebUiBridgeError(
            "--hidapi-input is an input-only fallback; add --no-visuals "
            "because direct hidapi mode cannot write N4 screen/key images"
        )
    if args.hidapi_read_timeout_ms < 1 or args.hidapi_read_timeout_ms > 5000:
        raise WebUiBridgeError("--hidapi-read-timeout-ms must be between 1 and 5000")
    # Validate the HTTP backend before importing/opening the physical N4.  A
    # stale Node process can still serve the latest HTML while lacking the
    # native/bridge routes, which would otherwise consume the device and drop
    # every report into a 404 endpoint.
    client = WebUiClient(args.webui_url, timeout=args.timeout)
    client.require_contract(LIVE_WEBUI_FEATURES)
    # All hardware/SDK imports remain behind the explicit --live guard.
    from n4_native_adapter import HttpReporter, JsonlSink, N4NativeAdapter, ReportDispatcher

    endpoint = _endpoint(args.webui_url, "/api/bridge/report")
    jsonl = JsonlSink(sys.stdout) if args.jsonl else None
    input_reporter = HttpReporter(endpoint, timeout=args.timeout)
    dispatcher = ReportDispatcher(
        jsonl=jsonl,
        http=input_reporter,
        input_profile=args.input_profile,
        include_micro_preview=False,
    )
    if args.hidapi_input:
        from n4_hidapi import DirectHidapiN4Reader

        if args.n4_path and args.hidapi_path:
            raise WebUiBridgeError("use only one of --n4-path and --hidapi-path")
        adapter = DirectHidapiN4Reader(
            dispatcher,
            device_path=args.hidapi_path or args.n4_path,
            device_index=args.n4_index,
            read_timeout_ms=args.hidapi_read_timeout_ms,
            status_stream=None if args.quiet else sys.stderr,
        )
        if not args.quiet and not args.no_init:
            print(
                "[n4/hidapi] input-only fallback selected; SDK initialization/output are disabled",
                file=sys.stderr,
                flush=True,
            )
    else:
        adapter = N4NativeAdapter(
            dispatcher,
            sdk_path=args.sdk_path,
            device_path=args.n4_path,
            device_index=args.n4_index,
            initialize=not args.no_init,
            status_stream=None if args.quiet else sys.stderr,
        )
    N4VisualOutput = None
    from n4_local_actions import PhysicalKnobActions
    knob_actions=PhysicalKnobActions(adapter)
    dispatcher.on_record=knob_actions.handle_record
    renderer = None
    if not args.no_visuals:
        from n4_visual_output import N4VisualOutput

        renderer = N4VisualOutput(
            adapter,
            refresh_ms=args.visual_refresh_ms,
            target=args.visual_target,
            status_stream=None if args.quiet else sys.stderr,
        )
    poller = VisualStatePoller(
        client,
        renderer,
        interval_ms=args.poll_ms,
        status_stream=None if args.quiet else sys.stderr,
    ) if renderer is not None else None
    if renderer is not None:
        renderer.start()
    if poller is not None and args.once:
        # A one-shot snapshot is still useful for a fixed lighting test while
        # the N4 input reader continues running. The renderer itself keeps the
        # resulting state; no polling thread is started.
        poller.fetch_once(force=True)
    elif poller is not None:
        poller.start()
    lifecycle = {
        "status": "starting",
        "message": "native N4 bridge is starting",
        "startedAt": utc_now(),
        "stoppedAt": None,
    }

    def native_snapshot() -> dict[str, Any]:
        """Build a JSON-safe status envelope without touching the device."""

        adapter_snapshot = getattr(adapter, "snapshot", None)
        n4 = adapter_snapshot() if callable(adapter_snapshot) else {
            "opened": bool(adapter.is_open),
            "ready": False,
            "path": None,
            "vendorId": int(adapter.vendor_id),
            "productId": int(adapter.product_id),
            "deviceIndex": int(adapter.device_index),
        }
        n4['localActions']=knob_actions.snapshot()
        dispatcher_snapshot = getattr(dispatcher, "snapshot", None)
        counters = dispatcher_snapshot() if callable(dispatcher_snapshot) else {
            "reportsSeen": int(dispatcher.reports_seen),
            "reportsForwarded": int(dispatcher.reports_forwarded),
            "reportsIgnored": int(dispatcher.reports_ignored),
        }
        http_stats = getattr(input_reporter, "snapshot", None)
        if callable(http_stats):
            counters["http"] = http_stats()
        visual = {
            "enabled": renderer is not None,
            "target": args.visual_target if renderer is not None else None,
            "uploads": int(getattr(renderer, "uploads", 0)) if renderer is not None else 0,
            "lastError": getattr(renderer, "last_error", None) if renderer is not None else None,
            "performance": getattr(renderer,"last_frame",{}) if renderer is not None else {},
            "fetches": int(getattr(poller, "fetches", 0)) if poller is not None else 0,
            "updates": int(getattr(poller, "updates", 0)) if poller is not None else 0,
            "errors": int(getattr(poller, "errors", 0)) if poller is not None else 0,
            "lastPollError": getattr(poller, "last_error", None) if poller is not None else None,
        }
        # Keep the public status useful between lifecycle callbacks: the
        # adapter is opening asynchronously, so a heartbeat can distinguish
        # "opening" from a fully running reader without touching USB again.
        status = lifecycle["status"]
        message = lifecycle["message"]
        if status == "starting":
            if n4.get("ready"):
                status, message = "running", "native N4 bridge is running"
            elif n4.get("opened"):
                status, message = "opening", "N4 opened; waiting for SDK initialization"
        return {
            "status": status,
            "message": message,
            "pid": os.getpid(),
            "process": "n4-webui-bridge",
            "webuiUrl": args.webui_url,
            "startedAt": lifecycle["startedAt"],
            "stoppedAt": lifecycle["stoppedAt"],
            "lastSeenAt": utc_now(),
            "n4": n4,
            "counters": counters,
            "visual": visual,
            "driverTouched": False,
        }

    status_reporter = None if no_status else NativeStatusReporter(
        client,
        native_snapshot,
        interval_ms=status_interval_ms,
        status_stream=None if args.quiet else sys.stderr,
    )
    if status_reporter is not None:
        status_reporter.start()
    try:
        result = adapter.run(duration=args.duration)
        lifecycle["status"] = "stopped"
        lifecycle["message"] = "native N4 bridge stopped"
        lifecycle["stoppedAt"] = utc_now()
        return result
    except KeyboardInterrupt:
        lifecycle["status"] = "stopping"
        lifecycle["message"] = "native N4 bridge stopping"
        if status_reporter is not None:
            status_reporter.publish(native_snapshot())
        adapter.stop()
        lifecycle["status"] = "stopped"
        lifecycle["message"] = "native N4 bridge stopped"
        lifecycle["stoppedAt"] = utc_now()
        return 0
    except Exception as exc:
        lifecycle["status"] = "error"
        lifecycle["message"] = str(exc)
        lifecycle["stoppedAt"] = utc_now()
        if status_reporter is not None:
            status_reporter.publish(native_snapshot())
        raise
    finally:
        if poller is not None:
            poller.stop()
        if renderer is not None:
            renderer.stop()
        dispatcher.close()
        if status_reporter is not None:
            if lifecycle["stoppedAt"] is None:
                lifecycle["stoppedAt"] = utc_now()
            if lifecycle["status"] not in {"error", "stopped"}:
                lifecycle["status"] = "stopped"
                lifecycle["message"] = "native N4 bridge stopped"
            status_reporter.stop(native_snapshot())


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.live:
            return _run_live(args)
        # No mode means the safe HTTP-only behavior.  This is deliberate: a
        # typo cannot unexpectedly claim the physical N4.
        return _run_safe(args)
    except (WebUiBridgeError, ValueError, OSError, ImportError) as exc:
        print(f"n4-webui-bridge: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "DEFAULT_WEBUI_URL",
    "LIVE_WEBUI_FEATURES",
    "NativeStatusReporter",
    "SAFE_WEBUI_FEATURES",
    "VisualStatePoller",
    "WEBUI_API_VERSION",
    "WEBUI_SERVER_SCHEMA",
    "WebUiBridgeError",
    "WebUiClient",
    "main",
]
