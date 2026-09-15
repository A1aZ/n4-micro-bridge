import unittest,pathlib,sys
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'src'))
from n4_visual_renderer import N4VisualRenderer
from n4_webui_bridge import _lighting_digest
class InfoStripTests(unittest.TestCase):
    def test_info_regions_and_brightness_update(self):
        config={'visual':{'theme':'pixel','stripMode':'knobs'},'knobs':[{'mode':m} for m in ['micro','scroll','reasoning','brightness']]}
        clock={'time':'09:07','date':'2026-09-15','weekday':'周二'}
        first=N4VisualRenderer(config).render(clock=clock)
        self.assertEqual([x.size for x in first.keys[10:]],[(176,112)]*4)
        config['visual']['stripBrightness']=55
        second=N4VisualRenderer(config).render(clock=clock)
        self.assertNotEqual(first.keys[13].tobytes(),second.keys[13].tobytes())
        self.assertEqual(first.keys[10].tobytes(),second.keys[10].tobytes())
        self.assertNotEqual(_lighting_digest({'model':{'stripBrightness':None}}),_lighting_digest({'model':{'stripBrightness':55}}))

    def test_left_strip_contains_clock_pixels_and_changes_by_minute(self):
        config={'visual':{'theme':'pixel','stripMode':'knobs'},'knobs':[{'mode':'micro'}]}
        first=N4VisualRenderer(config).render(clock={'time':'09:07','date':'2026-09-15','weekday':'周二'})
        second=N4VisualRenderer(config).render(clock={'time':'09:08','date':'2026-09-15','weekday':'周二'})
        self.assertNotEqual(first.keys[10].tobytes(), second.keys[10].tobytes())
