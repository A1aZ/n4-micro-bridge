import json
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from n4_webui_bridge import (  # noqa: E402
    NativeStatusReporter,
    SAFE_WEBUI_FEATURES,
    VisualStatePoller,
    WEBUI_API_VERSION,
    WEBUI_SERVER_SCHEMA,
    WebUiBridgeError,
    WebUiClient,
    _lighting_digest,
    main,
)


def snapshot(color=0x123456):
    return {
        "lighting": {
            "agents": [{"c": color, "b": 1, "e": 1}] + [{} for _ in range(5)],
            "keys": {"c": 0, "b": 0, "e": 0},
            "ambient": {"c": 0, "b": 0, "e": 0},
        },
        "model": {"buttons": [{"targetKey": "AG00", "enabled": True}] + [{} for _ in range(13)]},
    }


class FakeResponse:
    status = 200

    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.value).encode("utf-8")


class FakeRenderer:
    def __init__(self):
        self.snapshots = []

    def apply_snapshot(self, payload):
        self.snapshots.append(payload)


class N4WebUiBridgeTests(unittest.TestCase):
    def test_client_rejects_old_webui_contract_before_live_work(self):
        def opener(request, timeout):
            self.assertEqual(request.full_url, "http://127.0.0.1:8801/api/state")
            return FakeResponse({"identity": {}, "bridge": {}, "transport": {}, "visual": {}})

        client = WebUiClient("http://127.0.0.1:8801", opener=opener)
        with self.assertRaisesRegex(WebUiBridgeError, "old/incomplete"):
            client.require_contract(SAFE_WEBUI_FEATURES)

    def test_client_accepts_current_contract_and_versioned_features(self):
        calls = []

        def opener(request, timeout):
            calls.append(request.full_url)
            return FakeResponse({
                "server": {"schema": WEBUI_SERVER_SCHEMA, "apiVersion": WEBUI_API_VERSION},
                "features": {"visualApi": 2, "transportApi": 1, "eventStream": True},
            })

        client = WebUiClient("http://127.0.0.1:8801", opener=opener)
        value = client.require_contract(SAFE_WEBUI_FEATURES)
        self.assertEqual(value["server"]["apiVersion"], WEBUI_API_VERSION)
        self.assertEqual(calls, ["http://127.0.0.1:8801/api/state"])

    def test_client_posts_native_status_and_reporter_is_best_effort(self):
        calls = []

        def opener(request, timeout):
            calls.append({
                "url": request.full_url,
                "method": request.method,
                "timeout": timeout,
                "body": json.loads(request.data.decode("utf-8")),
            })
            return FakeResponse({"ok": True, "accepted": True})

        client = WebUiClient("http://127.0.0.1:8801", opener=opener)
        payload = {"status": "running", "n4": {"opened": True}}
        self.assertEqual(client.native_state(payload)["accepted"], True)
        self.assertEqual(calls[0]["url"], "http://127.0.0.1:8801/api/native/state")
        self.assertEqual(calls[0]["method"], "POST")
        self.assertEqual(calls[0]["body"], payload)

        reporter = NativeStatusReporter(
            client,
            lambda: payload,
            interval_ms=100,
            status_stream=None,
        )
        reporter.publish()
        self.assertEqual(reporter.posts, 1)
        self.assertEqual(reporter.failures, 0)
        self.assertFalse(reporter.snapshot()["running"])

        failing = WebUiClient(
            "http://127.0.0.1:8801",
            opener=lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("offline")),
        )
        failed_reporter = NativeStatusReporter(
            failing,
            lambda: payload,
            interval_ms=100,
            status_stream=None,
        )
        self.assertIsNone(failed_reporter.publish())
        self.assertEqual(failed_reporter.failures, 1)
        self.assertIn("offline", failed_reporter.last_error)

    def test_native_status_reporter_heartbeat_stops_cleanly(self):
        calls = []

        def opener(request, timeout):
            calls.append(json.loads(request.data.decode("utf-8")))
            return FakeResponse({"ok": True})

        client = WebUiClient("http://127.0.0.1:8801", opener=opener)
        reporter = NativeStatusReporter(
            client,
            lambda: {"status": "running", "lastSeenAt": "now"},
            interval_ms=100,
            status_stream=None,
        )
        reporter.start()
        time.sleep(0.24)
        reporter.stop({"status": "stopped"})
        self.assertGreaterEqual(len(calls), 2)
        self.assertFalse(reporter.snapshot()["running"])
        self.assertEqual(calls[-1]["status"], "stopped")

    def test_client_and_poller_only_apply_changed_state(self):
        values = [snapshot(), snapshot(), snapshot(0xABCDEF)]

        def opener(_request, timeout):
            self.assertGreater(timeout, 0)
            return FakeResponse(values.pop(0))

        client = WebUiClient("http://127.0.0.1:9", opener=opener)
        renderer = FakeRenderer()
        poller = VisualStatePoller(client, renderer, interval_ms=50, status_stream=None)
        poller.fetch_once()
        poller.fetch_once()
        poller.fetch_once()
        self.assertEqual(poller.fetches, 3)
        self.assertEqual(poller.updates, 2)
        self.assertEqual(len(renderer.snapshots), 2)
        self.assertNotEqual(_lighting_digest(renderer.snapshots[0]), _lighting_digest(renderer.snapshots[1]))

    def test_target_mapping_change_is_not_hidden_by_same_lighting(self):
        first = snapshot()
        second = snapshot()
        second["model"]["buttons"][0]["targetKey"] = "ACT06"
        values = [first, second]

        def opener(_request, timeout):
            return FakeResponse(values.pop(0))

        client = WebUiClient("http://127.0.0.1:9", opener=opener)
        renderer = FakeRenderer()
        poller = VisualStatePoller(client, renderer, interval_ms=50, status_stream=None)
        poller.fetch_once()
        poller.fetch_once()
        self.assertEqual(poller.updates, 2)

    def test_safe_cli_uses_offline_snapshot_and_never_opens_n4(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "visual.json"
            path.write_text(json.dumps(snapshot()), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "n4-webui-bridge.py"),
                 "--dry-run", "--state-json", str(path)],
                cwd=ROOT, capture_output=True, text=True, check=True,
            )
            value = json.loads(result.stdout)
            self.assertEqual(value["mode"], "dry-run")
            self.assertFalse(value["n4Opened"])
            self.assertFalse(value["driverTouched"])

    def test_default_mode_is_safe_http_check(self):
        # An unreachable endpoint must fail as an HTTP diagnostic, not attempt
        # to import the SDK or open a device.  Use a reserved local port.
        self.assertEqual(main(["--webui-url", "http://127.0.0.1:9", "--timeout", "0.05", "--dry-run"]), 2)

    def test_help_explains_explicit_live_boundary(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "n4-webui-bridge.py"), "--help"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        )
        self.assertIn("--live", result.stdout)
        self.assertIn("never opens USB unless --live", result.stdout)
        self.assertIn("--status-interval-ms", result.stdout)
        self.assertIn("--no-status", result.stdout)
        self.assertIn("--hidapi-input", result.stdout)
        self.assertIn("--hidapi-read-timeout-ms", result.stdout)


if __name__ == "__main__":
    unittest.main()
