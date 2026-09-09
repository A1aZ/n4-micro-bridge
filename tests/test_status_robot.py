import pathlib,sys,unittest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'src'))
from n4_visual_renderer import N4VisualRenderer,LightState
from micro_status import micro_status
class RobotTests(unittest.TestCase):
    def test_native_color_states_and_unknown(self):
        for color,state in [(0xffffff,'idle'),(0x309fff,'working'),(0x32d074,'complete'),(0xffb340,'attention'),(0xf34f5f,'error'),(0xb000ff,'unknown')]:
            self.assertEqual(micro_status(LightState(c=color,b=1,e=1)),state)
        self.assertEqual(micro_status(LightState(c=0x32d074,b=0,e=1)),'complete')
        self.assertEqual(micro_status(LightState(c=0,b=0,e=0)),'off')
    def test_same_state_same_robot_on_all_slots(self):
        renderer=N4VisualRenderer({'visual':{'theme':'pixel'}})
        source={'agents':[{'c':0x309fff,'b':1,'e':1}]*6}
        bundle=renderer.render(source)
        crops=[im.crop((10,30,102,102)).tobytes() for im in bundle.keys[:6]]
        self.assertEqual(len(set(crops)),1)
        faces=[]
        for color in [0xffffff,0x309fff,0x32d074,0xffb340,0xf34f5f]:
            faces.append(renderer.render_key(1,{'agents':[{'c':color,'b':1,'e':1}]}).crop((10,30,102,102)).tobytes())
        self.assertEqual(len(set(faces)),5)
