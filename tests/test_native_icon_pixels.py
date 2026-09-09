import pathlib,sys,tempfile,unittest
from unittest.mock import patch
from PIL import Image
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'src'))
from n4_visual_renderer import N4VisualRenderer
from n4_visual_output import N4VisualOutput
class NativePixels(unittest.TestCase):
    def test_main_icons_never_resize_at_runtime(self):
        for theme in ['symbols','pixel']:
            renderer=N4VisualRenderer({'visual':{'theme':theme}})
            with patch.object(Image.Image,'resize',side_effect=AssertionError('unexpected icon scaling')):
                self.assertEqual(renderer.render_key(1,{'agents':[{'b':1,'c':0xffffff,'e':1}]}).size,(112,112))
                self.assertEqual(renderer.render_key(7).size,(112,112))
    def test_intermediate_is_lossless(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Image.new('RGB',(112,112),(12,73,192))
            path=pathlib.Path(tmp)/'key.png';N4VisualOutput._save(source,path)
            with Image.open(path) as result:
                self.assertEqual(result.format,'PNG');self.assertEqual(source.tobytes(),result.tobytes())
