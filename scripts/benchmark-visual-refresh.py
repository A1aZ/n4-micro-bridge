"""CPU-only benchmark. Fake adapter; never opens HID or sends device commands."""
import pathlib,sys,time,json,statistics
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'src'))
from n4_visual_output import N4VisualOutput
class Fake:
    def set_key_image(self,*args):pass
    def set_touchscreen_image(self,*args):pass
    def refresh(self):pass
out=N4VisualOutput(Fake())
try:
    out.apply_snapshot({'lighting':{'agents':[{'c':0x30c7b7,'b':1,'e':4,'s':.4}]},'model':{'theme':'pixel','stripMode':'knobs'}})
    out.render_once(force=True)
    samples=[]
    for i in range(24):
        out._phase=(i/24)%1
        begin=time.perf_counter();result=out.render_once();samples.append((time.perf_counter()-begin)*1000)
        if not result['ok']:raise RuntimeError(result)
    print(json.dumps({'hardware':False,'frames':24,'medianMs':round(statistics.median(samples),2),'maxMs':round(max(samples),2)}))
finally:out.stop()
