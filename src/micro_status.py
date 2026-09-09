"""Interpret native status colour families for artwork only; unknown stays neutral."""
import colorsys
def micro_status(light,enabled=True):
    # Brightness/effect describe visibility, not chat assignment. A retained
    # colour can still describe the last host status while its lamp is dimmed.
    if not enabled or not light.c:return 'off'
    h,s,_=colorsys.rgb_to_hsv(*light.rgb)
    if s<.18:return 'idle'
    h*=360
    if h<=15 or h>=345:return 'error'
    if 25<=h<=65:return 'attention'
    if 90<=h<=165:return 'complete'
    if 180<=h<=250:return 'working'
    return 'unknown'
