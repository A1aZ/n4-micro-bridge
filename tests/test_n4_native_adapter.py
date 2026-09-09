import io
import json
import pathlib
import subprocess
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from n4_native_adapter import (  # noqa: E402
    HttpReporter,
    JsonlSink,
    N4_BUTTON_PROFILES,
    N4_KNOB_PRESS,
    N4_KNOB_ROTATE,
    N4_SWIPE,
    N4NativeAdapter,
    SDK_WORKER_JOIN_TIMEOUT_SECONDS,
    ReportDispatcher,
    _external_owner_hint_result,
    _parse_tasklist_csv,
    decode_n4_report,
    detect_external_owner_hints,
    is_n4_input_packet,
    micro_events_for_decoded,
    normalize_n4_report,
    parse_hex_report,
    report_record,
)


def packet(code: int, state: int = 0, *, report_id: bool = False) -> bytes:
    body = bytearray(512)
    body[:7] = b"ACK\x00\x00OK"
    body[9] = code
    body[10] = state
    return (bytes([0]) + bytes(body)) if report_id else bytes(body)


class FakeN4Device:
    """Small SDK-shaped fake used to exercise adapter lifecycle/output locking."""

    vendor_id = 0x6602
    product_id = 0x1001
    path = "fake://n4"

    class _ThreadProbe:
        """Minimal ``threading.Thread``-shaped diagnostic stand-in."""

        def __init__(self, alive=False):
            self.alive = bool(alive)

        def is_alive(self):
            return self.alive

    def __init__(self):
        self.feature_option = SimpleNamespace(
            hasRGBLed=True,
            ledCounts=4,
            support_single_led_color=True,
        )
        self.opened = threading.Event()
        self.closed = threading.Event()
        self.callback = None
        self.raw_read_callback = None
        self.read_thread = None
        self.run_read_thread = False
        self.heartbeat_thread = None
        self.run_heartbeat_thread = False
        self.calls = []
        self._lock = threading.Lock()
        self._active = 0
        self.max_active = 0

    def _call(self, name, *args):
        with self._lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
            self.calls.append((name, *args))
        try:
            # Make concurrent callers overlap unless the adapter serializes
            # them; this catches accidental unlocked SDK writes.
            time.sleep(0.005)
            return 0
        finally:
            with self._lock:
                self._active -= 1

    def set_raw_read_callback(self, callback):
        self.callback = callback
        self.raw_read_callback = callback

    def open(self):
        self._call("open")
        self.run_read_thread = True
        self.read_thread = self._ThreadProbe(True)
        self.run_heartbeat_thread = True
        self.heartbeat_thread = self._ThreadProbe(True)
        self.opened.set()
        return True

    def init(self):
        return self._call("init")

    def close(self):
        self._call("close")
        self.run_read_thread = False
        self.run_heartbeat_thread = False
        if self.read_thread is not None:
            self.read_thread.alive = False
        if self.heartbeat_thread is not None:
            self.heartbeat_thread.alive = False
        self.closed.set()

    def set_touchscreen_image(self, path):
        return self._call("set_touchscreen_image", path)

    def set_key_image(self, key, path):
        return self._call("set_key_image", key, path)

    def set_seondscreen_image(self, key, path):
        return self._call("set_seondscreen_image", key, path)

    def refresh(self):
        return self._call("refresh")

    def set_brightness(self, percent):
        return self._call("set_brightness", percent)

    def set_led_brightness(self, value):
        return self._call("set_led_brightness", value)

    def set_led_color(self, r, g, b):
        return self._call("set_led_color", r, g, b)

    def set_single_led_color(self, colors):
        return self._call("set_single_led_color", colors)

    def reset_led_effect(self):
        return self._call("reset_led_effect")


