import pathlib,sys,unittest,time,threading
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'src'))
from n4_visual_output import N4VisualOutput
from n4_visual_renderer import N4VisualRenderer

class Fake:
    def set_key_image(self,*args):pass
    def set_touchscreen_image(self,*args):pass
    def refresh(self):pass

class RefreshTests(unittest.TestCase):
    def test_startup_repaint_includes_static_keys_without_clearing_background(self):
        out=N4VisualOutput(Fake())
        try:
            self.assertTrue(out.render_once()['ok'])
            self.assertIsNotNone(out._startup_repaint_at)
            self.assertEqual(out.render_once()['uploads'],[])
            out._startup_repaint_at=time.monotonic()-1
            frame=out.render_once()
            self.assertTrue(frame['ok'])
            self.assertEqual([x.get('logicalKey') for x in frame['uploads']],list(range(1,15)))
            self.assertIsNone(out._startup_repaint_at)
            self.assertEqual(out.render_once()['uploads'],[])
        finally:out.stop()

    def test_repeated_host_wakeups_cannot_bypass_batch_pacing(self):
        out=N4VisualOutput(Fake(),refresh_ms=40)
        times=[]
        def frame():
            times.append(time.monotonic())
            return {'ok':True}
        try:
            with patch.object(out,'render_once',side_effect=frame):
                worker=threading.Thread(target=out._loop)
                worker.start()
                for _ in range(35):
                    out._wake.set()
                    time.sleep(.005)
                out._stop.set();out._wake.set();worker.join(1)
                self.assertFalse(worker.is_alive())
            self.assertGreaterEqual(len(times),2)
            # Windows monotonic clock can be quantized to ~15.6ms.
            self.assertTrue(all(b-a>=.025 for a,b in zip(times,times[1:])),times)
        finally:out.stop()

    def test_file_fallback_uploads_only_changed_regions_on_animation_and_state_changes(self):
        out=N4VisualOutput(Fake())
        try:
            out.apply_snapshot({'lighting':{'agents':[{'c':0x33ccbb,'b':1,'e':4,'s':.4}]},'model':{'theme':'pixel','stripMode':'knobs'}})
            self.assertTrue(out.render_once(force=True)['ok'])
            out._phase=.2
            self.assertEqual([x['logicalKey'] for x in out.render_once()['uploads']],[1])
            out.handle('v.oai.thstatus',[{'id':0,'c':0x8844cc}])
            frame=out.render_once()
            self.assertTrue(frame['ok'])
            self.assertEqual(frame['performance']['renderedKeys'],14)
            # A real agent colour/state change updates both its main key and
            # the passive 01–03 overview. Animation phase alone stays on key 1.
            self.assertEqual([x['logicalKey'] for x in frame['uploads']],[1,12])
            self.assertEqual(out.render_once()['uploads'],[])
        finally:out.stop()

    def test_initialization_gate_prevents_early_background_clear(self):
        class ReadyFake(Fake):
            ready=False
            calls=[]
            def wait_until_ready(self,timeout=0):return self.ready
            def set_key_image(self,key,path):self.calls.append(key)
            def set_touchscreen_image(self,path):self.calls.append('background')
        fake=ReadyFake();out=N4VisualOutput(fake)
        try:
            self.assertTrue(out.render_once()['waitingForDevice'])
            self.assertEqual(fake.calls,[])
            fake.ready=True
            self.assertTrue(out.render_once()['waitingForDevice'])
            self.assertEqual(fake.calls,[])
            out._ready_since=time.monotonic()-1
            with patch.object(out._stop,'wait',return_value=False):
                self.assertTrue(out.render_once()['ok'])
            self.assertEqual(fake.calls,['background']+list(range(1,15)))
            self.assertIsNotNone(out._startup_repaint_at)
        finally:out.stop()

    def test_single_key_pixels_match_full_bundle_including_information_strip(self):
        for theme in ['debug','symbols','atlas','pixel']:
            renderer=N4VisualRenderer({'visual':{'theme':theme,'stripMode':'knobs'}})
            source={'agents':[{'c':0x33ccbb,'b':1,'e':4,'s':.4}]}
            full=renderer.render(source,phase=.2)
            for i in range(1,15):
                self.assertEqual(renderer.render_key(i,source,phase=.2).tobytes(),full.keys[i-1].tobytes())
