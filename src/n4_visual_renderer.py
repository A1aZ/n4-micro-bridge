"""Pillow renderer for the Mirabox N4 output surface.

The renderer deliberately has no USB, HID, SDK, or WebUI dependency.  It
turns the lighting state kept by :class:`HostRpcEndpoint` (or an equivalent
mapping) into one 800x480 screen image and fourteen key images.  The first ten
keys use the N4 main-key format (112x112); keys 11..14 use the secondary-key
format (176x112).  Images are returned in the orientation expected by the
Python SDK; the SDK applies its documented 180-degree transport rotation.

Typical sideband usage::

    from n4_visual_renderer import N4VisualRenderer

    renderer = N4VisualRenderer()
    # ``endpoint`` can be a HostRpcEndpoint, or a mapping with ``agents`` /
    # ``keys`` / ``ambient`` (and optionally ``slots`` / ``lighting``).
    bundle = renderer.render(endpoint)
    paths = bundle.save_jpegs("out")
    # device.set_touchscreen_image(str(paths.screen))
    # for key, path in enumerate(paths.keys, 1):
    #     device.set_key_image(key, str(path))

Rendering is intentionally conservative: unknown animation effects are
rendered as a solid colour at the supplied deterministic phase, while
``breath`` and ``shallowBreath`` sample their brightness.  A missing Pillow
installation is reported only when image rendering is requested, allowing
protocol/sideband code to remain importable on minimal systems.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
from datetime import datetime
import math
import os
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

try:  # Pillow is optional for the protocol-only portions of this project.
    from PIL import Image, ImageDraw, ImageFont
except ImportError as exc:  # pragma: no cover - exercised by downstream users
    Image = None  # type: ignore[assignment]
    ImageDraw = None  # type: ignore[assignment]
    ImageFont = None  # type: ignore[assignment]
    _PILLOW_IMPORT_ERROR: Optional[BaseException] = exc
else:
    _PILLOW_IMPORT_ERROR = None


SCREEN_SIZE = (800, 480)
MAIN_KEY_SIZE = (112, 112)
SECONDARY_KEY_SIZE = (176, 112)
KEY_COUNT = 14
MAIN_KEY_COUNT = 10
SECONDARY_KEY_COUNT = 4
DEFAULT_JPEG_QUALITY = 90

EFFECT_NAMES = (
    "off",
    "solid",
    "snake",
    "rainbow",
    "breath",
    "gradient",
    "shallowBreath",
)
EFFECT_IDS = {
    name.lower(): index for index, name in enumerate(EFFECT_NAMES)
}
EFFECT_IDS.update({"shallow_breath": 6, "shallow-breath": 6})

DEFAULT_TARGETS = (
    "AG00", "AG01", "AG02", "AG03", "AG04", "AG05",
    "ACT06", "ACT07", "ACT08", "ACT09", "ACT10", "ACT11", "ACT12",
    None,
)

BASE_BACKGROUND = (9, 13, 20)
SURFACE = (17, 26, 39)
SURFACE_ALT = (23, 35, 53)
FOREGROUND = (232, 238, 247)
MUTED = (139, 154, 176)
BORDER = (49, 65, 87)
DISABLED = (41, 51, 67)
CLOCK_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _strip_panel(index: int, lighting: Any, clock: Any, touch_action: Any = None) -> dict[str, Any]:
    if index == 0:
        return {"kind": "clock", "clock": clock}
    if index in (1, 2):
        start = (index - 1) * 3
        return {
            "kind": "agents",
            "start": start,
            "agents": [light.as_dict() for light in lighting.agents[start:start + 3]],
        }
    if index == 3 and isinstance(touch_action, Mapping):
        return {"kind": "action", "action": dict(touch_action)}
    return {"kind": "knob"}


def _require_pillow() -> None:
    if Image is None:
        message = (
            "N4 visual rendering requires Pillow. Install it with "
            "`python -m pip install Pillow`."
        )
        raise RuntimeError(message) from _PILLOW_IMPORT_ERROR


def _is_mapping(value: Any) -> bool:
    return isinstance(value, Mapping)


def _clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def _number(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def normalize_clock(value: Any = None) -> dict[str, str]:
    """Return a compact local clock payload for the left information strip."""

    if isinstance(value, Mapping) and isinstance(value.get("time"), str) and isinstance(value.get("date"), str):
        date_text = value["date"]
        time_text = value["time"]
        return {
            "time": time_text,
            "date": date_text,
            "weekday": str(value.get("weekday") or ""),
            "minute": str(value.get("minute") or f"{date_text} {time_text}"),
        }
    current = value if isinstance(value, datetime) else datetime.now()
    date_text = current.strftime("%Y-%m-%d")
    time_text = current.strftime("%H:%M")
    return {
        "time": time_text,
        "date": date_text,
        "weekday": CLOCK_WEEKDAYS[current.weekday()],
        "minute": f"{date_text} {time_text}",
    }


def normalize_color(value: Any, default: int = 0) -> int:
    """Return a 24-bit integer RGB colour.

    Accepted forms are integer ``0xRRGGBB`` values, ``#RGB``/``#RRGGBB`` or
    ``0xRRGGBB`` strings, and three-item RGB sequences.  Alpha in an eight
    digit string is ignored, matching the Micro wire renderer.
    """

    if value is None:
        return int(default) & 0xFFFFFF
    if isinstance(value, bool):
        return int(value) & 0xFFFFFF
    if isinstance(value, int):
        if value < 0:
            raise ValueError("color must be non-negative")
        return value & 0xFFFFFF
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value) & 0xFFFFFF
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("#"):
            text = text[1:]
        elif text.lower().startswith("0x"):
            text = text[2:]
        if len(text) == 3 and all(char in "0123456789abcdefABCDEF" for char in text):
            text = "".join(char * 2 for char in text)
        if len(text) in (6, 8) and all(char in "0123456789abcdefABCDEF" for char in text):
            return int(text[:6], 16)
        raise ValueError("color must be an RGB integer or #RRGGBB string")
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        if len(value) != 3:
            raise ValueError("RGB sequence must contain exactly three values")
        channels = []
        for channel in value:
            number = _number(channel, -1)
            if number < 0 or number > 255:
                raise ValueError("RGB channels must be between 0 and 255")
            channels.append(int(number))
        return (channels[0] << 16) | (channels[1] << 8) | channels[2]
    raise ValueError("color must be an RGB integer or #RRGGBB string")


def color_rgb(value: Any, default: int = 0) -> tuple[int, int, int]:
    """Convert a wire colour into an ``(R, G, B)`` tuple."""

    number = normalize_color(value, default=default)
    return ((number >> 16) & 0xFF, (number >> 8) & 0xFF, number & 0xFF)


def color_hex(value: Any, default: int = 0) -> str:
    """Return an uppercase ``#RRGGBB`` representation."""

    return f"#{normalize_color(value, default=default):06X}"


