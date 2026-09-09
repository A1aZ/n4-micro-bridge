import pathlib,sys,unittest
from io import BytesIO
from PIL import Image
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'src'))
from n4_frame_cache import NativeFrameCache
from n4_visual_output import N4VisualOutput
class Fake:
    def __init__(self):self.sent=[];self.refreshes=0;self.fail=False
    def supports_native_frames(self):return True
    def set_key_frame(self,key,data):
        self.sent.append(key)
        if self.fail:raise RuntimeError('transport failed')
    def refresh(self):self.refreshes+=1
class Frames(unittest.TestCase):
    def test_cache_orientation_and_bounds(self):
        cache=NativeFrameCache(max_entries=2)
        img=Image.new('RGB',(112,112),'black')
        img.paste('white',(0,0,40,40))
        data=cache.get('one',lambda:img)
        reference=BytesIO();img.rotate(180,expand=False).save(reference,format='JPEG')
        self.assertEqual(data,reference.getvalue())
        self.assertIs(data,cache.get('one',lambda:None))
        with Image.open(BytesIO(data)) as result:
            self.assertGreater(result.getpixel((100,100))[0],240)
            self.assertLess(result.getpixel((10,10))[0],10)
        cache.get('two',lambda:img);cache.get('three',lambda:img)
        self.assertEqual(len(cache.frames),2)
    def test_memory_batches_skip_static_keys_and_retry_failed_batch(self):
        fake=Fake();out=N4VisualOutput(fake,target='keys')
        try:
            out.apply_snapshot({'lighting':{'agents':[{'c':0x33ccbb,'b':1,'e':4,'s':.4}]},'model':{'theme':'pixel'}})
            self.assertTrue(out.render_once()['ok']);self.assertEqual(len(fake.sent),14)
            self.assertEqual(fake.refreshes,1)
            out._phase=.1
            fake.sent=[]
            self.assertTrue(out.render_once()['ok'])
            self.assertEqual(fake.sent,[1])
            self.assertEqual(out.render_once()['uploads'],[])
            out.handle('v.oai.thstatus',[{'id':0,'c':0xff8800}]);fake.fail=True
            self.assertFalse(out.render_once()['ok'])
            fake.fail=False;fake.sent=[]
            self.assertTrue(out.render_once()['ok']);self.assertEqual(fake.sent,[1])
            self.assertEqual(fake.refreshes,3)
            self.assertEqual(out.render_once()['uploads'],[])
            self.assertGreater(out._frame_cache.hits,0)
        finally:out.stop()
