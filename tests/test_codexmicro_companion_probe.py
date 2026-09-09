import contextlib
import io
import json
import pathlib
import subprocess
import sys
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import codexmicro_companion as companion  # noqa: E402
import codexmicro_sideband as sideband  # noqa: E402


class EnumerateOnlyHid:
    """Fake backend that fails loudly if a probe tries to open a device."""

    def __init__(self, entries):
        self.entries = entries
        self.calls = []
        self.device_called = False

    def enumerate(self, vendor_id, product_id):
        self.calls.append((vendor_id, product_id))
        return list(self.entries)

    def device(self):  # pragma: no cover - a probe must never call this
        self.device_called = True
        raise AssertionError("enumeration probe must not instantiate hid.device")


class BrokenEnumerateHid:
    def enumerate(self, vendor_id, product_id):
        raise OSError("backend unavailable")


class OpenDevice:
    def __init__(self):
        self.opened_path = None
        self.closed = False
        self.writes = []
        self.read_calls = []

    def open_path(self, path):
        self.opened_path = path

    def write(self, packet):
        self.writes.append(bytes(packet))
        return len(packet)

    def read(self, length, timeout_ms):
        self.read_calls.append((length, timeout_ms))
        return []

    def close(self):
        self.closed = True


class OpenOnlyHid:
    def __init__(self):
        self.instance = OpenDevice()

    def device(self):
        return self.instance


