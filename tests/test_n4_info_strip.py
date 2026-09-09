import unittest,pathlib,sys
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'src'))
from n4_visual_renderer import N4VisualRenderer
from n4_webui_bridge import _lighting_digest
class InfoStripTests(unittest.TestCase):
    def test_info_regions_and_brightness_update(self):
        config={'visual':{'theme':'pixel','stripMode':'knobs'},'knobs':[{'mode':m} for m in ['micro','scroll','reasoning','brightness']]}
        first=N4VisualRenderer(config).render()
        self.assertEqual([x.size for x in first.keys[10:]],[(176,112)]*4)
        config['visual']['stripBrightness']=55
        second=N4VisualRenderer(config).render()
        self.assertNotEqual(first.keys[13].tobytes(),second.keys[13].tobytes())
        self.assertEqual(first.keys[10].tobytes(),second.keys[10].tobytes())
        self.assertNotEqual(_lighting_digest({'model':{'stripBrightness':None}}),_lighting_digest({'model':{'stripBrightness':55}}))