class N4NativeAdapterTests(unittest.TestCase):
    def test_tasklist_parser_keeps_only_image_name_and_pid(self):
        output = (
            '"Image Name","PID","Session Name","Session#","Mem Usage"\n'
            '"StreamDock.exe","44188","Console","1","10,000 K"\n'
            '"CefViewWing.exe","35908","Console","1","20,000 K"\n'
            '"broken","not-a-pid","Console","1","0 K"\n'
        )
        self.assertEqual(
            _parse_tasklist_csv(output),
            [
                {"name": "StreamDock.exe", "pid": 44188},
                {"name": "CefViewWing.exe", "pid": 35908},
            ],
        )

    def test_external_owner_hint_is_read_only_and_does_not_claim_handle(self):
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    '"StreamDock.exe","44188","Console","1","10,000 K"\n'
                    '"CefViewWing.exe","35908","Console","1","20,000 K"\n'
                ),
                stderr="",
            )

        value = _external_owner_hint_result(system_name="Windows", runner=runner)
        self.assertTrue(value["available"])
        self.assertEqual(value["status"], "detected")
        self.assertTrue(value["suspected"])
        self.assertEqual([item["pid"] for item in value["processes"]], [44188, 35908])
        self.assertEqual(value["confidence"], "process-presence-only")
        self.assertIn("句柄", value["message"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], ["tasklist.exe", "/FO", "CSV", "/NH"])
        self.assertFalse(calls[0][1]["check"])

    def test_external_owner_hint_handles_non_windows_without_subprocess(self):
        def fail_runner(*_args, **_kwargs):
            raise AssertionError("non-Windows check must not execute tasklist")

        value = _external_owner_hint_result(system_name="Linux", runner=fail_runner)
        self.assertFalse(value["available"])
        self.assertEqual(value["status"], "unsupported")
        self.assertFalse(value["suspected"])

    def test_external_owner_hint_public_result_has_stable_contract(self):
        # The public helper is best-effort and environment-dependent.  Assert
        # only stable contract fields so this works with or without StreamDock.
        value = detect_external_owner_hints(force=True)
        self.assertIn(
            value["status"],
            {"detected", "related-process", "none", "error", "unsupported"},
        )
        self.assertEqual(value["confidence"], "process-presence-only")
        self.assertIsInstance(value["processes"], list)

    def test_ordered_open_cleanup_waits_for_sdk_sleeping_workers(self):
        class Worker:
            def __init__(self):
                self.alive = True
                self.join_timeouts = []

            def is_alive(self):
                return self.alive

            def join(self, timeout=None):
                self.join_timeouts.append(timeout)
                self.alive = False

        class Device:
            run_read_thread = True
            run_heartbeat_thread = True

            def __init__(self):
                self.read_thread = Worker()
                self.heartbeat_thread = Worker()

        device = Device()
        N4NativeAdapter._stop_sdk_workers(device)
        self.assertFalse(device.run_read_thread)
        self.assertFalse(device.run_heartbeat_thread)
        self.assertEqual(
            device.heartbeat_thread.join_timeouts,
            [SDK_WORKER_JOIN_TIMEOUT_SECONDS],
        )
        self.assertEqual(
            device.read_thread.join_timeouts,
            [SDK_WORKER_JOIN_TIMEOUT_SECONDS],
        )

    def test_all_four_knob_directions(self):
        expected = {
            0xA0: (1, "ccw", "ENC_CC"),
            0xA1: (1, "cw", "ENC_CW"),
            0x50: (2, "ccw", "ENC_CC"),
            0x51: (2, "cw", "ENC_CW"),
            0x90: (3, "ccw", "ENC_CC"),
            0x91: (3, "cw", "ENC_CW"),
            0x70: (4, "ccw", "ENC_CC"),
            0x71: (4, "cw", "ENC_CW"),
        }
        self.assertEqual(dict(N4_KNOB_ROTATE), expected)
        for code, (knob, direction, key) in expected.items():
            event = decode_n4_report(packet(code), require_ack=True)
            self.assertIsNotNone(event)
            self.assertEqual(event["kind"], "knob_rotate")
            self.assertEqual((event["knob"], event["direction"], event["microKey"]), (knob, direction, key))
            self.assertEqual(event["act"], 2)

    def test_knob_press_one_shot_and_state_normalization(self):
        self.assertEqual(set(N4_KNOB_PRESS), {0x37, 0x35, 0x33, 0x36})
        pressed = decode_n4_report(packet(0x37, 0x01), require_ack=True)
        released = decode_n4_report(packet(0x37, 0x00), require_ack=True)
        self.assertEqual((pressed["kind"], pressed["knob"], pressed["act"], pressed["oneShot"]), ("knob_press", 1, 1, True))
        self.assertEqual((released["act"], released["oneShot"]), (0, False))
        preview = micro_events_for_decoded(pressed, synthesize_release=True, release_ms=125)
        self.assertEqual([item["params"]["act"] for item in preview], [1, 0])
        self.assertTrue(preview[1]["synthetic"])

    def test_secondary_screen_swipes_are_diagnostic_only(self):
        self.assertEqual(dict(N4_SWIPE), {0x38: "left", 0x39: "right"})
        for code, direction in N4_SWIPE.items():
            decoded = decode_n4_report(packet(code, 0), require_ack=True)
            self.assertEqual(decoded["kind"], "swipe")
            self.assertEqual(decoded["direction"], direction)
            self.assertIsNone(decoded["microKey"])
            self.assertFalse(decoded["microSupported"])
            self.assertEqual(micro_events_for_decoded(decoded), [])

    def test_injected_device_outputs_are_lifecycle_bound_and_serialized(self):
        fake = FakeN4Device()
        adapter = N4NativeAdapter(
            ReportDispatcher(on_record=lambda _record: None),
            device=fake,
            initialize=False,
            status_stream=None,
        )
        initial_snapshot = adapter.snapshot()
        self.assertFalse(initial_snapshot["opened"])
        self.assertFalse(initial_snapshot["ready"])
        self.assertEqual(initial_snapshot["vendorId"], 0x6602)
        self.assertEqual(initial_snapshot["productId"], 0x1001)
        self.assertEqual(
            initial_snapshot["reader"],
            {"threadAlive": False, "runFlag": False, "rawCallbackRegistered": False},
        )
        self.assertEqual(
            initial_snapshot["heartbeat"],
            {"threadAlive": False, "runFlag": False},
        )
        with self.assertRaises(RuntimeError):
            adapter.refresh()

        runner = threading.Thread(target=lambda: adapter.run(duration=2), daemon=True)
        runner.start()
        self.assertTrue(fake.opened.wait(1), "fake device was not opened")
        self.assertTrue(adapter.wait_until_ready(1))
        self.assertTrue(adapter.is_open)
        self.assertTrue(adapter.snapshot()["opened"])
        self.assertTrue(adapter.snapshot()["ready"])
        self.assertEqual(
            adapter.snapshot()["reader"],
            {"threadAlive": True, "runFlag": True, "rawCallbackRegistered": True},
        )
        self.assertEqual(
            adapter.snapshot()["heartbeat"],
            {"threadAlive": True, "runFlag": True},
        )

        operations = [
            lambda: adapter.set_touchscreen_image("screen.jpg"),
            lambda: adapter.set_main_key_image(1, "main.jpg"),
            lambda: adapter.set_second_screen_image(2, "secondary.jpg"),
            lambda: adapter.set_brightness(55),
            lambda: adapter.set_led_brightness(40),
            lambda: adapter.set_led_color(1, 2, 3),
            lambda: adapter.set_single_led_color([(4, 5, 6)]),
            lambda: adapter.refresh(),
        ]
        barrier = threading.Barrier(len(operations) + 1)
        errors = []

        def invoke(operation):
            try:
                barrier.wait(timeout=1)
                operation()
            except Exception as error:  # pragma: no cover - assertion below reports it
                errors.append(error)

        workers = [threading.Thread(target=invoke, args=(operation,)) for operation in operations]
        for worker in workers:
            worker.start()
        barrier.wait(timeout=1)
        for worker in workers:
            worker.join(timeout=2)
        self.assertFalse(errors, errors)
        self.assertEqual(fake.max_active, 1)
        self.assertIn(("set_touchscreen_image", "screen.jpg"), fake.calls)
        self.assertIn(("set_key_image", 1, "main.jpg"), fake.calls)
        self.assertIn(("set_seondscreen_image", 12, "secondary.jpg"), fake.calls)
        self.assertIn(("set_brightness", 55), fake.calls)
        self.assertIn(("set_led_color", 1, 2, 3), fake.calls)
        self.assertIn(("set_single_led_color", [(4, 5, 6)]), fake.calls)

        adapter.stop()
        runner.join(timeout=3)
        self.assertFalse(runner.is_alive())
        self.assertTrue(fake.closed.is_set())
        self.assertFalse(adapter.is_open)
        self.assertFalse(adapter.snapshot()["opened"])
        self.assertEqual(
            adapter.snapshot()["reader"],
            {"threadAlive": False, "runFlag": False, "rawCallbackRegistered": False},
        )
        self.assertEqual(
            adapter.snapshot()["heartbeat"],
            {"threadAlive": False, "runFlag": False},
        )
        with self.assertRaises(RuntimeError):
            adapter.set_touchscreen_image("after-close.jpg")

    def test_rgb_capability_false_is_explicit_and_does_not_claim_inherited_methods(self):
        """Ordinary N4's inherited SDK RGB no-ops must not look successful."""

        fake = FakeN4Device()
        # StreamDockN4 leaves the common RGB methods present, but its feature
        # flags explicitly say that there are no physical LEDs.
        fake.feature_option = SimpleNamespace(
            hasRGBLed=False,
            ledCounts=4,  # deliberately inconsistent; adapter must normalize to 0
            support_single_led_color=True,
        )
        adapter = N4NativeAdapter(
            ReportDispatcher(on_record=lambda _record: None),
            device=fake,
            initialize=False,
            status_stream=None,
        )
        runner = threading.Thread(target=lambda: adapter.run(duration=2), daemon=True)
        runner.start()
        self.assertTrue(fake.opened.wait(1), "fake device was not opened")
        self.assertTrue(adapter.wait_until_ready(1))
        try:
            capabilities = adapter.lighting_capabilities()
            self.assertTrue(capabilities["open"])
            self.assertFalse(capabilities["hasRGBLed"])
            self.assertEqual(capabilities["ledCounts"], 0)
            self.assertEqual(capabilities["methods"], [])

            # RGB capability gating must not affect the normal screen/key
            # output path used by N4VisualOutput.
            self.assertEqual(adapter.set_touchscreen_image("screen.jpg"), 0)
            self.assertIn(("set_touchscreen_image", "screen.jpg"), fake.calls)

            operations = (
                lambda: adapter.set_led_brightness(40),
                lambda: adapter.set_led_color(1, 2, 3),
                lambda: adapter.set_single_led_color([(4, 5, 6)]),
                lambda: adapter.reset_led_effect(),
            )
            for operation in operations:
                with self.assertRaisesRegex(NotImplementedError, "RGB"):
                    operation()

            # No silent SDK no-op should have been invoked.
            self.assertFalse(
                any(
                    call[0]
                    in {
                        "set_led_brightness",
                        "set_led_color",
                        "set_single_led_color",
                        "reset_led_effect",
                    }
                    for call in fake.calls
                )
            )
        finally:
            adapter.stop()
            runner.join(timeout=3)
            self.assertFalse(runner.is_alive())

    def test_per_led_rgb_requires_feature_flag_but_common_rgb_remains_compatible(self):
        fake = FakeN4Device()
        fake.feature_option = SimpleNamespace(
            hasRGBLed=True,
            ledCounts=4,
            support_single_led_color=False,
        )
        adapter = N4NativeAdapter(
            ReportDispatcher(on_record=lambda _record: None),
            device=fake,
            initialize=False,
            status_stream=None,
        )
        runner = threading.Thread(target=lambda: adapter.run(duration=2), daemon=True)
        runner.start()
        self.assertTrue(fake.opened.wait(1), "fake device was not opened")
        self.assertTrue(adapter.wait_until_ready(1))
        try:
            capabilities = adapter.lighting_capabilities()
            self.assertTrue(capabilities["hasRGBLed"])
            self.assertEqual(
                capabilities["methods"],
                ["set_led_brightness", "set_led_color", "reset_led_effect"],
            )
            self.assertEqual(adapter.set_led_color(1, 2, 3), 0)
            with self.assertRaisesRegex(NotImplementedError, "per-LED"):
                adapter.set_single_led_color([(4, 5, 6)])
            self.assertIn(("set_led_color", 1, 2, 3), fake.calls)
            self.assertNotIn(("set_single_led_color", [(4, 5, 6)]), fake.calls)
        finally:
            adapter.stop()
            runner.join(timeout=3)
            self.assertFalse(runner.is_alive())

    def test_button_and_unknown(self):
        button = decode_n4_report(packet(0x06, 1), require_ack=True)
        self.assertEqual((button["kind"], button["key"], button["microKey"], button["act"]), ("button", 1, "AG00", 1))
        unassigned = decode_n4_report(packet(0x43, 1), require_ack=True)
        self.assertEqual((unassigned["kind"], unassigned["key"], unassigned["microKey"]), ("button", 14, None))
        legacy = decode_n4_report(packet(11, 1), require_ack=True, input_profile="python")
        self.assertEqual((legacy["kind"], legacy["key"], legacy["microKey"]), ("button", 1, "AG00"))
        self.assertEqual(N4_BUTTON_PROFILES["cpp"], (6, 7, 8, 9, 10, 1, 2, 3, 4, 5, 0x40, 0x41, 0x42, 0x43))
        unknown = decode_n4_report(packet(0xEE), require_ack=True)
        self.assertEqual(unknown["kind"], "unknown")
        self.assertIsNone(decode_n4_report(b"\x00" * 10, require_ack=True))

    def test_report_id_normalization_and_hex(self):
        raw = packet(0xA1, report_id=True)
        self.assertTrue(is_n4_input_packet(raw))
        normalized, report_id = normalize_n4_report(raw)
        self.assertEqual(report_id, 0)
        self.assertEqual(normalized, raw[1:])
        self.assertEqual(parse_hex_report("41:43:4b 00 00 4f 4b"), b"ACK\x00\x00OK")
        record = report_record(raw, timestamp="2026-09-07T00:00:00.000Z")
        self.assertEqual(record["length"], 513)
        self.assertEqual(record["reportId"], 0)
        self.assertIn("normalizedBytes", record)

    def test_dispatcher_filters_status_and_writes_jsonl(self):
        output = io.StringIO()
        dispatcher = ReportDispatcher(jsonl=JsonlSink(output))
        self.assertIsNone(dispatcher.handle(b"\x00" * 32))
        record = dispatcher.handle(packet(0xA0))
        self.assertEqual(record["decoded"]["direction"], "ccw")
        parsed = json.loads(output.getvalue())
        self.assertEqual(parsed["decoded"]["microKey"], "ENC_CC")
        self.assertEqual(dispatcher.reports_seen, 2)
        self.assertEqual(dispatcher.reports_ignored, 1)
        self.assertEqual(dispatcher.reports_forwarded, 1)
        self.assertEqual(dispatcher.snapshot()["reportsSeen"], 2)
        self.assertEqual(dispatcher.snapshot()["reportsForwarded"], 1)

        all_output = io.StringIO()
        all_dispatcher = ReportDispatcher(
            jsonl=JsonlSink(all_output), include_non_input=True
        )
        status = all_dispatcher.handle(bytes([0x00] * 32))
        self.assertFalse(status["inputPacket"])
        self.assertIsNone(status["decoded"])

    def test_http_reporter_posts_normalized_array(self):
        received = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers["content-length"])
                received["body"] = json.loads(self.rfile.read(length))
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true,"events":[]}')

            def log_message(self, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/api/bridge/report"
            result = HttpReporter(url).post(packet(0xA1, report_id=True))
            self.assertTrue(result["ok"])
            self.assertEqual(received["body"]["reportId"], 0)
            self.assertEqual(received["body"]["bytes"][9], 0xA1)
            self.assertEqual(len(received["body"]["bytes"]), 512)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_cli_dry_run_without_sdk(self):
        command = [
            sys.executable,
            str(ROOT / "scripts" / "n4-native-adapter.py"),
            "--dry-run",
            "--quiet",
            "--hex",
            "41 43 4b 00 00 4f 4b 00 00 a1 00",
        ]
        completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=True)
        line = json.loads(completed.stdout.strip())
        self.assertEqual(line["decoded"]["microKey"], "ENC_CW")
        self.assertEqual(completed.stderr, "")

    def test_cli_help_and_dry_run_expose_optional_hidapi_fallback(self):
        help_result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "n4-native-adapter.py"), "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("--hidapi", help_result.stdout)
        self.assertIn("--hidapi-probe", help_result.stdout)
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "n4-native-adapter.py"),
                "--dry-run",
                "--hidapi",
                "--quiet",
                "--hex",
                "41 43 4b 00 00 4f 4b 00 00 a1 00",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(json.loads(completed.stdout)["decoded"]["microKey"], "ENC_CW")


if __name__ == "__main__":
    unittest.main()
