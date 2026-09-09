import pathlib
import re
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import codexmicro_companion as companion  # noqa: E402
from codexmicro_sideband import SidebandRuntime, encode_micro_message  # noqa: E402


def c_array_bytes(source: str, symbol: str) -> bytes:
    match = re.search(
        rf"\b{re.escape(symbol)}\s*\[\s*\]\s*=\s*\{{(.*?)\}}\s*;",
        source,
        re.S,
    )
    if not match:
        raise AssertionError(f"missing C byte array {symbol}")
    body = re.sub(r"/\*.*?\*/|//[^\r\n]*", "", match.group(1), flags=re.S)
    return bytes(int(value, 16) for value in re.findall(r"0x([0-9A-Fa-f]{1,2})", body))


class FakeDevice:
    def __init__(self):
        self.opened_path = None
        self.writes = []
        self.reads = []
        self.closed = False

    def open_path(self, path):
        self.opened_path = path

    def write(self, packet):
        self.writes.append(bytes(packet))
        return len(packet)

    def read(self, length, timeout_ms):
        self.last_read = (length, timeout_ms)
        if not self.reads:
            return []
        return self.reads.pop(0)

    def close(self):
        self.closed = True


class FailingOpenDevice(FakeDevice):
    def open_path(self, path):
        self.opened_path = path
        raise OSError("open failed")


class PartialWriteDevice(FakeDevice):
    def __init__(self, result):
        super().__init__()
        self.result = result

    def write(self, packet):
        self.writes.append(bytes(packet))
        return self.result


class FakeHid:
    def __init__(self):
        self.device_instance = FakeDevice()

    def enumerate(self, vendor_id, product_id):
        self.enumerated = (vendor_id, product_id)
        return [
            {
                "path": "wrong",
                "vendor_id": vendor_id,
                "product_id": product_id,
                "usage_page": "0xff00",
                "usage": 1,
            },
            {
                "path": "companion",
                "vendor_id": vendor_id,
                "product_id": product_id,
                "usage_page": "0xff70",
                "usage": "0x1",
            },
        ]

    def device(self):
        return self.device_instance


