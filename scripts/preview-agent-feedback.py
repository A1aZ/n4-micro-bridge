"""Render code-native theme QA contact sheet; no hardware or RPC actions."""
import pathlib,sys
from PIL import Image,ImageDraw
ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from n4_visual_renderer import N4VisualRenderer
sheet=Image.new('RGB',(6*132,3*152),(13,20,32))
for row,theme in enumerate(['symbols','atlas','pixel']):
    renderer=N4VisualRenderer({'visual':{'theme':theme}})
    lights=[{'c':0x30c7b7,'b':1,'e':4,'s':.4},{'c':0xffffff,'b':1,'e':1},
            {'c':0xf5be59,'b':1,'e':2,'s':.4},{'c':0x30c7b7,'b':1,'e':4,'s':.4},
            {'c':0x9174ed,'b':1,'e':6,'s':.4},{'b':0,'e':0}]
    for col in range(6):
        frame=renderer.render({'agents':lights},phase=.15 if col!=3 else .65)
        sheet.paste(frame.keys[col],(col*132+10,row*152+25))
    ImageDraw.Draw(sheet).text((10,row*152+6),theme,fill='white')
out=ROOT/'artifacts'/'themes'/'agent-feedback.png'
sheet.save(out)
print(out)
