import pathlib
import sys
import time
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from n4_hidapi import (  # noqa: E402
    DIRECT_HIDAPI_TRANSPORT,
    DirectHidapiN4Reader,
    N4_READ_BUFFER_SIZE,
    N4HidapiUnavailable,
    probe_n4_hidapi,
)
from n4_native_adapter import ReportDispatcher  # noqa: E402


def packet(code: int, state: int = 0) -> bytes:
    body = bytearray(512)
    body[:7] = b"ACK\x00\x00OK"
    body[9] = code
    body[10] = state
    return bytes(body)


class FakeHidDevice:
    def __init__(self, reports=None, *, fail_open=False, fail_read=False):
        self.reports = list(reports or [])
        self.fail_open = fail_open
        self.fail_read = fail_read
        self.opened_path = None
        self.closed = False
        self.read_calls = []

    def open_path(self, path):
        if self.fail_open:
            raise OSError("fake open failure")
        self.opened_path = path

    def read(self, size, timeout_ms):
        self.read_calls.append((size, timeout_ms))
        if self.fail_read:
            raise OSError("fake read failure")
        if self.reports:
            return list(self.reports.pop(0))
        return []

    def close(self):
        self.closed = True


class FakeHidModule:
    def __init__(self, entries, *, reports=None, fail_open=False, fail_read=False):
        self.entries = list(entries)
        self.device_instance = FakeHidDevice(
            reports,
            fail_open=fail_open,
            fail_read=fail_read,
        )
        self.device_calls = 0

    def enumerate(self, vendor_id, product_id):
        return list(self.entries)

    def device(self):
        self.device_calls += 1
        return self.device_instance


def n4_entry(path=b"n4", *, usage_page=0xFFA0, usage=1):
    return {
        "path": path,
        "vendor_id": 0x6602,
        "product_id": 0x1001,
        "usage_page": usage_page,
        "usage": usage,
        "product_string": "HOTSPOTEKUSB HID DEMO",
    }


class N4HidapiTests(unittest.TestCase):
    def test_probe_is_read_only_and_filters_keyboard_collection(self):
        hid = FakeHidModule([
            n4_entry(b"n4-input"),
            n4_entry(b"keyboard", usage_page=1, usage=6),
        ])
        value = probe_n4_hidapi(hid_module=hid)
        self.assertTrue(value["ok"])
        self.assertEqual(value["status"], "compatible")
        self.assertEqual(value["transport"], DIRECT_HIDAPI_TRANSPORT)
        self.assertEqual(value["matchCount"], 1)
        self.assertEqual(value["path"], "n4-input")
        self.assertFalse(value["opened"])
        self.assertEqual(hid.device_calls, 0)

    def test_probe_requires_usage_metadata_unless_path_is_explicit(self):
        hid = FakeHidModule([n4_entry(b"metadata-less", usage_page=None, usage=None)])
        value = probe_n4_hidapi(hid_module=hid)
        self.assertEqual(value["status"], "usage-metadata-missing")
        self.assertFalse(value["ok"])
        explicit = probe_n4_hidapi(hid_module=hid, path=b"metadata-less")
        self.assertEqual(explicit["status"], "compatible")
        self.assertTrue(explicit["ok"])

    def test_reader_forwards_reports_and_closes_handle(self):
        hid = FakeHidModule(
            [n4_entry(b"n4-input")],
            reports=[packet(0xA1), packet(0x37, 1)],
        )
        records = []
        dispatcher = ReportDispatcher(on_record=records.append)
        reader = DirectHidapiN4Reader(
            dispatcher,
            hid_module=hid,
            read_timeout_ms=10,
            status_stream=None,
        )
        self.assertEqual(reader.run(duration=0.02), 0)
        self.assertEqual([item["decoded"]["microKey"] for item in records], ["ENC_CW", "ENC"])
        self.assertTrue(hid.device_instance.closed)
        self.assertEqual(hid.device_instance.opened_path, b"n4-input")
        self.assertTrue(hid.device_instance.read_calls)
        self.assertEqual(hid.device_instance.read_calls[0], (N4_READ_BUFFER_SIZE, 10))
        self.assertEqual(reader.snapshot()["transport"], DIRECT_HIDAPI_TRANSPORT)
        self.assertEqual(reader.snapshot()["openMode"], DIRECT_HIDAPI_TRANSPORT)
        self.assertEqual(reader.snapshot()["readBufferSize"], N4_READ_BUFFER_SIZE)
        self.assertEqual(reader.snapshot()["transportDetails"]["inputReportSize"], 512)
        self.assertFalse(reader.snapshot()["opened"])
        self.assertGreaterEqual(reader.snapshot()["reads"], 2)

    def test_reader_allows_explicit_receive_buffer_capacity(self):
        hid = FakeHidModule(
            [n4_entry(b"n4-input")],
            reports=[packet(0xA0)],
        )
        reader = DirectHidapiN4Reader(
            ReportDispatcher(on_record=lambda _record: None),
            hid_module=hid,
            read_timeout_ms=10,
            read_buffer_size=513,
            status_stream=None,
        )
        self.assertEqual(reader.run(duration=0.01), 0)
        self.assertEqual(hid.device_instance.read_calls[0], (513, 10))
        self.assertEqual(reader.snapshot()["readBufferSize"], 513)

    def test_reader_rejects_unverified_path_and_closes_failed_open(self):
        hid = FakeHidModule([n4_entry(b"n4-input")], fail_open=True)
        reader = DirectHidapiN4Reader(
            ReportDispatcher(on_record=lambda _record: None),
            hid_module=hid,
            device_path=b"n4-input",
            status_stream=None,
        )
        with self.assertRaisesRegex(Exception, "open_path"):
            reader.open()
        self.assertTrue(hid.device_instance.closed)

        metadata_less = FakeHidModule([n4_entry(b"metadata-less", usage_page=None, usage=None)])
        unverified = DirectHidapiN4Reader(
            ReportDispatcher(on_record=lambda _record: None),
            hid_module=metadata_less,
            status_stream=None,
        )
        with self.assertRaisesRegex(N4HidapiUnavailable, "metadata"):
            unverified.open()
        self.assertEqual(metadata_less.device_calls, 0)


if __name__ == "__main__":
    unittest.main()
