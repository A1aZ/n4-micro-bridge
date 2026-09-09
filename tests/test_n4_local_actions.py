import pathlib
import sys
import unittest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'src'))
from n4_local_actions import PhysicalKnobActions,scroll_allowed

def record(action):return {'inputPacket':True,'decoded':{'kind':'knob_rotate'},'post':{'ok':True,'response':{'localActions':[action]}}}
class ActionsTests(unittest.TestCase):
    def test_scroll_restricted_to_foreground_target_pointer_and_no_modifiers(self):
        self.assertTrue(scroll_allowed('C:\\app\\ChatGPT.exe',True,False))
        for args in [('chrome.exe',True,False),('ChatGPT.exe',False,False),('ChatGPT.exe',True,True),('',True,False)]:self.assertFalse(scroll_allowed(*args))

    def test_brightness_bounds_and_no_extra_focus_events(self):
        class Device:
            def __init__(self):self.values=[]
            def set_brightness(self,v):self.values.append(v)
        d=Device();a=PhysicalKnobActions(d)
        for i in range(30):a.handle_record(record({'type':'brightness','delta':-5}))
        self.assertEqual(d.values[-1],10)
        for i in range(30):a.handle_record(record({'type':'brightness','delta':5}))
        self.assertEqual(d.values[-1],100)
        self.assertTrue(all(10<=v<=100 for v in d.values));self.assertIsNone(a.wheel)

    def test_simulated_or_failed_records_do_not_execute(self):
        class Forbidden:
            def set_brightness(self,v):raise AssertionError('must not execute')
        a=PhysicalKnobActions(Forbidden())
        for item in [None,{}, {'inputPacket':True,'decoded':None}, {'localActions':[{'type':'brightness','delta':5}]}]:a.handle_record(item)
        failed=record({'type':'brightness','delta':5});failed['post']['ok']=False;a.handle_record(failed)
        a.handle_record(record({'type':'setup-required','feature':'reasoning'}))
        self.assertEqual(a.executed,0);self.assertEqual(a.errors,0)

    def test_scroll_direction_and_errors_use_fake_backend_only(self):
        class Wheel:
            def __init__(self):self.directions=[]
            def scroll(self,d):self.directions.append(d);return {'executed':True,'reason':'fake'}
        w=Wheel();a=PhysicalKnobActions(None,w)
        for d in [-1,1]:a.handle_record(record({'type':'scroll','direction':d}))
        self.assertEqual(w.directions,[-1,1]);self.assertEqual(a.executed,2)
        a.handle_record(record({'type':'execute','command':'ignored'}));self.assertEqual(a.ignored,1)
