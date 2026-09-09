"""Capture real N4 reports using the StreamDock Python SDK.
Run from the repository root after installing the SDK dependencies.
"""
import sys, time, traceback
from pathlib import Path
sdk = Path(__file__).parents[1] / 'upstream' / 'StreamDock-Device-SDK' / 'Python-SDK' / 'src'
sys.path.insert(0, str(sdk))
from StreamDock.DeviceManager import DeviceManager

def main():
    manager = DeviceManager()
    devices = manager.enumerate()
    n4 = next((d for d in devices if d.vendor_id == 0x6602 and d.product_id == 0x1001), None)
    if n4 is None:
        print('N4 (6602:1001) not found'); return 2
    print(f'found {n4.path} {n4.vendor_id:04x}:{n4.product_id:04x}')
    def raw(device, data):
        b = bytes(data)
        print('RAW', len(b), b[:32].hex(' '), 'code=', hex(b[9]) if len(b)>9 else '-', 'state=', hex(b[10]) if len(b)>10 else '-')
    n4.set_raw_read_callback(raw)
    n4.open()
    # Match SDK lifecycle: set report sizes/device options, then heartbeat.
    n4.init()
    print('listening; press/rotate N4, Ctrl+C to stop')
    try:
        while True: time.sleep(.2)
    except KeyboardInterrupt: pass
    finally: n4.close()
    return 0
if __name__ == '__main__':
    try: raise SystemExit(main())
    except Exception:
        traceback.print_exc(); raise SystemExit(1)