def normalize_effect(value: Any, default: int = 0) -> int:
    """Convert an effect name/id into a bounded Micro effect id."""

    if value is None:
        return int(default) & 0xFF
    if isinstance(value, str):
        text = value.strip()
        key = text.lower()
        if key in EFFECT_IDS:
            return EFFECT_IDS[key]
        try:
            value = int(text, 10)
        except ValueError as exc:
            raise ValueError(f"unknown lighting effect: {value!r}") from exc
    number = _number(value, math.nan)
    if not math.isfinite(number):
        raise ValueError(f"unknown lighting effect: {value!r}")
    return int(_clamp(math.trunc(number), 0, 255))


def effect_name(effect: Any) -> str:
    """Return a human-readable effect name for an id or name."""

    number = normalize_effect(effect)
    return EFFECT_NAMES[number] if number < len(EFFECT_NAMES) else f"effect-{number}"


def animation_intensity(brightness: Any, effect: Any = 0, phase: Any = 0.5) -> float:
    """Sample a deterministic animation phase and return visible intensity."""

    base = _clamp(_number(brightness), 0.0, 1.0)
    effect_id = normalize_effect(effect)
    position = _clamp(_number(phase, 0.5), 0.0, 1.0)
    if effect_id == 0:
        return 0.0
    wave = 0.5 + 0.5 * math.sin(position * math.pi * 2 - math.pi / 2)
    if effect_id == 4:  # breath
        return base * (0.15 + 0.85 * wave)
    if effect_id == 6:  # shallowBreath
        return base * (0.5 + 0.5 * wave)
    return base


@dataclass(frozen=True)
class LightState:
    """Canonical, serialisable lighting state for one light group."""

    c: int = 0
    b: float = 0.0
    e: int = 0
    s: float = 0.0
    sk: float = 0.0
    sa: float = 0.0
    m: float = 0.0
    set: bool = False
    intensity: float = 0.0

    @property
    def rgb(self) -> tuple[int, int, int]:
        return color_rgb(self.c)

    @property
    def color_hex(self) -> str:
        return color_hex(self.c)

    @property
    def effect_name(self) -> str:
        return effect_name(self.e)

    def as_dict(self) -> dict[str, Any]:
        return {
            "c": self.c, "b": self.b, "e": self.e, "s": self.s,
            "sk": self.sk, "sa": self.sa, "m": self.m, "set": self.set,
            "intensity": self.intensity,
            "colorHex": self.color_hex, "effectName": self.effect_name,
        }


@dataclass(frozen=True)
class LightingState:
    """Six agent lights plus command-key and ambient light groups."""

    agents: tuple[LightState, ...]
    keys: LightState
    ambient: LightState

    def as_dict(self) -> dict[str, Any]:
        return {
            "agents": [light.as_dict() for light in self.agents],
            "keys": self.keys.as_dict(),
            "ambient": self.ambient.as_dict(),
        }


