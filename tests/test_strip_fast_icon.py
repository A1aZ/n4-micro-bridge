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

    def test_fast_button_uses_a_large_action_surface_above_a_knob_hint(self):
        config={
            'visual':{'theme':'pixel','stripMode':'knobs','stripBrightness':55},
            'knobs':[{'mode':mode} for mode in ('micro','scroll','reasoning','brightness')],
            'buttons':[{'targetKey':None,'enabled':False} for _ in range(14)],
        }
        config['buttons'][13]={'targetKey':'ACT06','enabled':True}
        image=N4VisualRenderer(config).render().keys[13]
        self.assertEqual(image.getpixel((1,56)),(124,98,40))
        self.assertEqual(image.getpixel((40,42)),(255,210,90))
        self.assertEqual(image.getpixel((168,56)),(35,30,20))
        self.assertEqual(image.getpixel((88,80)),(17,26,39))
        self.assertEqual(image.getpixel((1,97)),(46,59,76))
        self.assertEqual(image.getpixel((168,97)),(17,26,39))
