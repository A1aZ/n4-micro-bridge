"""Production key themes using bundled Phosphor icons and generated avatars.

Only local, allowlisted assets are loaded. Theme does not affect input mapping.
"""
import json
import math
from functools import lru_cache
from pathlib import Path
from PIL import Image, ImageDraw
from micro_status import micro_status

ROOT=Path(__file__).resolve().parents[1]/'assets'/'themes'
CATALOG=json.loads((ROOT/'catalog.json').read_text(encoding='utf-8'))
STATUS_ART=json.loads((ROOT/'status-art.json').read_text(encoding='utf-8'))
THEMES=tuple(item['id'] for item in CATALOG['themes'])
BG=(17,26,39)
FG=(244,241,232)
MUTED=(126,139,158)

def validate_theme(value):
    if value not in THEMES:raise ValueError('Unsupported N4 visual theme')
    return value

@lru_cache(maxsize=32)
def asset(name):
    allowed=set(CATALOG['taskIcons'])|{v['icon'] for v in CATALOG['commands'].values()}|{CATALOG['unassigned']['icon']}
    allowed|={name for variants in CATALOG['variants'].values() for name in variants.values()}
    allowed|=set(CATALOG['knobIcons'].values())
    allowed|={name+'-pixel' for name in tuple(allowed)}
    allowed|={name+'-native' for name in tuple(allowed) if not name.endswith('-pixel')}
    allowed|={f'pixel-{i:02}' for i in range(1,7)}
    allowed|={f'robot-{s}' for s in ('idle','working','complete','attention','error','off','unknown')}
    if name not in allowed:raise ValueError('Unknown theme asset')
    with Image.open(ROOT/'compiled'/(name+'.png')) as image:return image.convert('RGBA')

def agent_feedback(light, enabled=True):
    active=enabled and light.b>0 and light.e!=0 and (light.c!=0 or light.e in (3,5))
    animated=active and light.e in (2,3,4,5,6) and light.s>0
    label=({1:'常亮',2:'流动',3:'彩虹',4:'呼吸',5:'渐变',6:'微呼吸'}.get(light.e,'灯效') if active else '未亮')
    if not enabled:label='停用'
    return active,animated,label

def render_key(binding,light,theme,font,phase=0.5):
    validate_theme(theme)
    width=176 if binding.index>10 else 112
    image=Image.new('RGB',(width,112),BG);draw=ImageDraw.Draw(image)
    enabled=binding.enabled and binding.target_key is not None
    ink=FG if enabled else MUTED
    rgb=light.rgb
    level=light.intensity if enabled else 0
    accent=tuple(round(55+(c-55)*level) for c in rgb)
    draw.rounded_rectangle((1,1,width-2,110),radius=9,outline=(46,59,76),width=1)
    target=binding.target_key or ''
    slot=int(target[2:]) if target.startswith('AG') and target[2:].isdigit() and int(target[2:])<6 else None
    def text(x,y,value,size,anchor='mm',numeric=False):
        draw.text((x,y),value,font=font(size,numeric),fill=ink,anchor=anchor)
    def icon(name,size,cx,cy,pixel=False):
        source=asset(name)
        artwork=source.copy() if source.size==(size,size) else source.resize((size,size),Image.Resampling.NEAREST if pixel else Image.Resampling.LANCZOS)
        if not enabled or (slot is not None and not agent_feedback(light,enabled)[0]):
            artwork=artwork.copy();artwork.putalpha(artwork.getchannel('A').point(lambda a:round(a*.4)))
        image.paste(artwork,(round(cx-size/2),round(cy-size/2)),artwork)
    if slot is not None:
        active,animated,status=agent_feedback(light,enabled)
        # Fixed geometry: only brightness varies; no moving edges or sprites.
        motion=phase if animated else 0.5
        wave=(1-math.cos(motion*math.pi*2))/2 if animated else 1
        feedback_rgb=rgb if any(rgb) else (89,211,230)
        edge=tuple(round(c*(0.55+0.45*wave)*light.b) for c in feedback_rgb) if active else (65,78,94)
        tint=tuple(round(BG[i]*0.72+edge[i]*0.28) for i in range(3)) if active else BG
        draw.rounded_rectangle((2,2,width-3,109),radius=9,fill=tint,outline=edge,width=6)
        number=f'{slot+1:02}'
        if theme=='symbols':
            icon(CATALOG['taskIcons'][slot]+'-native',64,width/2,56)
            text(12,14,number,13,'lm')
        elif theme=='atlas':
            text(12,15,number,59,'lt',True)
        elif theme=='pixel':
            icon(STATUS_ART[micro_status(light,enabled)],96,width/2,56,True)
            text(9,13,number,13,'lm')
        draw.ellipse((width-19,12,width-11,20),fill=edge)
        if enabled and not active:
            # Visibility marker only; does not assert host sleep/unassignment.
            draw.ellipse((width-23,10,width-11,22),fill=MUTED)
            draw.ellipse((width-18,8,width-8,18),fill=BG)
        # Preserve the host's exact status hue independently of selection
        # pulsing. No inferred completion latch, timeout, or local read state.
        status_color=tuple(round(c*light.b) for c in rgb) if active else (36,46,60)
        draw.rectangle((8,104,width-9,109),fill=status_color)
    else:
        entry=CATALOG['commands'].get(target,CATALOG['unassigned'])
        pixel=False;size=64
        name=CATALOG['variants'].get(theme,{}).get(target,entry['icon'])
        icon(name+'-native',size,width/2,48 if (theme=='atlas' or width>112) else 56,pixel)
        if theme=='atlas' or width>112:text(width/2,93,entry['label'] if enabled else '未分配',15 if width>112 else 13)
        if enabled and level>0:draw.line((12,5,width-13,5),fill=accent,width=3)
    return image