class CompanionProbeTests(unittest.TestCase):
    def _entry_set(self):
        return [
            {
                "path": "primary",
                "vendor_id": 0x303A,
                "product_id": 0x8360,
                "usage_page": 0xFF00,
                "usage": 1,
                "product_string": "Codex primary",
            },
            {
                "path": b"companion",
                "vendorId": "0x303a",
                "productId": "0x8360",
                "usagePage": "0xff70",
                "usage": "1",
                "interfaceNumber": "2",
                "manufacturerString": "Mirabox",
            },
        ]

    def test_probe_enumerates_and_never_opens(self):
        hid = EnumerateOnlyHid(self._entry_set())
        value = companion.probe_companion_hid(hid_module=hid)

        self.assertTrue(value["ok"])
        self.assertEqual(value["status"], "compatible")
        self.assertTrue(value["enumerated"])
        self.assertTrue(value["hidapiAvailable"])
        self.assertTrue(value["interfaceFound"])
        self.assertTrue(value["identityCompatible"])
        self.assertTrue(value["pathReady"])
        self.assertFalse(value["driverTouched"])
        self.assertFalse(value["opened"])
        self.assertEqual(hid.calls, [(0x303A, 0x8360)])
        self.assertFalse(hid.device_called)
        self.assertEqual(value["candidateCount"], 2)
        self.assertEqual(value["matchCount"], 1)
        self.assertEqual(value["paths"], ["companion"])
        self.assertEqual(value["path"], "companion")
        self.assertEqual(value["matches"][0]["usagePage"], 0xFF70)
        self.assertEqual(value["matches"][0]["interfaceNumber"], 2)

    def test_probe_reports_candidates_without_false_positive(self):
        hid = EnumerateOnlyHid([
            {
                "path": "missing-usage",
                "vendor_id": 0x303A,
                "product_id": 0x8360,
            },
            {
                "path": "wrong-page",
                "vendor_id": 0x303A,
                "product_id": 0x8360,
                "usage_page": 0xFF00,
                "usage": 1,
            },
        ])
        value = companion.probe_companion_hid(hid_module=hid)

        self.assertFalse(value["ok"])
        self.assertEqual(value["status"], "interface-not-found")
        self.assertTrue(value["enumerated"])
        self.assertFalse(value["interfaceFound"])
        self.assertFalse(value["pathReady"])
        self.assertEqual(value["candidateCount"], 2)
        self.assertEqual(value["matchCount"], 0)
        self.assertEqual(value["paths"], [])
        self.assertFalse(any(item["matched"] for item in value["candidates"]))
        self.assertFalse(hid.device_called)

    def test_probe_classifies_backend_errors_and_missing_api(self):
        broken = companion.probe_companion_hid(hid_module=BrokenEnumerateHid())
        self.assertFalse(broken["ok"])
        self.assertEqual(broken["status"], "enumeration-error")
        self.assertFalse(broken["enumerated"])
        self.assertFalse(broken["opened"])
        self.assertFalse(broken["driverTouched"])

        missing = companion.probe_companion_hid(hid_module=object())
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["status"], "hidapi-unavailable")
        self.assertFalse(missing["opened"])
        self.assertFalse(missing["driverTouched"])

    def test_probe_distinguishes_identity_match_without_path(self):
        hid = EnumerateOnlyHid([
            {
                "vendor_id": 0x303A,
                "product_id": 0x8360,
                "usage_page": 0xFF70,
                "usage": 1,
            },
        ])
        value = companion.probe_companion_hid(hid_module=hid)

        self.assertFalse(value["ok"])
        self.assertEqual(value["status"], "path-unavailable")
        self.assertTrue(value["interfaceFound"])
        self.assertTrue(value["identityCompatible"])
        self.assertFalse(value["pathReady"])
        self.assertEqual(value["matchCount"], 1)
        self.assertEqual(value["paths"], [])
        self.assertFalse(hid.device_called)

    def test_probe_rejects_out_of_range_identity_values(self):
        with self.assertRaises(ValueError):
            companion.probe_companion_hid(hid_module=EnumerateOnlyHid([]), vendor_id=0x10000)
        with self.assertRaises(ValueError):
            companion.probe_companion_hid(hid_module=EnumerateOnlyHid([]), usage_page=-1)

    def test_explicit_open_path_uses_lazy_hidapi_handle(self):
        hid = OpenOnlyHid()
        info = {"path": "explicit"}
        transport = companion.CompanionHidTransport.open_path(
            "\\\\?\\hid#companion",
            hid_module=hid,
            info=info,
        )
        self.assertEqual(hid.instance.opened_path, "\\\\?\\hid#companion")
        self.assertIs(transport.info, info)
        transport.close()
        self.assertTrue(hid.instance.closed)

        with self.assertRaises(companion.CompanionHidUnavailable):
            companion.CompanionHidTransport.open_path("", hid_module=hid)

    def test_companion_cli_check_opens_only_explicit_fake_path(self):
        hid = OpenOnlyHid()
        explicit_path = "\\\\?\\hid#companion-check"
        stdout = io.StringIO()
        stderr = io.StringIO()

        # The companion path itself is platform-neutral once a hidapi backend
        # is supplied.  Patch only the front-door Windows guard; no Win32 API,
        # physical N4 adapter, or real HID handle is loaded by --check-only.
        with mock.patch.dict(sys.modules, {"hid": hid}), mock.patch.object(
            sideband.os, "name", "nt"
        ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = sideband.main([
                "--companion",
                "--companion-path",
                explicit_path,
                "--check-only",
            ])

        self.assertEqual(exit_code, 0, stderr.getvalue())
        value = json.loads(stdout.getvalue().strip())
        self.assertEqual(value["mode"], "companion-check")
        self.assertTrue(value["ok"])
        self.assertEqual(value["transport"], "companion-hid")
        self.assertEqual(value["path"], explicit_path)
        self.assertEqual(value["vendorId"], 0x303A)
        self.assertEqual(value["productId"], 0x8360)
        self.assertEqual(value["usagePage"], 0xFF70)
        self.assertEqual(value["usage"], 1)
        self.assertEqual(value["reportId"], 7)
        self.assertEqual(value["reportLength"], 64)
        self.assertFalse(value["driverTouched"])
        self.assertEqual(hid.instance.opened_path, explicit_path)
        self.assertEqual(hid.instance.writes, [])
        self.assertEqual(hid.instance.read_calls, [])
        self.assertTrue(hid.instance.closed)

    def test_cli_help_and_invalid_arguments_are_hardware_free(self):
        script = ROOT / "scripts" / "codexmicro-companion-probe.py"
        help_result = subprocess.run(
            [sys.executable, str(script), "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("never opens a device", help_result.stdout)

        invalid = subprocess.run(
            [sys.executable, str(script), "--vendor-id", "0x10000"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(invalid.returncode, 2)
        value = json.loads(invalid.stdout)
        self.assertEqual(value["status"], "invalid-arguments")
        self.assertFalse(value["opened"])
        self.assertFalse(value["driverTouched"])


if __name__ == "__main__":
    unittest.main()
