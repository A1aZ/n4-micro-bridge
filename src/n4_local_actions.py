"""User-operated N4 knob actions, never a general command/keypress runner.

Only the live physical-report callback uses this module. WebUI simulation or
replay responses alone cannot execute desktop input. No focus change, mouse
move, click, keypress, command execution or approval action is implemented.
"""
import ctypes
import os
from pathlib import PureWindowsPath
import threading


def scroll_allowed(image, pointer_in_target, modifiers_down):
    return PureWindowsPath(image or '').name.lower() in {'chatgpt.exe','codex.exe'} and pointer_in_target and not modifiers_down


class WindowsWheel:
    def __init__(self):
        if os.name!='nt': raise RuntimeError('Desktop scrolling requires Windows')
        self.user=ctypes.WinDLL('user32',use_last_error=True)
        self.kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        class Point(ctypes.Structure): _fields_=[('x',ctypes.c_int32),('y',ctypes.c_int32)]
        class Mouse(ctypes.Structure):
            _fields_=[('dx',ctypes.c_int32),('dy',ctypes.c_int32),('data',ctypes.c_uint32),('flags',ctypes.c_uint32),('time',ctypes.c_uint32),('extra',ctypes.c_size_t)]
        class Union(ctypes.Union): _fields_=[('mouse',Mouse)]
        class Input(ctypes.Structure): _fields_=[('kind',ctypes.c_uint32),('value',Union)]
        self.Point=Point;self.Input=Input
        self.user.GetForegroundWindow.restype=ctypes.c_void_p
        self.user.GetWindowThreadProcessId.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint32)]
        self.user.GetCursorPos.argtypes=[ctypes.POINTER(Point)]
        self.user.WindowFromPoint.argtypes=[Point];self.user.WindowFromPoint.restype=ctypes.c_void_p
        self.user.GetAncestor.argtypes=[ctypes.c_void_p,ctypes.c_uint];self.user.GetAncestor.restype=ctypes.c_void_p
        self.user.GetAsyncKeyState.argtypes=[ctypes.c_int];self.user.GetAsyncKeyState.restype=ctypes.c_int16
        self.user.SendInput.argtypes=[ctypes.c_uint,ctypes.POINTER(Input),ctypes.c_int];self.user.SendInput.restype=ctypes.c_uint
        self.kernel.OpenProcess.argtypes=[ctypes.c_uint32,ctypes.c_int,ctypes.c_uint32];self.kernel.OpenProcess.restype=ctypes.c_void_p
        self.kernel.QueryFullProcessImageNameW.argtypes=[ctypes.c_void_p,ctypes.c_uint32,ctypes.c_wchar_p,ctypes.POINTER(ctypes.c_uint32)]
        self.kernel.CloseHandle.argtypes=[ctypes.c_void_p]

    def scroll(self,direction):
        if type(direction) is not int or direction not in (-1,1):raise ValueError('scroll direction must be -1 or 1')
        window=self.user.GetForegroundWindow();pid=ctypes.c_uint32()
        self.user.GetWindowThreadProcessId(window,ctypes.byref(pid))
        handle=self.kernel.OpenProcess(0x1000,False,pid.value)
        image=''
        if handle:
            try:
                buffer=ctypes.create_unicode_buffer(32768);size=ctypes.c_uint32(len(buffer))
                if self.kernel.QueryFullProcessImageNameW(handle,0,buffer,ctypes.byref(size)):image=buffer.value
            finally:self.kernel.CloseHandle(handle)
        point=self.Point()
        inside=bool(self.user.GetCursorPos(ctypes.byref(point)) and self.user.GetAncestor(self.user.WindowFromPoint(point),2)==window)
        modifiers=any(self.user.GetAsyncKeyState(key)&0x8000 for key in [0x10,0x11,0x12,0x5b,0x5c])
        if not scroll_allowed(image,inside,modifiers):return {'executed':False,'reason':'请将 Codex 置于前台，鼠标放在聊天区，并松开修饰键'}
        # Re-check foreground immediately before sending; never steal focus.
        if self.user.GetForegroundWindow()!=window:return {'executed':False,'reason':'前台窗口已改变'}
        event=self.Input();event.kind=0;event.value.mouse.flags=0x0800
        event.value.mouse.data=(-120*direction)&0xffffffff
        if self.user.SendInput(1,ctypes.byref(event),ctypes.sizeof(event))!=1:raise OSError('Windows did not accept the scroll event')
        return {'executed':True,'reason':'滚轮事件已发送；滚动区域取决于鼠标位置'}


class PhysicalKnobActions:
    def __init__(self,adapter,wheel=None):
        self.adapter=adapter;self.wheel=wheel;self.brightness=100
        self.brightness_applied=False
        self.executed=0;self.ignored=0;self.errors=0;self.last_action=None;self.last_reason=None
        self.lock=threading.RLock()

    def handle_record(self,record):
        # This callback is registered only by n4-webui-bridge --live. Reject
        # status/ACK packets and failed HTTP responses even in that path.
        if not isinstance(record,dict):return
        decoded=record.get('decoded');post=record.get('post')
        if not record.get('inputPacket') or not isinstance(decoded,dict) or decoded.get('kind')!='knob_rotate' or not isinstance(post,dict) or not post.get('ok'):return
        response=record['post'].get('response')
        actions=response.get('localActions',[]) if isinstance(response,dict) else []
        if not isinstance(actions,list) or len(actions)>4:return
        with self.lock:
            for action in actions:
                if not isinstance(action,dict):continue
                kind=action.get('type');self.last_action=kind
                try:
                    if kind=='brightness' and type(action.get('delta')) is int and action['delta'] in (-5,5):
                        value=max(10,min(100,self.brightness+action['delta']))
                        result=self.adapter.set_brightness(value)
                        if isinstance(result,(int,float)) and not isinstance(result,bool) and result<0:raise OSError('N4 brightness write failed')
                        self.brightness=value;self.brightness_applied=True;self.executed+=1;self.last_reason=f'N4 亮度 {value}%'
                    elif kind=='scroll' and type(action.get('direction')) is int and action['direction'] in (-1,1):
                        if self.wheel is None:self.wheel=WindowsWheel()
                        result=self.wheel.scroll(action['direction'])
                        if result['executed']:self.executed+=1
                        else:self.ignored+=1
                        self.last_reason=result['reason']
                    elif kind=='setup-required':
                        self.ignored+=1;self.last_reason='请先在 Codex 中配置摇杆左右为降低/增加推理强度'
                    else:
                        self.ignored+=1;self.last_reason='不支持的本地动作'
                except Exception as error:
                    self.errors+=1;self.last_reason=str(error)

    def snapshot(self):
        with self.lock:return {'executed':self.executed,'ignored':self.ignored,'errors':self.errors,'brightness':self.brightness,'brightnessApplied':self.brightness_applied,'lastAction':self.last_action,'lastReason':self.last_reason}