@dataclass(frozen=True)
class KeyBinding:
    """One logical N4 image key and its Micro target."""

    index: int
    label: str
    target_key: Optional[str]
    enabled: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "label": self.label,
            "targetKey": self.target_key,
            "enabled": self.enabled,
        }


@dataclass(frozen=True)
class N4JpegBundle:
    """JPEG bytes corresponding to one :class:`N4RenderBundle`."""

    screen: bytes
    keys: tuple[bytes, ...]

    def __post_init__(self) -> None:
        if len(self.keys) != KEY_COUNT:
            raise ValueError(f"expected {KEY_COUNT} key JPEGs, got {len(self.keys)}")


@dataclass(frozen=True)
class N4PathBundle:
    """Paths written by :meth:`N4RenderBundle.save_jpegs`."""

    screen: Path
    keys: tuple[Path, ...]

    def __post_init__(self) -> None:
        if len(self.keys) != KEY_COUNT:
            raise ValueError(f"expected {KEY_COUNT} key paths, got {len(self.keys)}")


@dataclass
class N4RenderBundle:
    """Rendered screen/keys plus the canonical metadata used to create them."""

    screen: Any
    keys: tuple[Any, ...]
    lighting: LightingState
    bindings: tuple[KeyBinding, ...]
    key_lights: tuple[LightState, ...]
    key_sources: tuple[str, ...]
    phase: float

    def __post_init__(self) -> None:
        if len(self.keys) != KEY_COUNT:
            raise ValueError(f"expected {KEY_COUNT} key images, got {len(self.keys)}")
        if len(self.bindings) != KEY_COUNT or len(self.key_lights) != KEY_COUNT:
            raise ValueError("render metadata must contain fourteen keys")

    @property
    def main_keys(self) -> tuple[Any, ...]:
        return self.keys[:MAIN_KEY_COUNT]

    @property
    def secondary_keys(self) -> tuple[Any, ...]:
        return self.keys[MAIN_KEY_COUNT:]

    def metadata(self) -> dict[str, Any]:
        """Return a JSON-friendly description without embedding image bytes."""

        return {
            "schema": "mirabox.n4.render",
            "version": 1,
            "phase": self.phase,
            "screen": {"width": SCREEN_SIZE[0], "height": SCREEN_SIZE[1], "format": "JPEG", "rotation": 180},
            "mainKey": {"width": MAIN_KEY_SIZE[0], "height": MAIN_KEY_SIZE[1], "format": "JPEG", "rotation": 180},
            "secondaryKey": {"width": SECONDARY_KEY_SIZE[0], "height": SECONDARY_KEY_SIZE[1], "format": "JPEG", "rotation": 180},
            "lighting": self.lighting.as_dict(),
            "buttons": [
                {
                    **binding.as_dict(),
                    "source": source,
                    "light": light.as_dict(),
                    "width": MAIN_KEY_SIZE[0] if index <= MAIN_KEY_COUNT else SECONDARY_KEY_SIZE[0],
                    "height": MAIN_KEY_SIZE[1] if index <= MAIN_KEY_COUNT else SECONDARY_KEY_SIZE[1],
                }
                for index, (binding, light, source) in enumerate(
                    zip(self.bindings, self.key_lights, self.key_sources), 1
                )
            ],
        }

    def to_jpegs(self, quality: int = DEFAULT_JPEG_QUALITY) -> N4JpegBundle:
        """Encode the rendered images as RGB JPEG bytes."""

        _require_pillow()
        return N4JpegBundle(
            screen=_jpeg_bytes(self.screen, quality),
            keys=tuple(_jpeg_bytes(image, quality) for image in self.keys),
        )

    def save_jpegs(
        self,
        directory: os.PathLike[str] | str,
        *,
        prefix: str = "n4",
        quality: int = DEFAULT_JPEG_QUALITY,
    ) -> N4PathBundle:
        """Write ``<prefix>-screen.jpg`` and ``<prefix>-key-XX.jpg`` files."""

        if not prefix or any(char in prefix for char in "/\\"):
            raise ValueError("prefix must be a non-empty filename stem")
        output = Path(directory)
        output.mkdir(parents=True, exist_ok=True)
        encoded = self.to_jpegs(quality)
        screen_path = output / f"{prefix}-screen.jpg"
        screen_path.write_bytes(encoded.screen)
        key_paths = []
        for index, value in enumerate(encoded.keys, 1):
            path = output / f"{prefix}-key-{index:02d}.jpg"
            path.write_bytes(value)
            key_paths.append(path)
        return N4PathBundle(screen=screen_path, keys=tuple(key_paths))


