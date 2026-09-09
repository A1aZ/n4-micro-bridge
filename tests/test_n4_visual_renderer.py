import io
import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

try:
    from PIL import Image
except ImportError:  # pragma: no cover - the renderer reports this clearly
    Image = None

from codexmicro_sideband import HostRpcEndpoint  # noqa: E402
import n4_visual_renderer as renderer_module  # noqa: E402
from n4_visual_renderer import (  # noqa: E402
    MAIN_KEY_SIZE,
    SCREEN_SIZE,
    SECONDARY_KEY_SIZE,
    N4VisualRenderer,
    color_hex,
    normalize_light,
    normalize_lighting,
    render_key_image,
)


@unittest.skipUnless(Image is not None, "Pillow is required for image renderer tests")
class N4VisualRendererTests(unittest.TestCase):
    def test_endpoint_and_mapping_render_exact_n4_sizes(self):
        endpoint = HostRpcEndpoint()
        endpoint.handle({
            "method": "v.oai.thstatus",
            "params": [{"id": 0, "c": 0xFF2200, "b": 1, "e": "solid"}],
        })
        endpoint.handle({
            "method": "v.oai.rgbcfg",
            "params": {"keys": {"c": 0x22CC88, "b": 0.5, "e": "breath"}},
        })
        bundle = N4VisualRenderer().render(endpoint, phase=0.5)

        self.assertEqual(bundle.screen.size, SCREEN_SIZE)
        self.assertEqual(len(bundle.keys), 14)
        self.assertEqual([image.size for image in bundle.keys[:10]], [MAIN_KEY_SIZE] * 10)
        self.assertEqual([image.size for image in bundle.keys[10:]], [SECONDARY_KEY_SIZE] * 4)
        self.assertEqual(bundle.key_sources[0], "thstatus")
        self.assertEqual(bundle.key_lights[0].color_hex, "#FF2200")
        self.assertEqual(bundle.key_sources[6], "rgbcfg.keys")
        self.assertEqual(bundle.key_lights[6].color_hex, "#22CC88")
        self.assertEqual(bundle.metadata()["screen"], {
            "width": 800, "height": 480, "format": "JPEG", "rotation": 180,
        })
        self.assertEqual(bundle.metadata()["secondaryKey"]["width"], 176)

    def test_standard_lighting_mapping_and_custom_targets(self):
        lighting = {
            "lighting": {
                "agents": [{"c": 0x123456, "b": 1, "e": 1}],
                "keys": {"c": 0xABCDEF, "b": 0.75, "e": 1},
                "ambient": {"c": 0x010203, "b": 0.2, "e": "gradient"},
            }
        }
        state = normalize_lighting(lighting, phase=0.5)
        self.assertEqual(state.agents[0].color_hex, "#123456")
        self.assertEqual(state.keys.color_hex, "#ABCDEF")
        self.assertEqual(state.ambient.effect_name, "gradient")

        config = {"buttons": [{"targetKey": "ACT12", "enabled": True}]}
        bundle = N4VisualRenderer(config).render(lighting)
        self.assertEqual(bundle.key_sources[0], "rgbcfg.keys")
        self.assertEqual(bundle.key_lights[0].color_hex, "#ABCDEF")
        self.assertIsNone(bundle.bindings[13].target_key)
        self.assertEqual(bundle.bindings[13].enabled, False)
        self.assertEqual(color_hex("#abc"), "#AABBCC")

    def test_jpeg_bytes_roundtrip_and_saved_paths(self):
        bundle = N4VisualRenderer().render(phase=0.25)
        encoded = bundle.to_jpegs(quality=85)
        self.assertEqual(len(encoded.keys), 14)
        self.assertTrue(encoded.screen.startswith(b"\xff\xd8"))
        self.assertTrue(all(value.startswith(b"\xff\xd8") for value in encoded.keys))
        with Image.open(io.BytesIO(encoded.screen)) as screen:
            self.assertEqual(screen.size, SCREEN_SIZE)
            self.assertEqual(screen.format, "JPEG")
        with Image.open(io.BytesIO(encoded.keys[0])) as key:
            self.assertEqual(key.size, MAIN_KEY_SIZE)
        with Image.open(io.BytesIO(encoded.keys[10])) as key:
            self.assertEqual(key.size, SECONDARY_KEY_SIZE)

        with tempfile.TemporaryDirectory() as directory:
            paths = bundle.save_jpegs(directory, prefix="test")
            self.assertEqual(paths.screen.name, "test-screen.jpg")
            self.assertEqual(paths.keys[0].name, "test-key-01.jpg")
            self.assertEqual(len(paths.keys), 14)
            self.assertTrue(paths.screen.exists())
            self.assertTrue(all(path.exists() for path in paths.keys))

    def test_strict_validation_and_tolerant_live_values(self):
        self.assertEqual(normalize_light({"b": "bad"}).b, 0.0)
        with self.assertRaisesRegex(ValueError, "finite"):
            normalize_light({"b": "bad"}, strict=True)
        light = normalize_light({"c": 0xFF0000, "b": 2, "e": "breath"}, phase=0.5)
        self.assertEqual(light.b, 1.0)
        self.assertEqual(light.intensity, 1.0)
        self.assertEqual(render_key_image(11, phase=0.5).size, SECONDARY_KEY_SIZE)

    def test_bright_lights_keep_text_on_a_dark_opaque_plate(self):
        for color in [0xFFFFFF,0xFFFF00,0x00FFFF]:
            for effect in [1,3,4,5]:
                with self.subTest(color=color,effect=effect):
                    bundle=N4VisualRenderer().render({'agents':[{'c':color,'b':1,'e':effect}],
                                                     'keys':{'c':color,'b':1,'e':effect}},phase=.5)
                    for key in [bundle.keys[0],bundle.keys[10]]:
                        self.assertEqual(key.getpixel((12,38)),renderer_module.SURFACE)
                        glyphs=key.crop((20,40,key.width-20,90))
                        self.assertGreater(sum(1 for y in range(glyphs.height) for x in range(glyphs.width)
                                               if min(glyphs.getpixel((x,y)))>170),20)
                        output=io.BytesIO();key.save(output,format='JPEG',quality=88)
                        with Image.open(io.BytesIO(output.getvalue())) as jpeg:
                            self.assertLess(max(jpeg.getpixel((12,38))),65)


class N4VisualRendererPillowErrorTests(unittest.TestCase):
    def test_missing_pillow_has_actionable_error(self):
        original = renderer_module.Image
        renderer_module.Image = None
        try:
            with self.assertRaisesRegex(RuntimeError, "Pillow"):
                N4VisualRenderer()
        finally:
            renderer_module.Image = original


if __name__ == "__main__":
    unittest.main()
