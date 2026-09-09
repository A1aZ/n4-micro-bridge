import pathlib,sys,unittest
from PIL import Image
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'src'))
from n4_visual_output import N4VisualOutput
class Fake:
    def __init__(self):self.images=[]
    def set_key_image(self,key,path):
        with Image.open(path) as image:self.images.append(image.copy())
    def set_touchscreen_image(self,path):pass
    def refresh(self):pass
class Diagnostics(unittest.TestCase):
    def test_patterns_freeze_state_and_restore_all_keys(self):
        fake=Fake();out=N4VisualOutput(fake)
        try:
            for revision,mode in enumerate(['black','white','box']):
                payload={'lighting':{},'model':{'screenTest':{'mode':mode,'revision':revision}}}
                out.apply_snapshot(payload)
                self.assertTrue(out.render_once()['ok'])
                self.assertEqual(len(fake.images[-14:]),14)
                im=fake.images[-14]
                self.assertEqual(im.getpixel((0,0)),(255,255,255) if mode=='white' else (0,0,0))
                if mode=='box':self.assertGreater(im.getpixel((34,56))[0],230)
                payload['lighting']={'agents':[{'c':0xff0000,'b':1,'e':1}]}
                out.apply_snapshot(payload)
                self.assertEqual(out.render_once()['uploads'],[])
                self.assertIsNone(out._startup_repaint_at)
            out.apply_snapshot({'lighting':{},'model':{'screenTest':{'mode':'off'}}})
            self.assertEqual(len(out.render_once()['uploads']),14)
        finally:out.stop()
