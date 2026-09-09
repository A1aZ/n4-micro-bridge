"""Explicit virtual-HID roundtrip test; never opens physical N4 or emits keys."""
import argparse
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'src'))
from codexmicro_companion import CompanionHidTransport
from codexmicro_sideband import HostRpcEndpoint, MicroMessageDecoder, encode_micro_message


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='open both virtual collections and send only sys.version')
    parser.add_argument('--host-only', action='store_true', help='use an already running companion relay; never open the companion here')
    parser.add_argument('--handshake', action='store_true', help='also query status and read the default keymap over the real virtual HID')
    args=parser.parse_args()
    import hid
    entries=hid.enumerate(0x303a,0x8360)
    primary=[d for d in entries if d.get('usage_page')==0xff00 and d.get('usage')==1]
    if not args.live:
        print(json.dumps({'primaryCount':len(primary),'opened':False}));return 0
    if len(primary)!=1: raise RuntimeError('Exactly one primary Micro collection required')
    host=hid.device();bridge=None
    try:
        host.open_path(primary[0]['path'])
        if not args.host_only: bridge=CompanionHidTransport.open_first()
        endpoint=HostRpcEndpoint();incoming=MicroMessageDecoder();outgoing=MicroMessageDecoder()
        request_id=987654321
        requests=[{'method':'sys.version','id':request_id}]
        if args.handshake:
            requests.extend([
                {'method':'device.status','id':request_id+1},
                {'method':'fs.list','params':{'checksum':True},'id':request_id+2},
                {'method':'fs.readbin','params':{'file':'keymap.json','offset':0,'len':384},'id':request_id+3},
                {'method':'fs.read','params':{'file':'keymap.json'},'id':request_id+4},
            ])
        expected={r['id'] for r in requests};replies={}
        for request in requests:
            for report in encode_micro_message(request):
                if host.write(report)!=64: raise RuntimeError('Primary HID write was incomplete')
        deadline=time.monotonic()+8
        seen=[]
        while time.monotonic()<deadline:
            report=bridge.read_output(timeout_ms=100) if bridge else None
            if report:
                for request in incoming.feed(report):
                    seen.append(request.get('method'))
                    response=endpoint.handle(request)
                    if response is not None:
                        for encoded in encode_micro_message(response): bridge.push_input(encoded)
            data=host.read(64,100)
            if data:
                for response in outgoing.feed(bytes(data)):
                    if response.get('id') in expected:
                        replies[response['id']]=response
                        if len(replies)==len(expected):
                            ok=all('result' in r for r in replies.values())
                            result={'ok':ok,'mode':'virtual-hid-webui-roundtrip' if args.host_only else 'virtual-hid-roundtrip','replyCount':len(replies),'observedMethods':seen,'physicalN4Opened':False}
                            if args.handshake and ok:
                                result['keymapListed']=any(row['name']=='keymap.json' for row in replies[request_id+2]['result'])
                                result['keymapBytes']=replies[request_id+3]['result']['total_size']
                                result['keymapObject']=isinstance(replies[request_id+4]['result'],dict)
                                result['ok']=result['keymapListed'] and result['keymapObject']
                            print(json.dumps(result));return 0 if result['ok'] else 1
        raise TimeoutError('Virtual HID roundtrip timed out; observed methods: '+repr(seen))
    finally:
        if bridge: bridge.close()
        host.close()

if __name__=='__main__':
    try: sys.exit(main())
    except Exception as error:
        print(json.dumps({'ok':False,'error':str(error)}));sys.exit(1)
