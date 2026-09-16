"""Connect the virtual Micro companion to an existing WebUI/N4 bridge.

No physical N4 access, driver installation, automatic reconnect or synthetic
keypresses. Explicit --live is required. Old WebUI input is discarded once on
startup so historical presses cannot unexpectedly reach the application.
"""
from __future__ import annotations
import argparse
import ctypes
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from codexmicro_companion import CompanionHidTransport
from codexmicro_sideband import validate_report


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('Local WebUI relay does not follow redirects')


class WebUiTransientError(RuntimeError):
    """A local WebUI request failed in a way that may recover on its own."""

    def __init__(self, route, cause):
        self.route = route
        self.cause = cause
        super().__init__(f'{route}: {cause}')


class LocalApi:
    def __init__(self, url):
        parsed=urllib.parse.urlsplit(url)
        if parsed.scheme!='http' or parsed.hostname not in ('127.0.0.1','localhost','::1') or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('','/'):
            raise ValueError('WebUI URL must be a local HTTP origin')
        self.base=url.rstrip('/')
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())

    def request(self, route, body=None):
        data=None if body is None else json.dumps(body).encode('utf-8')
        request=urllib.request.Request(self.base+route,data=data,headers={'Content-Type':'application/json'})
        try:
            with self.opener.open(request,timeout=3) as response:
                raw=response.read(4*1024*1024+1)
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as error:
            # A local Node request can briefly time out while the event loop is
            # flushing a device frame. Keep the relay alive and let Relay apply
            # bounded backoff. POST callers must still decide whether a retry
            # is safe because the request may have been accepted already.
            raise WebUiTransientError(route, error) from error
        if len(raw)>4*1024*1024: raise ValueError('WebUI response too large')
        return json.loads(raw)


class Relay:
    def __init__(self, api, transport, emit=lambda value: None):
        self.api=api;self.transport=transport;self.emit=emit
        self.received=0;self.sent=0;self.discarded=0
        self._webui_failures=0
        self._webui_deferred=False
        self._webui_next_retry=0.0
        self._last_webui_notice=0.0

    @staticmethod
    def _is_transient_webui_error(error):
        return isinstance(error,(WebUiTransientError,urllib.error.URLError,socket.timeout,TimeoutError,ConnectionError))

    def _defer_webui(self, route, error):
        self._webui_failures+=1
        self._webui_deferred=True
        # Back off quickly, but never long enough to make the UI feel dead.
        delay=min(0.75,0.05*(2**min(self._webui_failures-1,4)))
        self._webui_next_retry=time.monotonic()+delay
        now=time.monotonic()
        if now-self._last_webui_notice>=5:
            self._last_webui_notice=now
            self.emit({
                'event':'webui-deferred',
                'route':getattr(error,'route',route),
                'error':str(getattr(error,'cause',error)),
                'failures':self._webui_failures,
                'retryInMs':round(delay*1000),
            })

    def _mark_webui_recovered(self):
        if self._webui_deferred:
            self.emit({'event':'webui-recovered','failures':self._webui_failures})
        self._webui_failures=0
        self._webui_deferred=False
        self._webui_next_retry=0.0

    def discard_backlog(self):
        value=self.api.request('/api/transport/input?drain=1&limit=512')
        self.discarded=len(value['reports'])
        self.emit({'event':'backlog-discarded','reports':self.discarded})

    def tick(self):
        if time.monotonic()<self._webui_next_retry:
            return False
        report=self.transport.read_output(timeout_ms=30)
        if report is not None:
            try:
                value=self.api.request('/api/transport/output',{'reports':[list(validate_report(report))],'source':'companion-hid'})
            except Exception as error:
                # Never replay an ambiguous POST: the WebUI may have already
                # accepted it before the response timed out. The next tick
                # resumes with a fresh device read.
                if self._is_transient_webui_error(error):
                    self._defer_webui('/api/transport/output',error)
                    return False
                raise
            self.received+=1
            for message in value.get('messages',[]):
                # Log method/ID only, not keymap or other RPC payload contents.
                self.emit({'event':'host-request','method':message.get('method'),'id':message.get('id')})
            for reply in value.get('replies',[]):
                if reply.get('error'): self.emit({'event':'rpc-error','id':reply.get('id'),'error':reply['error']})
        try:
            value=self.api.request('/api/transport/input?drain=1&limit=64')
        except Exception as error:
            if self._is_transient_webui_error(error):
                self._defer_webui('/api/transport/input?drain=1&limit=64',error)
                return False
            raise
        for row in value['reports']:
            # Do not retry ambiguous writes: replay could duplicate a user action.
            self.transport.push_input(validate_report(bytes(row['bytes'])))
            self.sent+=1
        self._mark_webui_recovered()
        return True


def acquire_singleton():
    if os.name!='nt': raise RuntimeError('Live relay currently targets the Windows virtual driver')
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateMutexW.argtypes=[ctypes.c_void_p,ctypes.c_int,ctypes.c_wchar_p]
    kernel.CreateMutexW.restype=ctypes.c_void_p
    kernel.CloseHandle.argtypes=[ctypes.c_void_p]
    kernel.CloseHandle.restype=ctypes.c_int
    handle=kernel.CreateMutexW(None,False,'Local\\MiraboxCodexMicroWebUIRelay')
    if not handle: raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error()==183:
        kernel.CloseHandle(handle);raise RuntimeError('Another WebUI Micro relay is already running')
    return lambda:kernel.CloseHandle(handle)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--webui-url',default='http://127.0.0.1:8792')
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--duration',type=float,default=0,help='seconds; 0 runs until stopped')
    args=parser.parse_args(argv)
    if args.duration<0: parser.error('duration must be nonnegative')
    emit=lambda value:print(json.dumps(value,ensure_ascii=True),flush=True)
    api=LocalApi(args.webui_url)
    state=api.request('/api/state')
    if state.get('server',{}).get('apiVersion',0)<2 or not state.get('features',{}).get('transportApi'):
        raise RuntimeError('WebUI transport API v2 is required')
    if not args.live:
        emit({'ok':True,'mode':'check-only','physicalN4Opened':False,'companionOpened':False});return 0
    release=acquire_singleton();transport=None
    try:
        transport=CompanionHidTransport.open_first()
        relay=Relay(api,transport,emit)
        relay.discard_backlog()
        emit({'event':'relay-started','pid':os.getpid(),'physicalN4Opened':False,'webuiUrl':api.base})
        started=time.monotonic();last=started
        while not args.duration or time.monotonic()-started<args.duration:
            if not relay.tick():
                time.sleep(0.05)
            if time.monotonic()-last>=5:
                emit({'event':'heartbeat','received':relay.received,'sent':relay.sent});last=time.monotonic()
        emit({'event':'relay-stopped','received':relay.received,'sent':relay.sent})
    finally:
        if transport: transport.close()
        release()
    return 0
