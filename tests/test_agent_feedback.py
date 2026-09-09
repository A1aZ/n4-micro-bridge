import sys,pathlib,unittest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'src'))
from n4_visual_renderer import N4VisualRenderer,LightState
from n4_themes import agent_feedback

class AgentFeedbackTests(unittest.TestCase):
    def test_labels_and_stationary_speed(self):
        for e,label in enumerate(['未亮','常亮','流动','彩虹','呼吸','渐变','微呼吸']):
            self.assertEqual(agent_feedback(LightState(c=0x33bbcc,b=1,e=e,s=.4))[2],label)
        self.assertFalse(agent_feedback(LightState(c=0xffffff,b=1,e=4,s=0))[1])

    def test_pulse_preserves_binding_and_static_agent_pixels(self):
        for theme in ['symbols','atlas','pixel']:
            renderer=N4VisualRenderer({'visual':{'theme':theme}})
            for e in [2,3,4,5,6]:
                source={'agents':[{'c':0x33bbcc,'b':1,'e':e,'s':.4}]}
                a=renderer.render(source,phase=.1);b=renderer.render(source,phase=.6)
                self.assertNotEqual(a.keys[0].tobytes(),b.keys[0].tobytes())
                self.assertEqual(a.keys[1].tobytes(),b.keys[1].tobytes())
                self.assertEqual(a.bindings,b.bindings)
            for light in [{'c':0xffffff,'b':1,'e':1},{'c':0xffffff,'b':0,'e':4,'s':.4},{'c':0xffffff,'b':1,'e':4,'s':0}]:
                self.assertEqual(renderer.render({'agents':[light]},phase=.1).keys[0].tobytes(),renderer.render({'agents':[light]},phase=.6).keys[0].tobytes())
