import pathlib,sys,unittest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'src'))
from n4_visual_renderer import N4VisualRenderer
class MicroStatusBand(unittest.TestCase):
    def test_host_hue_is_preserved_while_selection_pulses(self):
        for theme in ['pixel','symbols','atlas']:
            renderer=N4VisualRenderer({'visual':{'theme':theme}})
            for color in [0xffffff,0x309fff,0x32d074,0xffb340,0xf34f5f]:
                source={'agents':[{'c':color,'b':1,'e':4,'s':.35}]}
                rgb=(color>>16&255,color>>8&255,color&255)
                for phase in [0,.25,.5,.75]:
                    image=renderer.render_key(1,source,phase=phase)
                    self.assertEqual(image.getpixel((56,106)),rgb)
    def test_completion_is_not_latched_by_renderer(self):
        renderer=N4VisualRenderer({'visual':{'theme':'pixel'}})
        for color in [0x32d074,0xffffff,0x309fff]:
            image=renderer.render_key(1,{'agents':[{'c':color,'b':1,'e':1}]})
            self.assertEqual(image.getpixel((56,106)),(color>>16&255,color>>8&255,color&255))
        image=renderer.render_key(1,{'agents':[{'c':0x32d074,'b':0,'e':0}]})
        self.assertEqual(image.getpixel((56,106)),(36,46,60))