def _first(mapping: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in mapping and mapping[name] is not None:
            return mapping[name]
    return default


def normalize_light(
    value: Any = None,
    *,
    phase: float = 0.5,
    strict: bool = False,
) -> LightState:
    """Normalise one Micro light object.

    ``strict=False`` is intentional for live sideband callbacks: a malformed
    optional field should not stop N4 image updates.  Set ``strict=True`` for
    configuration validation or tests that want errors surfaced.
    """

    raw = value if _is_mapping(value) else {}

    def parse_unit(item: Any, label: str) -> float:
        if isinstance(item, bool):
            return 1.0 if item else 0.0
        try:
            number = float(item)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} must be a finite number") from exc
        if not math.isfinite(number):
            raise ValueError(f"{label} must be a finite number")
        return number

    def field(name: str, default: Any, parser):
        candidate = _first(raw, name, {
            "c": "color", "b": "brightness", "e": "effect", "s": "speed"
        }.get(name, name), default=default)
        try:
            return parser(candidate)
        except (TypeError, ValueError):
            if strict:
                raise
            return parser(default)

    c = field("c", 0, normalize_color)
    b = _clamp(field("b", 0.0, lambda item: parse_unit(item, "light.b")), 0.0, 1.0)
    e = field("e", 0, normalize_effect)
    s = _clamp(field("s", 0.0, lambda item: parse_unit(item, "light.s")), 0.0, 1.0)
    sk = _clamp(field("sk", 0.0, lambda item: parse_unit(item, "light.sk")), 0.0, 1.0)
    sa = _clamp(field("sa", 0.0, lambda item: parse_unit(item, "light.sa")), 0.0, 1.0)
    m = _clamp(field("m", 0.0, lambda item: parse_unit(item, "light.m")), 0.0, 1.0)
    phase_value = _clamp(_number(phase, 0.5), 0.0, 1.0)
    # HostRpcEndpoint currently does not retain an explicit ``set`` bit.  A
    # non-zero field is enough for a useful preview; explicit set=false wins.
    touched = any(raw.get(name) not in (None, 0, 0.0, False, "") for name in ("c", "b", "e", "s", "sk", "sa", "m"))
    is_set = bool(raw.get("set", touched))
    return LightState(
        c=c, b=b, e=e, s=s, sk=sk, sa=sa, m=m, set=is_set,
        intensity=animation_intensity(b, e, phase_value),
    )


def _source_parts(source: Any) -> tuple[Any, Any, Any, Any]:
    """Extract agents/slots, keys, ambient, and optional nested lighting."""

    if source is None:
        return [], {}, {}, {}
    if hasattr(source, "slots") or hasattr(source, "lighting"):
        agents = getattr(source, "slots", getattr(source, "agents", []))
        lighting = getattr(source, "lighting", {})
        if not _is_mapping(lighting):
            lighting = {}
        return agents, lighting.get("keys", {}), lighting.get("ambient", {}), lighting
    if not _is_mapping(source):
        raise TypeError("lighting source must be a mapping or HostRpcEndpoint-like object")
    nested = source.get("lighting", {})
    if not _is_mapping(nested):
        nested = {}
    agents = source.get("agents", source.get("slots", nested.get("agents", nested.get("slots", []))))
    keys = source.get("keys", nested.get("keys", {}))
    ambient = source.get("ambient", nested.get("ambient", {}))
    return agents, keys, ambient, nested


def normalize_lighting(source: Any = None, *, phase: float = 0.5, strict: bool = False) -> LightingState:
    """Build a canonical :class:`LightingState` from endpoint or mapping data."""

    if isinstance(source, LightingState):
        source = source.as_dict()
    agents_raw, keys_raw, ambient_raw, _ = _source_parts(source)
    if isinstance(agents_raw, (str, bytes, bytearray)) or not isinstance(agents_raw, Sequence):
        if strict:
            raise ValueError("lighting agents/slots must be a sequence")
        agents_raw = []
    agents = tuple(
        normalize_light(agents_raw[index] if index < len(agents_raw) else {}, phase=phase, strict=strict)
        for index in range(6)
    )
    return LightingState(
        agents=agents,
        keys=normalize_light(keys_raw, phase=phase, strict=strict),
        ambient=normalize_light(ambient_raw, phase=phase, strict=strict),
    )


def normalize_bindings(config: Any = None) -> tuple[KeyBinding, ...]:
    """Read the WebUI ``buttons`` schema, filling omitted entries safely."""

    buttons: Any = config.get("buttons") if _is_mapping(config) else None
    if buttons is None:
        buttons = []
    if isinstance(buttons, (str, bytes, bytearray)) or not isinstance(buttons, Sequence):
        raise ValueError("config.buttons must be a sequence")
    result = []
    for index in range(KEY_COUNT):
        raw = buttons[index] if index < len(buttons) and _is_mapping(buttons[index]) else {}
        default_target = DEFAULT_TARGETS[index]
        target = _first(raw, "targetKey", "target_key", default=default_target)
        if target is not None:
            target = str(target)
        label = str(_first(raw, "label", default=f"N4 按键 {index + 1}"))
        enabled = bool(_first(raw, "enabled", default=(index < len(DEFAULT_TARGETS) - 1)))
        result.append(KeyBinding(index=index + 1, label=label, target_key=target, enabled=enabled))
    return tuple(result)


