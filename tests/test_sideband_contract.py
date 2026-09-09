import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
CHECKER = ROOT / "scripts" / "check-sideband-contract.py"
BUILD_ENV_CHECKER = ROOT / "scripts" / "check-driver-build-env.ps1"


def load_checker():
    spec = importlib.util.spec_from_file_location("sideband_contract_checker", CHECKER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class SidebandContractTests(unittest.TestCase):
    def test_offline_contract_passes_without_wdk_or_device(self):
        checker = load_checker()
        report = checker.check_contract(ROOT)
        self.assertTrue(report["ok"], report["issues"])
        self.assertEqual(report["descriptor"]["length"], 29)
        self.assertEqual(
            report["ioctl"]["c"]["IOCTL_MIRABOX_CODEX_PUSH_INPUT"]["value"],
            0x0022A004,
        )
        self.assertEqual(
            report["ioctl"]["c"]["IOCTL_MIRABOX_CODEX_READ_OUTPUT"]["value"],
            0x00226008,
        )

    def test_cli_json_is_machine_readable(self):
        result = subprocess.run(
            [sys.executable, str(CHECKER), "--json"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        report = json.loads(result.stdout)
        self.assertTrue(report["ok"], report["issues"])
        self.assertEqual(report["guid"]["c"], "{7F5A2E91-1C0E-4D1E-9C2A-1A6B8E537044}")

    @unittest.skipUnless(os.name == "nt", "PowerShell build-environment probe is Windows-only")
    def test_driver_build_environment_probe_is_machine_readable(self):
        powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
        if not powershell:
            self.skipTest("PowerShell is unavailable")
        result = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(BUILD_ENV_CHECKER),
                "-Json",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        # A complete WDK returns 0; a correctly diagnosed missing WDK returns
        # 2.  Either result must still carry valid JSON and never attempt a
        # build or installation.
        self.assertIn(result.returncode, (0, 2), result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report["project"]["exists"])
        self.assertTrue(report["visualStudio"]["present"])
        self.assertTrue(report["windowsSdk"]["present"])
        self.assertIsInstance(report["wdk"]["present"], bool)
        self.assertEqual(report["canBuild"], report["wdk"]["present"])
        if report["wdk"]["components"]["wdfLibrary"]:
            self.assertEqual(pathlib.Path(report["wdk"]["wdfLibrary"]).name, "WdfDriverStubUm.lib")


if __name__ == "__main__":
    unittest.main()
