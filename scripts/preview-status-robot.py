import pathlib,sys
from PIL import Image,ImageDraw
ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from n4_visual_renderer import N4VisualRenderer
colors=[0xffffff,0x309fff,0x32d074,0xffb340,0xf34f5f,0]
bundle=N4VisualRenderer({'visual':{'theme':'pixel'}}).render({'agents':[{'c':c,'b':1 if c else 0,'e':1 if c else 0} for c in colors]})
sheet=Image.new('RGB',(792,150),(13,20,32));draw=ImageDraw.Draw(sheet)
for i,name in enumerate(['Idle','Working','Complete / unread','Needs input','Error','Off']):
    sheet.paste(bundle.keys[i],(i*132+10,10));draw.text((i*132+10,129),name,fill='white')
sheet.save(ROOT/'artifacts/themes/status-robot.png')
