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
    title={'micro':'','scroll':'聊天滚动','reasoning':'推理强度','brightness':'亮度'}[mode]
    detail={'micro':'功能由 Codex 决定','scroll':'鼠标放聊天区','reasoning':'− / +' if knob.get('reasoningConfigured') else '待绑定左右方向','brightness':f'{brightness}%' if brightness is not None else '旋转调节 · 每格5%'}[mode]
    touch_action=knob.get('touchAction')
    if touch_action is None and fast_touch:
        touch_action={'targetKey':'ACT06','eyebrow':'快捷','label':'加速','scope':'strip'}
    if not knob.get('enabled',True):detail='已停用'
    image=Image.new('RGB',(176,112),BG);draw=ImageDraw.Draw(image)
    panel=knob.get('panel')
    if not isinstance(panel,dict):
        panel={'kind':'clock','clock':clock} if index==0 and isinstance(clock,dict) else {'kind':'action','action':touch_action} if touch_action else {'kind':'knob'}
    kind=panel.get('kind')
    if kind=='clock' and isinstance(panel.get('clock'),dict):
        value=panel['clock']
        draw.rounded_rectangle((1,1,174,77),radius=8,fill=BG,outline=(46,59,76))
        draw.text((12,15),'当前时间',font=font(10),fill=MUTED,anchor='lm')
        date_text=str(value.get('date','')) + (f" · {value['weekday']}" if value.get('weekday') else '')
        draw.text((164,15),date_text,font=font(9),fill=MUTED,anchor='rm')
        draw.text((12,53),str(value.get('time','')),font=font(34),fill=FG,anchor='lm')
    elif kind=='agents' and isinstance(panel.get('agents'),list):
        draw.rounded_rectangle((1,1,174,77),radius=8,fill=BG,outline=(46,59,76))
        start=panel.get('start',0) if isinstance(panel.get('start'),int) else 0
        agents=panel['agents'][:3]
        draw.text((12,15),'Agent 状态',font=font(10),fill=MUTED,anchor='lm')
        draw.text((164,15),f'{start+1:02}–{start+len(agents):02}',font=font(9),fill=MUTED,anchor='rm')
        for offset,agent in enumerate(agents):
            value=agent if isinstance(agent,dict) else {}
            cx=31+offset*57
            rgb=int(value.get('c',0) or 0)&0xffffff
            visibility=max(0.0,min(1.0,float(value.get('b',0) or 0))) if int(value.get('e',0) or 0)!=0 else 0.0
            colour=((rgb>>16)&255,(rgb>>8)&255,rgb&255) if rgb else (65,78,94)
            fill=tuple(round(channel*(.35+.65*visibility)) for channel in colour) if rgb else colour
            draw.ellipse((cx-8,31,cx+8,47),fill=fill,outline=colour if rgb else (89,104,123),width=2)
            draw.text((cx,62),f'{start+offset+1:02}',font=font(12),fill=FG,anchor='mm')
    elif kind=='action' and (isinstance(panel.get('action'),dict) or touch_action):
        # The N4 reports only one hardware code for this whole 176x112 strip,
        # without touch coordinates.  Only this upper panel advertises the
        # real action; the bottom rail is a passive physical-knob legend.
        action=panel.get('action') if isinstance(panel.get('action'),dict) else touch_action
        draw.rounded_rectangle((1,1,174,77),radius=8,fill=(35,30,20),outline=(124,98,40))
        draw.text((12,15),f"{action.get('eyebrow','快捷')}操作",font=font(10),fill=(184,163,108),anchor='lm')
        draw.polygon([(48,15),(32,42),(43,42),(38,66),(62,35),(51,35)],fill=(255,210,90))
        draw.text((82,42),str(action.get('label','加速')),font=font(22),fill=FG,anchor='lm')
        draw.text((82,62),'触摸触发',font=font(10),fill=(184,163,108),anchor='lm')
    else:
        draw.rounded_rectangle((1,1,174,77),radius=8,fill=BG,outline=(46,59,76))
        symbol=asset(CATALOG['knobIcons'][mode]).resize((26,26),Image.Resampling.LANCZOS)
        image.paste(symbol,(14,25),symbol)
        if title:draw.text((51,39),title,font=font(16),fill=FG,anchor='lm')
        draw.text((88,62),detail,font=font(11),fill=MUTED,anchor='mm')
    draw.rounded_rectangle((1,84,174,110),radius=7,fill=BG,outline=(46,59,76))
    symbol=asset(CATALOG['knobIcons'][mode]).resize((16,16),Image.Resampling.LANCZOS)
    image.paste(symbol,(8,89),symbol)
    knob_hint=f'旋钮 {index+1} · 已停用' if not knob.get('enabled',True) else f'旋钮 {index+1} · {title}' if title else f'旋钮 {index+1}'
    knob_value='' if not knob.get('enabled',True) else {
        'micro':'',
        'scroll':'',
        'reasoning':'− / +' if knob.get('reasoningConfigured') else '待绑定',
        'brightness':f'{brightness}%' if brightness is not None else '5% / 格',
    }[mode]
    draw.text((31,97),knob_hint,font=font(10),fill=MUTED,anchor='lm')
    if knob_value:draw.text((165,97),knob_value,font=font(11),fill=FG,anchor='rm')
    return image