def render_info_strip(index,knob,brightness,font,fast_touch=False,clock=None):
    mode=knob.get('mode','micro')
    if mode not in CATALOG['knobIcons']:raise ValueError('Invalid information-strip mode')
    title={'micro':'导航','scroll':'聊天滚动','reasoning':'推理强度','brightness':'亮度'}[mode]
    detail={'micro':'跟随 Codex 模式','scroll':'鼠标放聊天区','reasoning':'− / +' if knob.get('reasoningConfigured') else '待绑定左右方向','brightness':f'{brightness}%' if brightness is not None else '旋转调节 · 每格5%'}[mode]
    if not knob.get('enabled',True):detail='已停用'
    image=Image.new('RGB',(176,112),BG);draw=ImageDraw.Draw(image)
    draw.rounded_rectangle((1,1,174,110),radius=8,outline=(46,59,76))
    if index == 0 and isinstance(clock, dict) and clock.get('time') and clock.get('date'):
        # The left-most strip is the one glance-away status area.  Keep the
        # first knob's role as a small hint while giving the clock enough
        # contrast and pixels to remain legible on the physical 176x112 lens.
        draw.text((12,15),'当前时间',font=font(11),fill=MUTED,anchor='lm')
        date_text=str(clock['date']) + (f" · {clock['weekday']}" if clock.get('weekday') else '')
        draw.text((164,15),date_text,font=font(10),fill=MUTED,anchor='ra')
        draw.text((12,61),str(clock['time']),font=font(35),fill=FG,anchor='lm')
        symbol=asset(CATALOG['knobIcons'][mode]).resize((18,18),Image.Resampling.LANCZOS)
        image.paste(symbol,(12,80),symbol)
        hint='旋钮 1 · 已停用' if not knob.get('enabled',True) else f'旋钮 1 · {title}'
        draw.text((38,94),hint,font=font(11),fill=MUTED,anchor='lm')
        return image
    draw.text((12,15),f'旋钮 {index+1}',font=font(11),fill=MUTED,anchor='lm')
    symbol=asset(CATALOG['knobIcons'][mode]).resize((25,25),Image.Resampling.LANCZOS)
    image.paste(symbol,(12,39),symbol)
    draw.text((47,52),title,font=font(17),fill=FG,anchor='lm')
    if fast_touch:
        draw.text((12,86),f'{brightness}%' if brightness is not None else '旋转调亮度',font=font(12),fill=MUTED,anchor='lm')
        draw.rounded_rectangle((125,67,167,104),radius=6,fill=(52,43,24),outline=(237,187,70))
        draw.polygon([(147,71),(136,88),(145,88),(140,100),(158,81),(149,81)],fill=(255,210,90))
    else:draw.text((88,89),detail,font=font(12),fill=MUTED,anchor='mm')
    return image
