import io
import json
import pathlib
import subprocess
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from codexmicro_sideband import (  # noqa: E402
    BRIDGE_INTERFACE_GUID,
    IOCTL_MIRABOX_CODEX_GET_INFO,
    IOCTL_MIRABOX_CODEX_PUSH_INPUT,
    IOCTL_MIRABOX_CODEX_READ_OUTPUT,
    IOCTL_MIRABOX_CODEX_RESET,
    MIRABOX_CODEX_MICRO_REPORT_ID,
    MIRABOX_CODEX_MICRO_REPORT_LENGTH,
    MicroMessageDecoder,
    HostRpcEndpoint,
    ReportValidationError,
    SidebandClient,
    SidebandIoError,
    SidebandInfo,
    SidebandUnavailable,
    SIDEBAND_ARCHITECTURE,
    SIDEBAND_ARCHITECTURE_WARNING,
    SIDEBAND_OPERATIONAL,
    encode_micro_message,
    main,
    probe_sideband,
    sideband_capability,
    validate_report,
)
import codexmicro_sideband as sideband  # noqa: E402
from codexmicro_files import MicroFileStore  # noqa: E402


class FakeApi:
    """Enough of _WindowsApi to exercise SidebandClient off Windows."""

    def __init__(self):
        self.paths = [r"\\?\codexmicro-test"]
        self.opened = []
        self.calls = []
        self.next_handle = object()
        self.output = []

    def enumerate_paths(self):
        return list(self.paths)

    def create_file(self, path):
        self.opened.append(path)
        return self.next_handle

    def close_handle(self, handle):
        self.calls.append(("close", handle))

    def device_io_control(self, handle, code, input_data=b"", output_size=0, timeout_ms=5000):
        self.calls.append((code, bytes(input_data), output_size, timeout_ms))
        if code == IOCTL_MIRABOX_CODEX_GET_INFO:
            return bytes.fromhex("3a30 6083 0001 00ff 06 40 0000")
        if code == IOCTL_MIRABOX_CODEX_READ_OUTPUT:
            if not self.output:
                raise RuntimeError("no fake output")
            return self.output.pop(0)
        return b""


class EmptyApi(FakeApi):
    def enumerate_paths(self):
        return []


class AccessDeniedApi(FakeApi):
    def create_file(self, path):
        raise SidebandIoError("CreateFileW", sideband.ERROR_ACCESS_DENIED, "Access is denied")


class MismatchApi(FakeApi):
    def device_io_control(self, handle, code, input_data=b"", output_size=0, timeout_ms=5000):
        if code == IOCTL_MIRABOX_CODEX_GET_INFO:
            # Correct framing, intentionally wrong VID/PID and report ID.
            return bytes.fromhex("3412 7856 0001 00ff 05 40 0000")
        return super().device_io_control(handle, code, input_data, output_size, timeout_ms)


def sample_report(payload=b""):
    report = bytearray(MIRABOX_CODEX_MICRO_REPORT_LENGTH)
    report[0] = MIRABOX_CODEX_MICRO_REPORT_ID
    report[1] = 2
    report[2] = min(len(payload), 61)
    report[3 : 3 + report[2]] = payload[:61]
    return bytes(report)


