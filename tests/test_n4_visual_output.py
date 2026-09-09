import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from n4_visual_output import (  # noqa: E402
    DEFAULT_TARGETS,
    MAIN_KEY_SIZE,
    N4VisualOutput,
    SCREEN_SIZE,
    SECONDARY_KEY_SIZE,
)
from codexmicro_sideband import HostRpcEndpoint  # noqa: E402


class FakeN4:
    def __init__(self):
        self.keys = []
        self.screens = []
        self.refreshes = 0
        self.calls = []

    def set_key_image(self, key, path):
        self.calls.append(("key", key))
        self.keys.append((key, path))

    def set_touchscreen_image(self, path):
        self.calls.append(("background", path))
        self.screens.append(path)

    def refresh(self):
        self.refreshes += 1


class N4VisualOutputTests(unittest.TestCase):
    def test_dimensions_and_initial_upload(self):
        fake = FakeN4()
        renderer = N4VisualOutput(fake, refresh_ms=40, status_stream=None)
        try:
            self.assertEqual(DEFAULT_TARGETS[-1], None)
            self.assertEqual(renderer._render_key(0, "AG00", 0.5).size, MAIN_KEY_SIZE)
            self.assertEqual(renderer._render_key(10, "ACT06", 0.5).size, SECONDARY_KEY_SIZE)
            self.assertEqual(renderer._render_screen(0.5).size, SCREEN_SIZE)
            result = renderer.render_once(force=True)
            self.assertTrue(result["ok"])
            self.assertEqual(len(fake.keys), 14)
            self.assertEqual(len(fake.screens), 1)
            self.assertEqual(fake.refreshes, 1)
            self.assertEqual(fake.calls[0][0], "background")
            self.assertEqual([key for kind,key in fake.calls[1:]], list(range(1,15)))
            from PIL import Image
            with Image.open(fake.screens[0]) as background:
                self.assertEqual(background.size, SCREEN_SIZE)
                self.assertEqual(background.getextrema(), ((0,0),(0,0),(0,0)))
            self.assertTrue(all(pathlib.Path(path).exists() for _, path in fake.keys))
        finally:
            renderer.stop()

    def test_sticky_thstatus_rgbcfg_and_hash_dedup(self):
        fake = FakeN4()
        renderer = N4VisualOutput(fake, refresh_ms=40, status_stream=None)
        try:
            renderer.render_once(force=True)
            first_count = len(fake.keys)
            renderer.render_once()
            self.assertEqual(len(fake.keys), first_count)
            renderer.handle("v.oai.thstatus", [{"id": 2, "c": "#ff0088", "b": 1, "e": "solid"}])
            self.assertEqual(renderer.state["agents"][2]["c"], 0xFF0088)
            self.assertEqual(renderer.state["agents"][2]["b"], 1)
            renderer.handle("v.oai.rgbcfg", {"keys": {"c": 0x00FF00, "b": 0.5, "e": 1}})
            self.assertEqual(renderer.state["keys"]["c"], 0x00FF00)
            self.assertEqual(renderer.state["keys"]["b"], 0.5)
            result = renderer.render_once()
            self.assertTrue(result["ok"])
            self.assertGreater(len(fake.keys), first_count)
            # Live lighting must never re-upload the software dashboard or
            # clear all key regions when only one key's state has changed.
            self.assertEqual(len(fake.screens), 1)
        finally:
            renderer.stop()

    def test_upload_failure_is_retryable_and_does_not_raise(self):
        class Broken(FakeN4):
            def set_key_image(self, key, path):
                raise RuntimeError("N4 not open")

        renderer = N4VisualOutput(Broken(), refresh_ms=40, status_stream=None)
        try:
            result = renderer.render_once()
            self.assertFalse(result["ok"])
            self.assertTrue(renderer._dirty)
            self.assertIn("N4 not open", renderer.last_error)
        finally:
            renderer.stop()

    def test_sideband_host_rpc_is_wired_to_renderer_callback(self):
        fake = FakeN4()
        renderer = N4VisualOutput(fake, refresh_ms=40, status_stream=None)
        try:
            endpoint = HostRpcEndpoint(on_visual=renderer.handle)
            reply = endpoint.handle({
                "id": 7,
                "method": "v.oai.rgbcfg",
                "params": {"keys": {"c": "#123456", "b": 0.75, "e": "solid"}},
            })
            self.assertEqual(reply, {"id": 7, "result": {"ok": True}})
            self.assertEqual(renderer.state["keys"]["c"], 0x123456)
            self.assertEqual(renderer.state["keys"]["b"], 0.75)
            self.assertTrue(renderer._dirty)
        finally:
            renderer.stop()

    def test_lights_preview_updates_renderer_with_long_field_names(self):
        fake = FakeN4()
        renderer = N4VisualOutput(fake, refresh_ms=40, status_stream=None)
        try:
            endpoint = HostRpcEndpoint(on_visual=renderer.handle)
            reply = endpoint.handle({
                "id": 8,
                "method": "lights.preview",
                "params": {
                    "backlight": {"color": 0xFF0000, "brightness": 0.5, "effect": "breath", "magic": 0},
                    "underglow": {"color": 0x0000FF, "brightness": 1, "effect": "solid"},
                },
            })
            self.assertEqual(reply, {"id": 8, "result": {"ok": True}})
            self.assertEqual(renderer.state["keys"]["c"], 0xFF0000)
            self.assertEqual(renderer.state["keys"]["m"], 0.0)
            self.assertEqual(renderer.state["ambient"]["c"], 0x0000FF)
            self.assertTrue(renderer._dirty)
        finally:
            renderer.stop()

    def test_apply_snapshot_rebuilds_renderer_for_mapping_changes(self):
        fake = FakeN4()
        renderer = N4VisualOutput(fake, refresh_ms=40, status_stream=None)
        try:
            payload = {
                "lighting": {
                    "agents": [{"c": 0xFF0000, "b": 1, "e": 1}] + [{} for _ in range(5)],
                    "keys": {"c": 0x00FF00, "b": 1, "e": 1},
                    "ambient": {},
                },
                "model": {
                    "buttons": [
                        {"targetKey": "ACT06", "enabled": True},
                    ] + [{"targetKey": None, "enabled": False} for _ in range(13)],
                },
            }
            renderer.apply_snapshot(payload)
            self.assertEqual(renderer.targets[0], "ACT06")
            bundle = renderer._renderer.render(renderer.state, phase=0.5)
            self.assertEqual(bundle.key_sources[0], "rgbcfg.keys")
            self.assertEqual(bundle.bindings[0].target_key, "ACT06")
        finally:
            renderer.stop()

    def test_failed_refresh_does_not_commit_hashes_and_retries_full_batch(self):
        class RefreshFailsOnce(FakeN4):
            def __init__(self):
                super().__init__()
                self.fail_refresh = True

            def refresh(self):
                self.refreshes += 1
                if self.fail_refresh:
                    self.fail_refresh = False
                    raise RuntimeError("refresh failed")

        fake = RefreshFailsOnce()
        renderer = N4VisualOutput(fake, refresh_ms=40, status_stream=None)
        try:
            first = renderer.render_once(force=True)
            self.assertFalse(first["ok"])
            self.assertEqual(renderer._hashes, {})
            self.assertEqual(len(fake.keys), 14)
            self.assertEqual(len(fake.screens), 1)

            second = renderer.render_once()
            self.assertTrue(second["ok"])
            self.assertEqual(len(fake.keys), 28)
            self.assertEqual(len(fake.screens), 2)
            self.assertEqual(fake.refreshes, 2)
            self.assertEqual(len(renderer._hashes), 15)

            third = renderer.render_once()
            self.assertTrue(third["ok"])
            self.assertEqual(len(third["uploads"]), 0)
            self.assertEqual(fake.refreshes, 2)
        finally:
            renderer.stop()

    def test_stop_then_start_recreates_temp_directory_and_can_render_again(self):
        fake = FakeN4()
        renderer = N4VisualOutput(fake, refresh_ms=40, status_stream=None)
        first_directory = pathlib.Path(renderer._tmpdir.name)
        renderer.stop()
        self.assertFalse(first_directory.exists())
        try:
            renderer.start()
            second_directory = pathlib.Path(renderer._tmpdir.name)
            self.assertTrue(second_directory.exists())
            self.assertNotEqual(first_directory, second_directory)
            self.assertTrue(renderer.flush(timeout=3))
        finally:
            renderer.stop()

    def test_forced_background_clear_invalidates_unchanged_keys_even_on_failure(self):
        class FailAfterClear(FakeN4):
            fail = False
            def set_key_image(self, key, path):
                if self.fail:
                    self.fail = False
                    raise RuntimeError('upload interrupted after background clear')
                super().set_key_image(key,path)
        fake=FailAfterClear()
        renderer=N4VisualOutput(fake,status_stream=None)
        try:
            self.assertTrue(renderer.render_once()['ok'])
            fake.fail=True
            self.assertFalse(renderer.render_once(force=True)['ok'])
            self.assertFalse(any(key.startswith('key:') for key in renderer._hashes))
            count=len(fake.keys)
            self.assertTrue(renderer.render_once()['ok'])
            self.assertEqual(len(fake.keys)-count,14)
            self.assertEqual(len(renderer.render_once()['uploads']),0)
        finally:
            renderer.stop()


if __name__ == "__main__":
    unittest.main()
