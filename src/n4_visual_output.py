"""Pillow renderer for Codex Micro lighting messages on a real N4.

The JavaScript ``n4-visuals.cjs`` module is the canonical preview/state model
used by the WebUI.  This small Python counterpart intentionally mirrors its
wire fields rather than trying to execute JavaScript from the native bridge.
It keeps a sticky ``thstatus``/``rgbcfg`` state, renders JPEGs at the exact
N4 SDK dimensions, and calls thread-safe methods on ``N4NativeAdapter``:
``set_key_image``, ``set_touchscreen_image`` and (optionally) ``refresh``.

Rendering is lazy and isolated in a worker.  A missing N4, missing Pillow, or
an SDK upload error leaves the state dirty and is retried later; it never
installs a driver or terminates the sideband output reader.
"""

from __future__ import annotations

import hashlib
import json
import math
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from n4_visual_renderer import N4VisualRenderer


SCREEN_SIZE = (800, 480)
MAIN_KEY_SIZE = (112, 112)
SECONDARY_KEY_SIZE = (176, 112)
DEFAULT_TARGETS = (
    "AG00", "AG01", "AG02", "AG03", "AG04", "AG05",
    "ACT06", "ACT07", "ACT08", "ACT09", "ACT10", "ACT11", "ACT12", None,
)
EFFECT_IDS = {
    "off": 0, "solid": 1, "snake": 2, "rainbow": 3,
    "breath": 4, "gradient": 5, "shallowbreath": 6,
    "shallow_breath": 6,
}
DEFAULT_LIGHT = {"c": 0, "b": 0.0, "e": 0, "s": 0.0, "sk": 0.0, "sa": 0.0, "m": 0.0, "set": False}


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return min(high, max(low, value))


def _color(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("color must not be boolean")
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)) or float(value) < 0:
            raise ValueError("color must be non-negative")
        return int(value) & 0xFFFFFF
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("#"):
            text = text[1:]
        elif text.lower().startswith("0x"):
            text = text[2:]
        if len(text) == 3:
            text = "".join(ch * 2 for ch in text)
        if len(text) in (6, 8) and all(ch in "0123456789abcdefABCDEF" for ch in text):
            return int(text[:6], 16)
    raise ValueError("color must be #RRGGBB or an integer")


def _unit(value: Any, field: str) -> float:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} must be a finite number")
    return _clamp(number)


def _effect(value: Any) -> int:
    if isinstance(value, str):
        text = value.strip()
        if text.lower() in EFFECT_IDS:
            return EFFECT_IDS[text.lower()]
        if text.isdigit():
            return max(0, min(255, int(text)))
        raise ValueError(f"unknown lighting effect: {value}")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("lighting effect must be a number or known name") from exc
    return max(0, min(255, number))