class CodexMicroSidebandTests(unittest.TestCase):
    def test_decoder_rejects_nonfinite_json_and_recovers(self):
        for payload in [b'{"x":NaN}', b'{"x":Infinity}', b'{"x":1e400}', b'{"x":"\xc3("}']:
            with self.subTest(payload=payload):
                decoder = MicroMessageDecoder()
                report = bytes([6,2,len(payload)]) + payload + bytes(61-len(payload))
                with self.assertRaises(ValueError):
                    decoder.feed(report)
                self.assertEqual(decoder.feed(encode_micro_message({"id":7})[0]),[{"id":7}])

    def test_ioctl_values_match_ctl_code(self):
        self.assertEqual(IOCTL_MIRABOX_CODEX_PUSH_INPUT, 0x0022A004)
        self.assertEqual(IOCTL_MIRABOX_CODEX_READ_OUTPUT, 0x00226008)
        self.assertEqual(IOCTL_MIRABOX_CODEX_GET_INFO, 0x0022600C)
        self.assertEqual(IOCTL_MIRABOX_CODEX_RESET, 0x0022A010)
        self.assertEqual(BRIDGE_INTERFACE_GUID, "{7F5A2E91-1C0E-4D1E-9C2A-1A6B8E537044}")
        guid = sideband._guid_from_text()
        self.assertEqual((guid.Data1, guid.Data2, guid.Data3), (0x7F5A2E91, 0x1C0E, 0x4D1E))
        self.assertEqual(bytes(guid.Data4), bytes.fromhex("9c2a1a6b8e537044"))

    def test_report_validation_is_strict(self):
        value = validate_report(sample_report())
        self.assertEqual(len(value), 64)
        with self.assertRaises(ReportValidationError):
            validate_report(b"\x06" + b"\0" * 62)
        with self.assertRaises(ReportValidationError):
            validate_report(b"\x05" + b"\0" * 63)

    def test_encode_and_decode_multibyte_message(self):
        message = {"method": "host.focused_app", "params": {"title": "旋钮😀"}}
        reports = encode_micro_message(message)
        self.assertTrue(reports)
        self.assertTrue(all(len(report) == 64 for report in reports))
        self.assertTrue(all(report[0:2] == b"\x06\x02" for report in reports))
        decoder = MicroMessageDecoder()
        decoded = []
        for report in reports:
            decoded.extend(decoder.feed(report))
        self.assertEqual(decoded, [message])

    def test_sideband_client_fake_api_calls_all_operations(self):
        api = FakeApi()
        client = SidebandClient(api=api, timeout_ms=123)
        info = client.check()
        self.assertIsInstance(info, SidebandInfo)
        self.assertTrue(info.compatible)
        client.reset()
        report = sample_report(b"x")
        client.push_input(report)
        api.output.append(report)
        self.assertEqual(client.read_output(timeout_ms=50), report)
        codes = [call[0] for call in api.calls if isinstance(call, tuple) and call]
        self.assertIn(IOCTL_MIRABOX_CODEX_GET_INFO, codes)
        self.assertIn(IOCTL_MIRABOX_CODEX_RESET, codes)
        self.assertIn(IOCTL_MIRABOX_CODEX_PUSH_INPUT, codes)
        self.assertIn(IOCTL_MIRABOX_CODEX_READ_OUTPUT, codes)
        client.close()

    def test_read_only_probe_classifies_success_and_expected_failures(self):
        compatible = probe_sideband(api=FakeApi())
        self.assertTrue(compatible["ok"])
        self.assertEqual(compatible["status"], "compatible")
        self.assertEqual(compatible["architecture"], SIDEBAND_ARCHITECTURE)
        self.assertFalse(compatible["operational"])
        self.assertEqual(compatible["architectureWarning"], SIDEBAND_ARCHITECTURE_WARNING)
        self.assertTrue(compatible["interfaceFound"])
        self.assertTrue(compatible["identityCompatible"])
        self.assertFalse(compatible["driverTouched"])

        missing = probe_sideband(api=EmptyApi())
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["status"], "interface-not-found")
        self.assertFalse(missing["interfaceFound"])

        denied = probe_sideband(api=AccessDeniedApi())
        self.assertFalse(denied["ok"])
        self.assertEqual(denied["status"], "access-denied")

        mismatch = probe_sideband(api=MismatchApi())
        self.assertFalse(mismatch["ok"])
        self.assertEqual(mismatch["status"], "identity-mismatch")
        self.assertTrue(mismatch["interfaceFound"])
        self.assertTrue(mismatch["compatibilityErrors"])

    def test_probe_cli_reports_unsupported_platform_without_touching_driver(self):
        command = [sys.executable, str(ROOT / "scripts" / "codexmicro-probe.py")]
        completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 2)
        value = json.loads(completed.stdout)
        if sys.platform != "win32":
            self.assertEqual(value["status"], "unsupported-platform")
        self.assertFalse(value["driverTouched"])

    def test_non_windows_client_fails_safely_without_path(self):
        # Injecting no API exercises the platform guard without loading any DLL.
        if sys.platform == "win32":
            self.skipTest("platform guard is specific to non-Windows hosts")
        with self.assertRaises(SidebandUnavailable):
            SidebandClient(timeout_ms=1).open()

    def test_cli_help_and_dry_run_are_hardware_free(self):
        command = [sys.executable, str(ROOT / "scripts" / "codexmicro-bridge.py"), "--dry-run"]
        completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=True)
        value = json.loads(completed.stdout)
        self.assertFalse(value["driverTouched"])
        self.assertEqual(value["reportLength"], 64)
        self.assertEqual(value["architecture"], SIDEBAND_ARCHITECTURE)
        self.assertFalse(value["operational"])
        self.assertEqual(value["architectureWarning"], SIDEBAND_ARCHITECTURE_WARNING)
        self.assertIn("IOCTL_MIRABOX_CODEX_PUSH_INPUT", value["ioctl"])
        help_result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "codexmicro-bridge.py"), "--help"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        )
        self.assertIn("--check-only", help_result.stdout)
        self.assertIn("--allow-scaffold-sideband", help_result.stdout)
        self.assertIn("--companion", help_result.stdout)
        self.assertIn("never installs a driver", help_result.stdout)

    def test_companion_check_fails_cleanly_without_companion_or_hidapi(self):
        # The command must report a concise, machine-safe error rather than a
        # traceback when the optional hidapi package is absent.  When hidapi is
        # installed but the virtual companion TLC is not present, enumeration
        # reaches the expected "no HID collection" branch instead.  Both are
        # safe/read-only outcomes and must be accepted across environments.
        command = [
            sys.executable,
            str(ROOT / "scripts" / "codexmicro-bridge.py"),
            "--companion",
            "--check-only",
        ]
        completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
        self.assertIn(completed.returncode, (0, 2))
        self.assertNotIn("Traceback (most recent call last)", completed.stderr)
        if completed.returncode == 2:
            error = completed.stderr.lower()
            self.assertTrue(
                "hidapi" in error or "no hid collection" in error,
                f"unexpected companion check error: {completed.stderr!r}",
            )

    def test_capability_metadata_is_explicitly_non_operational(self):
        value = sideband_capability()
        self.assertEqual(value["architecture"], SIDEBAND_ARCHITECTURE)
        self.assertIs(SIDEBAND_OPERATIONAL, False)
        self.assertIs(value["operational"], False)
        self.assertIn("companion control device", value["architectureWarning"])

    def test_cli_check_only_is_non_destructive_on_non_windows(self):
        if sys.platform == "win32":
            self.skipTest("non-Windows safety message")
        self.assertEqual(main(["--check-only"]), 2)

    def test_host_endpoint_compatibility_rpcs_and_preview(self):
        seen = []
        endpoint = HostRpcEndpoint(on_visual=lambda method, params: seen.append((method, params)))
        reply = endpoint.handle({
            "method": "lights.preview",
            "params": {
                "backlight": {"color": 0xFF0000, "brightness": 0.5, "effect": "breath", "magic": 0},
                "underglow": {"color": 0x0000FF, "brightness": 1, "effect": "solid"},
            },
            "id": 4,
        })
        self.assertEqual(reply["result"], {"ok": True})
        self.assertEqual(endpoint.lighting["keys"]["c"], 0xFF0000)
        self.assertEqual(endpoint.lighting["keys"]["m"], 0.0)
        self.assertEqual(endpoint.lighting["ambient"]["e"], "solid")
        self.assertEqual(endpoint.handle({"method": "ui.active_screen", "id": 5})["result"], {"screen_name": "home"})
        self.assertEqual(endpoint.handle({"method": "appmgr.list_installed", "id": 6})["result"], [])
        self.assertEqual(endpoint.handle({"method": "sys.selftest", "id": 7})["result"], {"ok": True})
        self.assertEqual(seen[0][0], "lights.preview")

    def test_host_endpoint_rejects_partial_lighting_updates_atomically(self):
        endpoint = HostRpcEndpoint()
        before = [dict(slot) for slot in endpoint.slots]
        reply = endpoint.handle({"method": "v.oai.thstatus", "params": [
            {"id": 0, "c": 0x123456}, {"id": 8, "c": 1},
        ], "id": 1})
        self.assertEqual(reply["error"]["code"], -32602)
        self.assertEqual(endpoint.slots, before)

    def test_host_endpoint_file_rpc_exposes_default_keymap_and_chunks(self):
        endpoint = HostRpcEndpoint()
        listing = endpoint.handle({"method": "fs.list", "params": {"checksum": True}, "id": 8})
        self.assertEqual([item["name"] for item in listing["result"]], ["keymap.json"])
        self.assertIsInstance(listing["result"][0]["size"], str)
        first = endpoint.handle({
            "method": "fs.readbin",
            "params": {"file": "/keymap.json", "offset": 0, "len": 9999},
            "id": 9,
        })
        self.assertEqual(first["result"]["total_size"], int(listing["result"][0]["size"]))
        self.assertLessEqual(len(first["result"]["data"]), 512)
        keymap = endpoint.handle({"method": "fs.read", "params": {"file": "keymap.json"}, "id": 10})
        self.assertEqual(keymap["result"]["version"], 1)
        self.assertEqual(
            keymap["result"]["profiles"][0]["layers"][0]["layout"]["encoders"][0][1],
            "KV_OAI_ENC_CW",
        )

    def test_host_endpoint_file_rpc_stream_write_delete_and_errors(self):
        endpoint = HostRpcEndpoint()
        import base64

        payload = base64.b64encode(b'{"version":1,"smartActions":{}}').decode("ascii")
        first = endpoint.handle({
            "method": "fs.writebin",
            "params": {"file": "smart_actions.json", "data": payload[:8], "completed": False},
            "id": 11,
        })
        self.assertEqual(first["result"], {"data_written": 6})
        second = endpoint.handle({
            "method": "fs.writebin",
            "params": {"file": "smart_actions.json", "data": payload[8:], "completed": True},
            "id": 12,
        })
        self.assertEqual(second["result"], {"data_written": 25})
        self.assertEqual(
            endpoint.handle({"method": "fs.read", "params": {"file": "smart_actions.json"}, "id": 13})["result"],
            {"version": 1, "smartActions": {}},
        )
        self.assertEqual(
            endpoint.handle({
                "method": "fs.write",
                "params": {"file": "smart_actions.json", "data": {"version": 2, "smartActions": {}}},
                "id": 14,
            })["result"],
            {"ok": True},
        )
        self.assertEqual(
            endpoint.handle({"method": "fs.delete", "params": {"file": "smart_actions.json"}, "id": 15})["result"],
            {"ok": True},
        )
        self.assertEqual(
            endpoint.handle({"method": "fs.read", "params": {"file": "smart_actions.json"}, "id": 16})["error"]["code"],
            -2,
        )
        self.assertEqual(
            endpoint.handle({
                "method": "fs.writebin",
                "params": {"file": "other.bin", "data": "", "completed": True},
                "id": 17,
            })["error"]["code"],
            -3,
        )
        self.assertEqual(endpoint.handle({"method": "fs.txbegin", "id": 18})["result"], {"tx": 1})
        self.assertEqual(endpoint.handle({"method": "fs.txcommit", "id": 19})["result"], {"ok": True})

    def test_host_endpoint_file_rpc_rejects_malformed_utf8_with_file_error(self):
        # Keep the invalid byte inside a JSON string: permissive decoders would
        # replace it and incorrectly return a successful object.
        files = MicroFileStore({
            "smart_actions.json": b'{"x":"' + bytes((0xC3, 0x28)) + b'"}',
        })
        endpoint = HostRpcEndpoint(files=files)
        reply = endpoint.handle({
            "method": "fs.read", "params": {"file": "smart_actions.json"}, "id": 21,
        })
        self.assertEqual(reply["error"]["code"], -2)
        self.assertEqual(reply["error"]["message"], "File does not exist")


if __name__ == "__main__":
    unittest.main()
