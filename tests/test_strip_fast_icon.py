import pathlib,sys,unittest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'src'))
from n4_visual_renderer import N4VisualRenderer
class FastIcon(unittest.TestCase):
    def test_badge_changes_only_last_zone_and_matches_single_key(self):
        base={'visual':{'theme':'pixel','stripMode':'knobs'},'buttons':[{'targetKey':None,'enabled':False} for _ in range(14)]}
        plain=N4VisualRenderer(base).render()
        base['buttons'][13]={'targetKey':'ACT06','enabled':True}
        renderer=N4VisualRenderer(base);marked=renderer.render()
        for i in range(13):self.assertEqual(plain.keys[i].tobytes(),marked.keys[i].tobytes())
        self.assertNotEqual(plain.keys[13].tobytes(),marked.keys[13].tobytes())
        self.assertEqual(renderer.render_key(14).tobytes(),marked.keys[13].tobytes())

    def test_fast_button_is_an_independent_full_height_action_surface(self):
        config={
            'visual':{'theme':'pixel','stripMode':'knobs','stripBrightness':55},
            'knobs':[{'mode':mode} for mode in ('micro','scroll','reasoning','brightness')],
            'buttons':[{'targetKey':None,'enabled':False} for _ in range(14)],
        }
        config['buttons'][13]={'targetKey':'ACT06','enabled':True}
        image=N4VisualRenderer(config).render().keys[13]
        self.assertEqual(image.getpixel((104,56)),(46,59,76))
        self.assertEqual(image.getpixel((108,56)),(17,26,39))
        self.assertEqual(image.getpixel((113,56)),(124,98,40))
        self.assertEqual(image.getpixel((130,58)),(255,210,90))
        self.assertEqual(image.getpixel((168,56)),(35,30,20))
