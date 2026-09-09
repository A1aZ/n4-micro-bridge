"""Offline production-renderer contact sheets for visual QA, no device I/O."""
import pathlib
import sys
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'src'))
from PIL import Image
from n4_visual_renderer import N4VisualRenderer
root=pathlib.Path(__file__).resolve().parents[1]
out=root/'artifacts'/'themes';out.mkdir(parents=True,exist_ok=True)
for theme in ['symbols','atlas','pixel']:
    lighting={'agents':[{'c':c,'b':1,'e':1} for c in [0x41cdec,0xa08aee,0x667080,0xffd269,0x667080,0x70de8b]]}
    bundle=N4VisualRenderer({'visual':{'theme':theme}}).render(lighting)
    sheet=Image.new('RGB',(744,390),(9,13,20))
    for i,key in enumerate(bundle.keys[:10]):sheet.paste(key,(76+(i%5)*120,16+(i//5)*120))
    for i,key in enumerate(bundle.keys[10:]):sheet.paste(key,(20+i*176,268))
    sheet.save(out/(theme+'.png'))
print(out)
