"""Bounded native JPEG frame cache; no device I/O or filesystem access."""
from collections import OrderedDict
from io import BytesIO
from PIL import Image

class NativeFrameCache:
    def __init__(self,max_entries=256,max_bytes=16*1024*1024):
        self.frames=OrderedDict();self.max_entries=max_entries;self.max_bytes=max_bytes
        self.size=0;self.hits=0;self.misses=0
    def get(self,key,render):
        if key in self.frames:
            self.hits+=1;self.frames.move_to_end(key);return self.frames[key]
        self.misses+=1
        image=render()
        if image.size not in ((112,112),(176,112)):raise ValueError('Unexpected N4 frame dimensions')
        # SDK N4 formats specify 180 degrees, no flips. Transpose is lossless.
        native=image.convert('RGB').transpose(Image.Transpose.ROTATE_180)
        # Match the official N4 file encoder defaults, including chroma
        # sampling. Do not assume its firmware accepts every valid JPEG mode.
        data=BytesIO();native.save(data,format='JPEG')
        frame=data.getvalue()
        self.frames[key]=frame;self.size+=len(frame)
        while len(self.frames)>self.max_entries or self.size>self.max_bytes:
            _,old=self.frames.popitem(last=False);self.size-=len(old)
        return frame
