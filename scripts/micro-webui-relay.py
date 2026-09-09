import json
import pathlib
import sys
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'src'))
from micro_webui_relay import main
if __name__=='__main__':
    try: sys.exit(main())
    except KeyboardInterrupt: sys.exit(0)
    except Exception as error:
        print(json.dumps({'event':'relay-failed','error':str(error)}),flush=True);sys.exit(1)
