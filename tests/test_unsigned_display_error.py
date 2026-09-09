import pathlib,sys,unittest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'src'))
from n4_visual_output import N4VisualOutput
class UnsignedError(unittest.TestCase):
    def test_error_does_not_commit_cache_and_next_pass_retries(self):
        class Fake:
            fail=True
            def set_key_image(self,*args):return 0
            def set_touchscreen_image(self,*args):return 0
            def refresh(self):return 0x05000000 if self.fail else 0
        fake=Fake();output=N4VisualOutput(fake)
        try:
            self.assertFalse(output.render_once()['ok']);self.assertEqual(output._hashes,{})
            fake.fail=False;self.assertTrue(output.render_once()['ok']);self.assertEqual(len(output._hashes),15)
        finally:output.stop()