def _merge_light(base: Optional[Mapping[str, Any]], patch: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    out = dict(DEFAULT_LIGHT)
    if isinstance(base, Mapping):
        out.update(base)
    if patch is None:
        return out
    if not isinstance(patch, Mapping):
        raise ValueError("lighting patch must be an object")
    aliases = {"color": "c", "brightness": "b", "effect": "e", "speed": "s", "magic": "m"}
    for source, target in aliases.items():
        if source in patch and target not in patch:
            patch = {**patch, target: patch[source]}
    if "c" in patch:
        out["c"] = _color(patch["c"])
    if "b" in patch:
        out["b"] = _unit(patch["b"], "light.b")
    if "e" in patch:
        out["e"] = _effect(patch["e"])
    if "s" in patch:
        out["s"] = _unit(patch["s"], "light.s")
    for field in ("sk", "sa", "m"):
        if field in patch:
            out[field] = _unit(patch[field], f"light.{field}")
    if patch:
        out["set"] = True
    return out


def _new_state() -> dict[str, Any]:
    return {
        "agents": [dict(DEFAULT_LIGHT) for _ in range(6)],
        "keys": dict(DEFAULT_LIGHT),
        "ambient": dict(DEFAULT_LIGHT),
    }


class N4VisualOutput:
    """Render sticky Micro lighting state and upload changed N4 images."""

    def __init__(self, adapter: Any, *, targets: Optional[Sequence[Optional[str]]] = None,
                 refresh_ms: int = 250, target: str = "both",
                 status_stream: Any = None,
                 on_update: Optional[Callable[[Mapping[str, Any]], None]] = None) -> None:
        if target not in {"both", "screen", "keys"}:
            raise ValueError("visual target must be both, screen, or keys")
        if refresh_ms < 20:
            raise ValueError("visual refresh_ms must be at least 20")
        # Importing Pillow belongs to live mode only.  Fail with a focused
        # message if the SDK environment omitted it.
        try:
            from PIL import Image, ImageDraw  # noqa: F401
        except ImportError as exc:
            raise RuntimeError("Pillow is required for N4 visual output; install SDK requirements") from exc
        self.adapter = adapter
        self.targets = tuple(targets or DEFAULT_TARGETS)
        if len(self.targets) != 14:
            raise ValueError("N4 visual targets must contain 14 entries")
        self.theme = 'debug'
        self.strip_options = {}
        self._renderer = self._make_renderer(self.targets, self.theme)
        self.refresh_ms = int(refresh_ms)
        self.target = target
        self.status_stream = status_stream
        self.on_update = on_update
        self.state = _new_state()
        self._tmpdir = tempfile.TemporaryDirectory(prefix="mirabox-codex-n4-")
        self._hashes: dict[str, str] = {}
        self._render_inputs = {}
        self.last_frame = {}
        self._startup_repaint_at = None
        self._ready_since = None
        self._batch_signature = None
        self.screen_test = {'mode':'off'}
        from n4_frame_cache import NativeFrameCache
        self._frame_cache=NativeFrameCache()
        self._dirty = True
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        self._generation = 0
        self._phase = 0.5
        self.last_error: Optional[str] = None
        self.uploads = 0

    def _status(self, message: str) -> None:
        if self.status_stream is not None:
            print(message, file=self.status_stream, flush=True)

    @staticmethod
    def _make_renderer(targets: Sequence[Optional[str]], theme: str = 'debug', options=None) -> N4VisualRenderer:
        """Create the shared pure renderer for a target mapping."""

        return N4VisualRenderer(
            {"visual": {"theme": theme, **{key:value for key,value in (options or {}).items() if key!='knobs'}}, "knobs":(options or {}).get('knobs',[]), "buttons": [
                {"label": f"N4 按键 {index + 1}", "targetKey": target,
                 "enabled": target is not None}
                for index, target in enumerate(targets)
            ]},
            jpeg_quality=88,
        )

    def handle(self, method: str, params: Any) -> None:
        """Apply a host lighting message and wake the renderer."""

        with self._lock:
            if method == "v.oai.thstatus":
                if not isinstance(params, list):
                    raise ValueError("v.oai.thstatus params must be an array")
                next_state = {**self.state, "agents": [dict(item) for item in self.state["agents"]]}
                for update in params:
                    if not isinstance(update, Mapping) or not isinstance(update.get("id"), int):
                        raise ValueError("thstatus slot id must be an integer")
                    ident = int(update["id"])
                    if not 0 <= ident < 6:
                        raise ValueError("thstatus slot id must be between 0 and 5")
                    patch = {key: value for key, value in update.items() if key != "id"}
                    next_state["agents"][ident] = _merge_light(next_state["agents"][ident], patch)
                self.state = next_state
            elif method == "v.oai.rgbcfg":
                if not isinstance(params, Mapping):
                    raise ValueError("v.oai.rgbcfg params must be an object")
                next_state = {**self.state}
                for side in ("keys", "ambient"):
                    if side in params and params[side] is not None:
                        next_state[side] = _merge_light(self.state[side], params[side])
                self.state = next_state
            elif method == "lights.preview":
                if not isinstance(params, Mapping):
                    raise ValueError("lights.preview params must be an object")
                next_state = {**self.state}
                if params.get("backlight") is not None:
                    next_state["keys"] = _merge_light(self.state["keys"], params["backlight"])
                if params.get("underglow") is not None:
                    next_state["ambient"] = _merge_light(self.state["ambient"], params["underglow"])
                self.state = next_state
            else:
                return
            self._dirty = True
            self._wake.set()

    def apply_snapshot(self, payload: Mapping[str, Any]) -> None:
        """Replace lighting from a WebUI ``/api/visual/state`` snapshot.

        The endpoint returns canonical state rather than the original RPC
        message.  This method lets a polling companion feed that snapshot into
        the same renderer used by the sideband RPC callback, without requiring
        a virtual HID device or a second N4 handle.
        """

        if not isinstance(payload, Mapping):
            raise ValueError("visual snapshot must be an object")
        lighting = payload.get("lighting")
        if lighting is None and isinstance(payload.get("model"), Mapping):
            lighting = payload["model"].get("lighting")
        if not isinstance(lighting, Mapping):
            raise ValueError("visual snapshot has no lighting object")
        agents_value = lighting.get("agents", [])
        if not isinstance(agents_value, list):
            raise ValueError("visual lighting agents must be an array")
        next_state = _new_state()
        for index in range(min(6, len(agents_value))):
            next_state["agents"][index] = _merge_light(DEFAULT_LIGHT, agents_value[index])
        if lighting.get("keys") is not None:
            next_state["keys"] = _merge_light(DEFAULT_LIGHT, lighting.get("keys"))
        if lighting.get("ambient") is not None:
            next_state["ambient"] = _merge_light(DEFAULT_LIGHT, lighting.get("ambient"))
        model = payload.get("model")
        if model is None and isinstance(payload.get("visual"), Mapping):
            visual_value = payload["visual"]
            model = visual_value.get("model", visual_value)
        next_targets: Optional[tuple[Optional[str], ...]] = None
        next_theme = model.get('theme', self.theme) if isinstance(model, Mapping) else self.theme
        from n4_themes import validate_theme
        validate_theme(next_theme)
        next_options={"stripMode":model.get('stripMode','buttons'),"stripBrightness":model.get('stripBrightness'),"knobs":model.get('knobs',[])} if isinstance(model,Mapping) else self.strip_options
        if isinstance(model, Mapping) and isinstance(model.get("buttons"), list) and len(model["buttons"]) == 14:
            next_targets = tuple(
                (str(button.get("targetKey")) if button.get("targetKey") is not None and button.get("enabled", True) else None)
                if isinstance(button, Mapping) else None
                for button in model["buttons"]
            )
        with self._lock:
            self.state = next_state
            diagnostic=model.get('screenTest',{'mode':'off'}) if isinstance(model,Mapping) else {'mode':'off'}
            if not isinstance(diagnostic,Mapping) or diagnostic.get('mode') not in ('off','black','white','box'):
                raise ValueError('Invalid screen diagnostic')
            self.screen_test=dict(diagnostic)
            if (next_targets is not None and next_targets != self.targets) or next_theme != self.theme or next_options != self.strip_options:
                if next_targets is not None: self.targets = next_targets
                self.theme = next_theme
                self.strip_options = next_options
                self._renderer = self._make_renderer(self.targets, self.theme, self.strip_options)
            self._dirty = True
            self._generation = getattr(self, "_generation", 0) + 1
            self._wake.set()

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._ensure_tmpdir()
            self._stop.clear()
            self._dirty = True
            self._wake.set()
            self._thread = threading.Thread(target=self._loop, name="n4-visual-output", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
        # Never remove temporary files while a worker may still be uploading
        # one of them.  A slow SDK call can outlive the bounded join; leave the
        # directory in place and let a later stop/GC clean it safely.
        if thread is not None and thread.is_alive():
            self._status("[n4-visual] worker did not stop before timeout; retaining temporary files")
            return
        with self._lock:
            self._thread = None
            temporary = self._tmpdir
            self._tmpdir = None
            self._hashes.clear()
            self._render_inputs.clear()
        if temporary is not None:
            temporary.cleanup()

    def _ensure_tmpdir(self) -> Path:
        """Return a live temporary directory, recreating it after stop()."""

        temporary = self._tmpdir
        if temporary is None or not Path(temporary.name).is_dir():
            self._tmpdir = tempfile.TemporaryDirectory(prefix="mirabox-codex-n4-")
            self._hashes.clear()
            temporary = self._tmpdir
        return Path(temporary.name)

    def flush(self, timeout: float = 2.0) -> bool:
        """Render synchronously once; useful for tests and graceful startup."""

        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            result = self.render_once()
            if result.get("ok"):
                return True
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        return False

    def _loop(self) -> None:
        interval = self.refresh_ms / 1000.0
        next_frame = time.monotonic()
        animation_start=next_frame
        while not self._stop.is_set():
            self._wake.wait(max(0.0,next_frame-time.monotonic()))
            self._wake.clear()
            # A new host status can wake us early. Coalesce it into the next
            # frame instead of bypassing the USB pacing limit.
            if self._stop.wait(max(0.0,next_frame-time.monotonic())):
                return
            if self._stop.is_set():
                return
            with self._lock:
                startup_due=self._startup_repaint_at is not None and time.monotonic()>=self._startup_repaint_at
                animated=self.screen_test.get('mode','off')=='off' and any(float(light.get('b',0))>0 and float(light.get('s',0))>0 and int(light.get('e',0)) in (2,3,4,5,6) for light in self.state['agents'])
                dirty = self._dirty or startup_due or animated
            if dirty:
                self._phase=(0.5+(time.monotonic()-animation_start)/5.0)%1.0
                self.render_once()
            # The SDK call returning does not prove the panel has finished
            # displaying the JPEG. Keep a quiet interval after each batch.
            next_frame=time.monotonic()+interval

    def _key_signature(self,index,phase):
        light=self._key_light(self.targets[index])
        is_info=index>=10 and self.strip_options.get('stripMode')=='knobs'
        animated=not is_info and int(light.get('e',0)) in {2,3,4,5,6} and float(light.get('b',0))>0
        # Non-debug Agent animations explicitly honor speed=0.
        if self.theme!='debug' and str(self.targets[index]).startswith('AG'):
            animated=animated and float(light.get('s',0))>0
        return json.dumps([self.theme,self.targets[index],self.strip_options,
                           None if is_info else light,phase if animated else None],sort_keys=True)

    def _key_light(self, target: Optional[str]) -> Mapping[str, Any]:
        if not target:
            return DEFAULT_LIGHT
        if target.startswith("AG") and len(target) == 4 and target[2:].isdigit():
            index = int(target[2:])
            if 0 <= index < 6:
                return self.state["agents"][index]
        if target.startswith("ACT"):
            return self.state["keys"]
        return DEFAULT_LIGHT

    def _render_key(self, index: int, target: Optional[str], phase: float) -> Any:
        # Keep this compatibility method because older tests/callers use the
        # zero-based index + explicit target signature.  The actual drawing
        # lives in N4VisualRenderer so sideband output and offline previews
        # cannot drift apart.
        return self._renderer.render_key(
            index + 1,
            self.state,
            phase=phase,
            target_override=False if target is None else target,
        )

    def _render_screen(self, phase: float) -> Any:
        return self._renderer.render(self.state, phase=phase).screen

    @staticmethod
    def _save(image: Any, path: Path) -> str:
        # Lossless intermediate; the SDK performs the sole JPEG encoding.
        image.save(path, format="PNG")
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def _check_upload_result(value: Any, operation: str) -> Any:
        """Treat the upstream SDK's conventional negative return as failure."""

        if isinstance(value, (int, float)) and not isinstance(value, bool) and value < 0:
            raise RuntimeError(f"N4 SDK {operation} failed with status {value}")
        return value

    def render_once(self, *, force: bool = False) -> dict[str, Any]:
        """Render/upload changed assets; return a diagnostic summary."""

        with self._lock:
            phase = int(self._phase*20)%20/20.0
            started=time.perf_counter()
            rendered=0
            try:
                ready_check=getattr(self.adapter,'wait_until_ready',None)
                paced=callable(ready_check)
                if paced:
                    if not ready_check(timeout=0):
                        self._ready_since=None
                        self._batch_signature=None
                        self._hashes.clear()
                        return {'ok':False,'waitingForDevice':True,'uploads':[]}
                    if self._ready_since is None:self._ready_since=time.monotonic()
                    if time.monotonic()-self._ready_since<0.75:
                        return {'ok':False,'waitingForDevice':True,'uploads':[]}
                tempdir = self._ensure_tmpdir()
                startup_repaint=self._startup_repaint_at is not None and time.monotonic()>=self._startup_repaint_at
                # USB writes returning successfully do not acknowledge LCD
                # presentation. Retry every key once after startup settles,
                # including static keys; do NOT resend the black background.
                diagnostic=self.screen_test.get('mode','off')
                animated=any(float(light.get('b',0))>0 and float(light.get('s',0))>0 and int(light.get('e',0)) in (2,3,4,5,6) for light in self.state['agents'])
                signature=json.dumps(self.screen_test if diagnostic!='off' else [self.state,self.theme,self.targets,self.strip_options,phase if animated else None],sort_keys=True)
                repaint_keys=force or startup_repaint or signature!=self._batch_signature or any(f'key:{i}' not in self._hashes for i in range(1,15))
                native_frames=diagnostic=='off' and callable(getattr(self.adapter,'supports_native_frames',None)) and self.adapter.supports_native_frames()
                # Render a single immutable snapshot so screen and key files
                # share exactly the same lighting phase.  The renderer owns
                # all Pillow drawing; this class remains an upload/cache
                # worker only.
                bundle = self._renderer.render(self.state, phase=phase) if self.target=='screen' or (repaint_keys and not native_frames) else None
                uploads: list[dict[str, Any]] = []
                pending_hashes: dict[str, str] = {}
                pending_inputs = {}
                # N4's 800x480 API is a BACKGROUND across the physical display
                # windows, not a separate monitor. The dashboard bundle.screen
                # is a software preview; uploading it after the individual keys
                # crops its title/grid through the key caps and overwrites them.
                # In normal 'both' mode clear the background FIRST, then let the
                # SDK place all 10 main + 4 secondary key images in their regions.
                # 'screen' remains an explicit whole-background diagnostic mode.
                if self.target=='screen' or (self.target=='both' and (force or 'screen' not in self._hashes)):
                    from PIL import Image
                    background = Image.new("RGB", SCREEN_SIZE, (0, 0, 0)) if self.target == "both" else bundle.screen
                    path = tempdir / "screen.png"
                    digest = self._save(background, path)
                    if force or self._hashes.get("screen") != digest:
                        # Even a failed/ambiguous background write may have
                        # erased key regions. Invalidate them before attempting
                        # it, so the next retry cannot skip cached key images.
                        self._hashes = {key: value for key, value in self._hashes.items() if not key.startswith("key:")}
                        result = self.adapter.set_touchscreen_image(str(path))
                        self._check_upload_result(result, "set_touchscreen_image")
                        pending_hashes["screen"] = digest
                        uploads.append({"kind": "background" if self.target == "both" else "screen", "path": str(path)})
                        if paced and self._stop.wait(0.3):raise RuntimeError('Display initialization cancelled')
                if self.target in {"both", "keys"} and repaint_keys:
                    for index in range(14):
                        cache_key = f"key:{index + 1}"
                        if native_frames:
                            frame_signature=self._key_signature(index,phase)
                            misses=self._frame_cache.misses
                            data=self._frame_cache.get((index,frame_signature),lambda:self._renderer.render_key(index+1,self.state,phase=phase))
                            rendered+=self._frame_cache.misses-misses
                            digest=hashlib.sha256(data).hexdigest()
                            if not force and not startup_repaint and self._hashes.get(cache_key)==digest:continue
                            self.adapter.set_key_frame(index+1,data)
                            pending_hashes[cache_key]=digest
                            uploads.append({'kind':'key','logicalKey':index+1,'bytes':len(data),'transport':'memory'})
                            if paced and self._stop.wait(.04):raise RuntimeError('Display upload cancelled')
                            continue
                        image=bundle.keys[index]
                        if diagnostic!='off':
                            from PIL import Image,ImageDraw
                            image=Image.new('RGB',image.size,'white' if diagnostic=='white' else 'black')
                            if diagnostic=='box':
                                width,height=image.size
                                ImageDraw.Draw(image).rectangle((width//2-22,height//2-22,width//2+21,height//2+21),outline='white',width=3)
                        rendered+=1
                        path = tempdir / f"key-{index + 1}.png"
                        digest = self._save(image, path)
                        result = self.adapter.set_key_image(index + 1, str(path))
                        self._check_upload_result(result, f"set_key_image({index + 1})")
                        pending_hashes[cache_key] = digest
                        uploads.append({"kind": "key", "logicalKey": index + 1, "path": str(path)})
                        if paced and self._stop.wait(0.04):raise RuntimeError('Display upload cancelled')
                refresh = getattr(self.adapter, "refresh", None)
                if uploads and callable(refresh):
                    self._check_upload_result(refresh(), "refresh")
                # Commit hashes only after every upload and the display refresh
                # succeed.  If refresh fails, the next retry resends the full
                # batch instead of falsely believing it is on the device.
                self._hashes.update(pending_hashes)
                self._render_inputs.update(pending_inputs)
                self._batch_signature=signature
                if startup_repaint:self._startup_repaint_at=None
                if 'screen' in pending_hashes and self.target=='both':
                    self._startup_repaint_at=time.monotonic()+2.0
                if diagnostic!='off':self._startup_repaint_at=None
                self._dirty = False
                self.last_error = None
                self.uploads += len(uploads)
                self.last_frame={"durationMs":round((time.perf_counter()-started)*1000,2),"renderedKeys":rendered,"uploadedRegions":len(uploads),"targetIntervalMs":self.refresh_ms,"frameTransport":"memory" if native_frames else "file","cacheHits":self._frame_cache.hits,"cacheBytes":self._frame_cache.size}
                result = {"ok": True, "uploads": uploads, "phase": phase,"performance":self.last_frame}
            except Exception as exc:
                self.last_error = str(exc)
                self._dirty = True
                self._status(f"[n4-visual] upload deferred: {exc}")
                result = {"ok": False, "error": str(exc), "phase": phase}
        if self.on_update:
            self.on_update(result)
        return result


__all__ = [
    "DEFAULT_TARGETS",
    "N4VisualOutput",
    "SCREEN_SIZE",
    "MAIN_KEY_SIZE",
    "SECONDARY_KEY_SIZE",
]
