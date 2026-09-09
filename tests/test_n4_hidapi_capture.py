import importlib.util
import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _load_capture_module():
    path = ROOT / "scripts" / "n4-hidapi-capture.py"
    spec = importlib.util.spec_from_file_location("n4_hidapi_capture_script", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


capture = _load_capture_module()


class N4HidapiCaptureTests(unittest.TestCase):
    def test_interface_zero_is_selectable(self):
        candidates = [
            {
                "path": b"input",
                "vendor_id": 0x6602,
                "product_id": 0x1001,
                "usage_page": 0xFFA0,
                "usage": 1,
                "interface_number": 0,
            },
            {
                "path": b"keyboard",
                "vendor_id": 0x6602,
                "product_id": 0x1001,
                "usage_page": 1,
                "usage": 6,
                "interface_number": 1,
            },
        ]
        selected = capture._select(candidates, path=None, interface=0)
        self.assertEqual(selected["path"], b"input")

    def test_missing_interface_metadata_still_sorts_after_explicit_zero(self):
        self.assertLess(
            capture._candidate_score({"usage_page": 0xFFA0, "usage": 1, "interface_number": 0}),
            capture._candidate_score({"usage_page": 0xFFA0, "usage": 1}),
        )


if __name__ == "__main__":
    unittest.main()
