import pathlib
import sys
import unittest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'src'))
from n4_visual_renderer import N4VisualRenderer
from n4_visual_output import N4VisualOutput
from n4_webui_bridge import _lighting_digest

class ThemeTests(unittest.TestCase):
    def test_all_themes_render_native_dimensions_and_distinct_art(self):
        outputs=[]
        for theme in ['symbols','atlas','pixel','debug']:
            bundle=N4VisualRenderer({'visual':{'theme':theme}}).render()
            self.assertEqual([image.size for image in bundle.keys],[(112,112)]*10+[(176,112)]*4)
            outputs.append(bundle.keys[0].tobytes())
        self.assertEqual(len(set(outputs)),4)
        with self.assertRaises(ValueError):N4VisualRenderer({'visual':{'theme':'../invalid'}})

    def test_theme_only_change_triggers_poller_and_native_redraw(self):
        a={'lighting':{},'model':{'theme':'symbols'}}
        b={'lighting':{},'model':{'theme':'pixel'}}
        self.assertNotEqual(_lighting_digest(a),_lighting_digest(b))
        class Fake:
            def __init__(self):self.keys=[];self.backgrounds=0
            def set_key_image(self,index,path):self.keys.append(index)
            def set_touchscreen_image(self,path):self.backgrounds+=1
            def refresh(self):pass
        device=Fake();output=N4VisualOutput(device)
        try:
            output.apply_snapshot(a);self.assertTrue(output.render_once()['ok'])
            count=len(device.keys)
            output.apply_snapshot(b);self.assertTrue(output.render_once()['ok'])
            self.assertEqual(output.theme,'pixel');self.assertGreater(len(device.keys),count)
            self.assertEqual(device.backgrounds,1)
            self.assertEqual(output.render_once()['uploads'],[])
        finally:output.stop()