def _target_light(binding: KeyBinding, lighting: LightingState) -> tuple[LightState, str]:
    target = binding.target_key
    if not binding.enabled or not isinstance(target, str):
        return LightState(), "none"
    if len(target) == 4 and target.startswith("AG") and target[2:].isdigit():
        slot = int(target[2:])
        if 0 <= slot < len(lighting.agents):
            return lighting.agents[slot], "thstatus"
    if target.startswith("ACT") and target[3:].isdigit() and 6 <= int(target[3:]) <= 12:
        return lighting.keys, "rgbcfg.keys"
    return LightState(), "none"


def _mix(first: tuple[int, int, int], second: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
    ratio = _clamp(amount, 0.0, 1.0)
    return tuple(round(a + (b - a) * ratio) for a, b in zip(first, second))  # type: ignore[return-value]


def _scale(rgb: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
    ratio = _clamp(amount, 0.0, 1.0)
    return tuple(round(channel * ratio) for channel in rgb)  # type: ignore[return-value]


def _short_label(value: Any, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)] + "…"


def _font_candidates() -> tuple[Path, ...]:
    root = Path(__file__).resolve().parents[1]
    candidates = [
        root / "upstream" / "openmicrokbd" / "app" / "resources" / "NotoSansSC.ttf",
    ]
    windir = os.environ.get("WINDIR")
    if windir:
        candidates.extend([Path(windir) / "Fonts" / "msyh.ttc", Path(windir) / "Fonts" / "segoeui.ttf"])
    candidates.extend([
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
    ])
    return tuple(candidates)


def _resolve_font(font_path: Optional[os.PathLike[str] | str]) -> Optional[str]:
    if font_path is not None:
        path = Path(font_path)
        if not path.exists():
            raise FileNotFoundError(f"font file not found: {path}")
        return str(path)
    for path in _font_candidates():
        if path.exists():
            return str(path)
    return None


@lru_cache(maxsize=64)
def _load_font(path: Optional[str], size: int) -> Any:
    _require_pillow()
    if path:
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            pass
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size=size)
    except OSError:
        return ImageFont.load_default()

@lru_cache(maxsize=32)
def _load_theme_font(path: Optional[str], size: int, numeric: bool = False) -> Any:
    number_path = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts' / 'bahnschrift.ttf'
    selected = str(number_path) if numeric and number_path.exists() else path
    try:
        font = ImageFont.truetype(selected, size=size)
        try:
            axes = font.get_variation_axes()
            values = [min(axis['maximum'], max(axis['minimum'], 700 if axis['name'] == b'Weight' else 75 if numeric and axis['name'] == b'Width' else axis['default'])) for axis in axes]
            font.set_variation_by_axes(values)
        except (OSError, AttributeError):
            pass
        return font
    except (OSError, TypeError):
        return _load_font(path, size)


def _draw_text(draw: Any, xy: tuple[float, float], text: Any, font: Any, fill: tuple[int, int, int], *, anchor: str = "mm") -> None:
    value = str(text)
    try:
        draw.text(xy, value, font=font, fill=fill, anchor=anchor)
    except (UnicodeEncodeError, ValueError):
        # A minimal Pillow install may lack a CJK font.  Preserve rendering
        # rather than failing an output update; ASCII labels remain useful.
        fallback = value.encode("ascii", "replace").decode("ascii")
        draw.text(xy, fallback, font=font, fill=fill, anchor=anchor)


def _gradient_background(image: Any, top: tuple[int, int, int], bottom: tuple[int, int, int]) -> None:
    draw = ImageDraw.Draw(image)
    height = max(1, image.height - 1)
    for y in range(image.height):
        draw.line((0, y, image.width, y), fill=_mix(top, bottom, y / height))


def _light_fill(light: LightState, base: tuple[int, int, int] = SURFACE) -> tuple[int, int, int]:
    # Keep a readable dark surface while reflecting colour and sampled effect.
    return _mix(base, light.rgb, 0.20 + 0.65 * light.intensity if light.intensity > 0 else 0.0)