class CompanionProtocolTests(unittest.TestCase):
    def test_descriptor_shapes_and_identity_are_distinct(self):
        self.assertEqual(companion.MIRABOX_CODEX_MICRO_REPORT_DESCRIPTOR_LENGTH, 29)
        self.assertEqual(companion.MIRABOX_CODEX_COMPANION_REPORT_DESCRIPTOR_LENGTH, 29)
        self.assertEqual(companion.MIRABOX_CODEX_MICRO_DUAL_REPORT_DESCRIPTOR_LENGTH, 58)
        self.assertEqual(companion.MIRABOX_CODEX_MICRO_REPORT_DESCRIPTOR[:3], b"\x06\x00\xff")
        self.assertEqual(companion.MIRABOX_CODEX_COMPANION_REPORT_DESCRIPTOR[:3], b"\x06\x70\xff")
        self.assertEqual(companion.MIRABOX_CODEX_MICRO_REPORT_DESCRIPTOR[7:9], b"\x85\x06")
        self.assertEqual(companion.MIRABOX_CODEX_COMPANION_REPORT_DESCRIPTOR[7:9], b"\x85\x07")
        self.assertEqual(companion.DUAL_REPORT_DESCRIPTOR,
                         companion.MIRABOX_CODEX_MICRO_REPORT_DESCRIPTOR
                         + companion.MIRABOX_CODEX_COMPANION_REPORT_DESCRIPTOR)

    def test_c_descriptor_matches_python_and_active_driver_dual_tlc(self):
        protocol_path = ROOT / "driver" / "codexmicro-umdf" / "protocol" / "codexmicro_protocol.h"
        driver_path = ROOT / "driver" / "codexmicro-umdf" / "driver" / "codexmicro.c"
        protocol = protocol_path.read_text(encoding="utf-8")
        driver = driver_path.read_text(encoding="utf-8")
        self.assertEqual(
            c_array_bytes(protocol, "MIRABOX_CODEX_COMPANION_REPORT_DESCRIPTOR"),
            companion.MIRABOX_CODEX_COMPANION_REPORT_DESCRIPTOR,
        )
        self.assertEqual(
            c_array_bytes(protocol, "MIRABOX_CODEX_MICRO_DUAL_REPORT_DESCRIPTOR"),
            companion.MIRABOX_CODEX_MICRO_DUAL_REPORT_DESCRIPTOR,
        )
        # The active driver now advertises both top-level collections.  Keep
        # this assertion source-only so it remains useful without a WDK build.
        active_descriptor = c_array_bytes(driver, "G_DefaultReportDescriptor")
        self.assertEqual(len(active_descriptor), 58)
        self.assertEqual(
            active_descriptor,
            companion.MIRABOX_CODEX_MICRO_DUAL_REPORT_DESCRIPTOR,
        )
        self.assertIn("BridgeReadInput", driver)
        self.assertNotIn("CompanionReadQueue", driver)

    def test_report_id_translation_preserves_opaque_body(self):
        codex = bytes([companion.MIRABOX_CODEX_MICRO_REPORT_ID]) + bytes(range(1, 64))
        wrapped = companion.pack_companion_report(codex)
        self.assertEqual(len(wrapped), 64)
        self.assertEqual(wrapped[0], companion.MIRABOX_CODEX_COMPANION_REPORT_ID)
        self.assertEqual(wrapped[1:], codex[1:])
        self.assertEqual(companion.unpack_companion_report(wrapped), codex)

    def test_report_validation_rejects_wrong_id_and_length(self):
        with self.assertRaises(companion.CompanionReportError):
            companion.pack_companion_report(b"\x05" + bytes(63))
        with self.assertRaises(companion.CompanionReportError):
            companion.unpack_companion_report(b"\x06" + bytes(63))
        with self.assertRaises(companion.CompanionReportError):
            companion.pack_companion_report(bytes(63))
        with self.assertRaises(companion.CompanionReportError):
            companion.unpack_companion_report(bytes(65))

    def test_hid_info_selection_requires_companion_usage_when_present(self):
        entries = [
            {"path": "primary", "vendor_id": 0x303A, "product_id": 0x8360,
             "usage_page": 0xFF00, "usage": 1},
            {"path": "wrong", "vendor_id": 0x303A, "product_id": 0x8360,
             "usage_page": 0xFF70, "usage": 2},
            {"path": "right", "vendor_id": "0x303a", "product_id": "0x8360",
             "usage_page": "0xff70", "usage": "1"},
        ]
        selected = companion.find_companion_hid_info(entries)
        self.assertEqual(companion.companion_path_from_info(selected), "right")
        self.assertIsNone(companion.find_companion_hid_info([
            {"path": "wrong", "vendor_id": 0x303A, "product_id": 0x8360,
             "usage_page": 0xFF00, "usage": 1},
        ]))

    def test_hid_info_selection_rejects_ambiguous_missing_fields(self):
        complete = {
            "path": "right",
            "vendor_id": 0x303A,
            "product_id": 0x8360,
            "usage_page": 0xFF70,
            "usage": 1,
        }
        for missing in ("vendor_id", "product_id", "usage_page", "usage"):
            entry = dict(complete)
            entry.pop(missing)
            self.assertIsNone(
                companion.find_companion_hid_info([entry]),
                f"missing {missing} must not select an ambiguous HID collection",
            )

        # A caller can explicitly opt out of an identity field check; this is
        # different from silently accepting a missing field under defaults.
        no_vid = dict(complete)
        no_vid.pop("vendor_id")
        self.assertEqual(
            companion.companion_path_from_info(
                companion.find_companion_hid_info([no_vid], vendor_id=None)
            ),
            "right",
        )

    def test_lazy_hidapi_adapter_discovers_reads_and_writes(self):
        hid = FakeHid()
        transport = companion.CompanionHidTransport.open_first(hid_module=hid)
        self.assertEqual(hid.enumerated, (0x303A, 0x8360))
        self.assertEqual(hid.device_instance.opened_path, "companion")

        codex = bytes([0x06]) + bytes(range(1, 64))
        self.assertEqual(transport.write_codex_report(codex), 64)
        self.assertEqual(hid.device_instance.writes, [companion.pack_companion_report(codex)])

        companion_packet = companion.pack_companion_report(codex)
        hid.device_instance.reads.append(list(companion_packet))
        self.assertEqual(transport.read_codex_report(timeout_ms=37), codex)
        self.assertEqual(hid.device_instance.last_read, (64, 37))
        self.assertIsNone(transport.read_codex_report(timeout_ms=1))
        transport.close()
        self.assertTrue(hid.device_instance.closed)
        with self.assertRaises(companion.CompanionHidError):
            transport.write_codex_report(codex)

    def test_open_failure_closes_the_temporary_hidapi_device(self):
        hid = FakeHid()
        hid.device_instance = FailingOpenDevice()
        with self.assertRaisesRegex(companion.CompanionHidError, "open_path"):
            companion.CompanionHidTransport.open_first(hid_module=hid)
        self.assertTrue(hid.device_instance.closed)

    def test_write_requires_a_complete_64_byte_hidapi_transfer(self):
        codex = bytes([0x06]) + bytes(range(1, 64))
        for result in (None, 0, 7, 63, -1):
            transport = companion.CompanionHidTransport(
                PartialWriteDevice(result), owns_device=False
            )
            with self.assertRaisesRegex(companion.CompanionHidError, "write"):
                transport.write_codex_report(codex)

        complete = companion.CompanionHidTransport(
            PartialWriteDevice(64), owns_device=False
        )
        self.assertEqual(complete.write_codex_report(codex), 64)

    def test_borrowed_handle_and_read_validation(self):
        device = FakeDevice()
        transport = companion.CompanionHidTransport(device, owns_device=False)
        transport.close()
        self.assertFalse(device.closed)

        active = companion.CompanionHidTransport(device, owns_device=False)
        with self.assertRaises(ValueError):
            active.read_codex_report(timeout_ms=-1)
        device.reads.append([0x06] + [0] * 63)
        with self.assertRaises(companion.CompanionReportError):
            active.read_codex_report(timeout_ms=1)

    def test_sideband_runtime_accepts_companion_transport_aliases(self):
        device = FakeDevice()
        transport = companion.CompanionHidTransport(device, owns_device=False)
        runtime = SidebandRuntime(transport, output_poll_ms=1, status_stream=None)

        message = {"method": "v.oai.hid", "params": {"k": "ENC_CW", "act": 2}}
        runtime.push_message(message)
        self.assertEqual(len(device.writes), 1)
        self.assertEqual(
            companion.unpack_companion_report(device.writes[0]),
            encode_micro_message(message)[0],
        )

        seen = []
        runtime.on_host_message = lambda value: (seen.append(value), runtime.stop_event.set())
        host_message = {"method": "host.focused_app", "params": {}}
        codex_report = encode_micro_message(host_message)[0]
        device.reads.append(list(companion.pack_companion_report(codex_report)))
        runtime._output_loop()
        self.assertEqual(seen, [host_message])
        runtime.stop()


if __name__ == "__main__":
    unittest.main()