def _render_key_image(
    binding: KeyBinding,
    light: LightState,
    source: str,
    *,
    font_path: Optional[str],
    theme: str = 'debug',
    phase: float = 0.5,
) -> Any:
    _require_pillow()
    if theme != 'debug':
        from n4_themes import render_key
        return render_key(binding, light, theme, lambda size, numeric=False: _load_theme_font(font_path, size, numeric),phase=phase)
    width, height = SECONDARY_KEY_SIZE if binding.index > MAIN_KEY_COUNT else MAIN_KEY_SIZE
    image = Image.new("RGB", (width, height), BASE_BACKGROUND)
    draw = ImageDraw.Draw(image)
    enabled = binding.enabled and binding.target_key is not None
    rgb = light.rgb
    intensity = light.intensity if enabled else 0.0
    border = _mix(DISABLED, rgb, max(0.24, intensity)) if enabled else DISABLED
    surface = _light_fill(light) if enabled else BASE_BACKGROUND
    draw.rounded_rectangle((0, 0, width - 1, height - 1), radius=10, fill=surface, outline=border, width=2)
    if enabled and intensity > 0:
        glow = _mix(surface, rgb, min(0.7, 0.2 + intensity * 0.5))
        draw.rounded_rectangle((3, 3, width - 4, height - 4), radius=8, fill=glow, outline=None)
    accent = _mix(DISABLED, rgb, max(0.20, intensity)) if enabled else DISABLED
    draw.rounded_rectangle((6, 6, width - 7, 12), radius=3, fill=accent)
    if enabled and light.e == 2:  # snake: a static diagonal trace
        draw.line((8, height - 16, width - 8, 16), fill=_mix(FOREGROUND, rgb, 0.3), width=5)
    if enabled and light.e == 3:  # rainbow: compact deterministic stripes
        rainbow = ((255, 77, 109), (255, 209, 102), (6, 214, 160), (77, 171, 247), (199, 125, 255))
        stripe_width = max(1, width // len(rainbow))
        for index, stripe in enumerate(rainbow):
            draw.rectangle((index * stripe_width, 14, (index + 1) * stripe_width, 19), fill=stripe)
    # A Micro LED may legitimately be full white/yellow. Do not put pale
    # glyphs directly on that glow: the N4 key lens makes them disappear.
    # Keep a constant opaque dark label area; colour/effects remain visible
    # on the surrounding frame and top strip without dimming the text.
    draw.rounded_rectangle((8, 28, width - 9, height - 6), radius=7, fill=SURFACE)
    target = binding.target_key or "未映射"
    label = _short_label(binding.label or target, 23 if width >= 176 else 14)
    title_font = _load_font(font_path, 26 if width >= 176 else 22)
    label_font = _load_font(font_path, 15 if width >= 176 else 12)
    small_font = _load_font(font_path, 12 if width >= 176 else 10)
    text_colour = FOREGROUND if enabled else MUTED
    _draw_text(draw, (width / 2, height * 0.53), target, title_font, text_colour)
    _draw_text(draw, (width / 2, height * 0.70), label, label_font, text_colour)
    subtitle = f"{light.effect_name} · {round(light.b * 100)}%" if enabled else "disabled"
    _draw_text(draw, (width / 2, height * 0.87), subtitle, small_font, MUTED)
    return image


def _render_screen_image(
    bindings: tuple[KeyBinding, ...],
    lighting: LightingState,
    key_images: tuple[Any, ...],
    key_sources: tuple[str, ...],
    *,
    font_path: Optional[str],
    phase: float,
    title: str,
) -> Any:
    _require_pillow()
    ambient = lighting.ambient
    if ambient.e == 3:  # rainbow ambient preview
        image = Image.new("RGB", SCREEN_SIZE, BASE_BACKGROUND)
        rainbow = ((44, 25, 58), (45, 47, 73), (22, 73, 72), (23, 55, 86), (63, 36, 73))
        for index in range(len(rainbow) - 1):
            top = round(index * image.height / (len(rainbow) - 1))
            bottom = round((index + 1) * image.height / (len(rainbow) - 1))
            segment = Image.new("RGB", (image.width, max(1, bottom - top)), rainbow[index])
            _gradient_background(segment, rainbow[index], rainbow[index + 1])
            image.paste(segment, (0, top))
        image = Image.blend(Image.new("RGB", SCREEN_SIZE, BASE_BACKGROUND), image, max(.12, ambient.intensity))
    elif ambient.e == 5:
        image = Image.new("RGB", SCREEN_SIZE, BASE_BACKGROUND)
        _gradient_background(image, _mix(BASE_BACKGROUND, ambient.rgb, max(.12, ambient.intensity)), BASE_BACKGROUND)
    else:
        background = _mix(BASE_BACKGROUND, ambient.rgb, max(.12, ambient.intensity) if ambient.intensity else 0.0)
        image = Image.new("RGB", SCREEN_SIZE, background)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((14, 14, SCREEN_SIZE[0] - 15, 46), radius=8, fill=SURFACE, outline=BORDER, width=1)
    title_font = _load_font(font_path, 18)
    small_font = _load_font(font_path, 12)
    _draw_text(draw, (30, 30), title, title_font, FOREGROUND, anchor="lm")
    enabled_count = sum(1 for binding in bindings if binding.enabled and binding.target_key)
    _draw_text(draw, (770, 30), f"{enabled_count}/14 keys · ambient {ambient.effect_name}", small_font, MUTED, anchor="rm")

    main_total_width = MAIN_KEY_COUNT // 2 * MAIN_KEY_SIZE[0] + 4 * 8
    origin_x = round((SCREEN_SIZE[0] - main_total_width) / 2)
    for index, image_key in enumerate(key_images[:MAIN_KEY_COUNT]):
        x = origin_x + (index % 5) * (MAIN_KEY_SIZE[0] + 8)
        y = 56 + (index // 5) * (MAIN_KEY_SIZE[1] + 8)
        image.paste(image_key, (x, y))
    for index, image_key in enumerate(key_images[MAIN_KEY_COUNT:]):
        x = 48 + index * SECONDARY_KEY_SIZE[0]
        image.paste(image_key, (x, 294))

    draw.rounded_rectangle((42, 420, 758, 468), radius=12, fill=SURFACE, outline=BORDER, width=1)
    _draw_text(draw, (58, 431), "N4 encoder mapping", small_font, MUTED, anchor="lm")
    knob_font = _load_font(font_path, 10)
    for index in range(4):
        x = 110 + index * 195
        y = 443
        draw.ellipse((x - 16, y - 16, x + 16, y + 16), fill=SURFACE_ALT, outline=BORDER, width=1)
        draw.line((x, y - 12, x, y - 4), fill=FOREGROUND, width=3)
        knob = f"旋钮 {index + 1}"
        if index < len(bindings):
            knob = f"旋钮 {index + 1}"
        _draw_text(draw, (x, 468), knob, knob_font, MUTED)
    return image


class N4VisualRenderer:
    """Stateless renderer configured for one N4 mapping profile."""

    def __init__(
        self,
        config: Any = None,
        *,
        font_path: Optional[os.PathLike[str] | str] = None,
        strict: bool = False,
        jpeg_quality: int = DEFAULT_JPEG_QUALITY,
        title: str = "Codex Micro · Mirabox N4",
    ) -> None:
        _require_pillow()
        self.bindings = normalize_bindings(config)
        visual = config.get('visual', {}) if isinstance(config, Mapping) else {}
        self.theme = visual.get('theme', 'debug')
        self.strip_mode=visual.get('stripMode','buttons')
        self.strip_brightness=visual.get('stripBrightness')
        if self.strip_mode not in ('buttons','knobs'):raise ValueError('Invalid strip mode')
        if self.strip_brightness is not None and (type(self.strip_brightness) is not int or not 10<=self.strip_brightness<=100):raise ValueError('Invalid strip brightness')
        self.knobs=config.get('knobs',[]) if isinstance(config,Mapping) else []
        from n4_themes import validate_theme
        validate_theme(self.theme)
        self.font_path = _resolve_font(font_path)
        self.strict = bool(strict)
        quality = int(jpeg_quality)
        if not 1 <= quality <= 100:
            raise ValueError("jpeg_quality must be between 1 and 100")
        self.jpeg_quality = quality
        self.title = str(title)

    def render(self, source: Any = None, *, phase: float = 0.5, clock: Any = None) -> N4RenderBundle:
        """Render one endpoint/mapping snapshot into a bundle of PIL images."""

        phase_value = _clamp(_number(phase, 0.5), 0.0, 1.0)
        clock_value = normalize_clock(clock)
        lighting = normalize_lighting(source, phase=phase_value, strict=self.strict)
        key_lights = []
        key_sources = []
        key_images = []
        for binding in self.bindings:
            light, source_name = _target_light(binding, lighting)
            key_lights.append(light)
            key_sources.append(source_name)
            if self.strip_mode=='knobs' and binding.index>10:
                from n4_themes import render_info_strip
                index=binding.index-11
                raw_knob=self.knobs[index] if index<len(self.knobs) else {}
                knob=dict(raw_knob) if isinstance(raw_knob,Mapping) else {}
                touch_action=None
                if index==3 and binding.enabled and binding.target_key=='ACT06':
                    touch_action={'targetKey':'ACT06','eyebrow':'快捷','label':'加速','icon':'lightning','scope':'strip','layout':'stacked'}
                    knob['touchAction']=touch_action
                knob['panel']=_strip_panel(index,lighting,clock_value if index==0 else None,touch_action)
                key_images.append(render_info_strip(index,knob,self.strip_brightness,lambda size:_load_theme_font(self.font_path,size),clock=clock_value if index==0 else None))
            else:
                key_images.append(_render_key_image(binding, light, source_name, font_path=self.font_path, theme=self.theme,phase=phase_value))
        key_images_tuple = tuple(key_images)
        screen = _render_screen_image(
            self.bindings, lighting, key_images_tuple, tuple(key_sources),
            font_path=self.font_path, phase=phase_value, title=self.title,
        )
        return N4RenderBundle(
            screen=screen, keys=key_images_tuple, lighting=lighting,
            bindings=self.bindings, key_lights=tuple(key_lights),
            key_sources=tuple(key_sources), phase=phase_value,
        )

    def render_screen(self, source: Any = None, *, phase: float = 0.5, clock: Any = None) -> Any:
        """Convenience wrapper returning only the 800x480 image."""

        return self.render(source, phase=phase, clock=clock).screen

    def render_key(
        self,
        index: int,
        source: Any = None,
        *,
        phase: float = 0.5,
        target_override: Any = None,
        clock: Any = None,
    ) -> Any:
        """Convenience wrapper returning one 1-based key image.

        ``target_override`` is useful for compatibility adapters that render a
        single key with a transient mapping while retaining one configured
        renderer instance.  Pass ``None`` to use the configured target; to
        intentionally render an unmapped key use ``target_override=False``.
        """

        if not isinstance(index, int) or not 1 <= index <= KEY_COUNT:
            raise ValueError(f"key index must be between 1 and {KEY_COUNT}")
        phase_value = _clamp(_number(phase, 0.5), 0.0, 1.0)
        lighting = normalize_lighting(source, phase=phase_value, strict=self.strict)
        configured = self.bindings[index - 1]
        if target_override is None and self.strip_mode=='knobs' and index>10:
            from n4_themes import render_info_strip
            raw_knob=self.knobs[index-11] if index-11<len(self.knobs) else {}
            knob=dict(raw_knob) if isinstance(raw_knob,Mapping) else {}
            touch_action=None
            if index==14 and configured.enabled and configured.target_key=='ACT06':
                touch_action={'targetKey':'ACT06','eyebrow':'快捷','label':'加速','icon':'lightning','scope':'strip','layout':'stacked'}
                knob['touchAction']=touch_action
            clock_value=normalize_clock(clock) if index==11 else None
            knob['panel']=_strip_panel(index-11,lighting,clock_value,touch_action)
            return render_info_strip(index-11,knob,self.strip_brightness,lambda size:_load_theme_font(self.font_path,size),clock=clock_value)
        target = None if target_override is False else str(target_override)
        binding = configured if target_override is None else KeyBinding(
            index=configured.index,
            label=configured.label,
            target_key=target,
            enabled=bool(target),
        )
        light, source_name = _target_light(binding, lighting)
        return _render_key_image(binding, light, source_name, font_path=self.font_path, theme=self.theme,phase=phase_value)

    def render_jpegs(self, source: Any = None, *, phase: float = 0.5, clock: Any = None) -> N4JpegBundle:
        """Render and encode one snapshot without touching disk."""

        return self.render(source, phase=phase, clock=clock).to_jpegs(self.jpeg_quality)


def render_n4_bundle(
    source: Any = None,
    config: Any = None,
    *,
    phase: float = 0.5,
    clock: Any = None,
    font_path: Optional[os.PathLike[str] | str] = None,
    strict: bool = False,
) -> N4RenderBundle:
    """Pure convenience function equivalent to ``N4VisualRenderer(...).render``."""

    return N4VisualRenderer(config, font_path=font_path, strict=strict).render(source, phase=phase, clock=clock)


def render_screen_image(
    source: Any = None,
    config: Any = None,
    *,
    phase: float = 0.5,
    clock: Any = None,
    font_path: Optional[os.PathLike[str] | str] = None,
    strict: bool = False,
) -> Any:
    """Render only the 800x480 N4 screen image."""

    return render_n4_bundle(source, config, phase=phase, clock=clock, font_path=font_path, strict=strict).screen


def render_key_image(
    index: int,
    source: Any = None,
    config: Any = None,
    *,
    phase: float = 0.5,
    clock: Any = None,
    font_path: Optional[os.PathLike[str] | str] = None,
    strict: bool = False,
) -> Any:
    """Render one 1-based N4 key image."""

    return N4VisualRenderer(config, font_path=font_path, strict=strict).render_key(index, source, phase=phase, clock=clock)


def render(
    source: Any = None,
    config: Any = None,
    *,
    phase: float = 0.5,
    clock: Any = None,
    font_path: Optional[os.PathLike[str] | str] = None,
    strict: bool = False,
) -> N4RenderBundle:
    """Short alias for :func:`render_n4_bundle` used by live adapters."""

    return render_n4_bundle(source, config, phase=phase, clock=clock, font_path=font_path, strict=strict)


def _jpeg_bytes(image: Any, quality: int = DEFAULT_JPEG_QUALITY) -> bytes:
    _require_pillow()
    quality = int(quality)
    if not 1 <= quality <= 100:
        raise ValueError("quality must be between 1 and 100")
    output = BytesIO()
    image.convert("RGB").save(output, format="JPEG", quality=quality, optimize=False, progressive=False)
    return output.getvalue()


__all__ = [
    "SCREEN_SIZE", "MAIN_KEY_SIZE", "SECONDARY_KEY_SIZE", "KEY_COUNT",
    "MAIN_KEY_COUNT", "SECONDARY_KEY_COUNT", "DEFAULT_JPEG_QUALITY",
    "EFFECT_NAMES", "normalize_color", "color_rgb", "color_hex", "normalize_clock",
    "normalize_effect", "effect_name", "animation_intensity",
    "LightState", "LightingState", "KeyBinding", "N4JpegBundle",
    "N4PathBundle", "N4RenderBundle", "normalize_light",
    "normalize_lighting", "normalize_bindings", "N4VisualRenderer",
    "render_n4_bundle", "render_screen_image", "render_key_image", "render",
]
