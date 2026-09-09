"""Cross-runtime file RPC checks; no USB, driver or persistent files involved.

Numeric/boolean boundaries are based on fw/src/codex/wire.rs parse_u32,
parse_bool and handle_request in the checked-out openmicrokbd reference.
Invalid JSON is deliberately rejected rather than repeating its lax scanner.
"""
import base64
import os
import json
import pathlib
import shutil
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from codexmicro_sideband import HostRpcEndpoint, MicroMessageDecoder, encode_micro_message
from codexmicro_files import MicroFileStore


def call(method, params=None, ident=1):
    return {"method": method, "params": params, "id": ident}


def cases():
    seed = {"smart_actions.json": base64.b64encode(b'{"v":12345}').decode()}
    scenarios = []
    for length in [None, True, False, "3", "", [], {}, -1, 2.9, 0, 3, 4294967296]:
        scenarios.append({"name": f"length {length!r}", "initial": seed, "requests": [
            call("fs.readbin", {"file": "smart_actions.json", "len": length})]})
    for offset in [None, True, "3", -1, 2.9, 4294967296]:
        scenarios.append({"name": f"offset {offset!r}", "initial": seed, "requests": [
            call("fs.readbin", {"file": "smart_actions.json", "offset": offset, "len": 3})]})
    for completed in [True, False, 1, 0, "true", "false", [], {}, None]:
        scenarios.append({"name": f"completed {completed!r}", "requests": [
            call("fs.writebin", {"file": "smart_actions.json", "data": "e30=", "completed": completed}),
            call("fs.read", {"file": "smart_actions.json"}, 2)]})
    for body in [b'{"x":NaN}', b'{"x":Infinity}', b'{"x":-Infinity}', b'{"x":1e400}', b'{"x":"\xc3("}', b'{}', b'[]']:
        scenarios.append({"name": repr(body), "initial": {"smart_actions.json":base64.b64encode(body).decode()},
                          "requests": [call("fs.read", {"file":"smart_actions.json"})]})
    for params in [None, [], {}, {"data":"e30="}]:
        scenarios.append({"name": f"missing filename {params!r}", "requests":[call("fs.writebin", params)]})
    scenarios.append({"name":"stream, abort, retry, delete", "requests":[
        call("fs.writebin", {"file":"smart_actions.json","data":"eyJhIjo=","completed":False}),
        call("fs.writebin", {"file":"smart_actions.json","data":"!!","completed":True},2),
        call("fs.writebin", {"file":"smart_actions.json","data":"e30=","completed":True},3),
        call("fs.read", {"file":"smart_actions.json"},4),
        call("fs.delete", {"file":"smart_actions.json"},5),
        call("fs.read", {"file":"smart_actions.json"},6),
    ]})
    return scenarios


class MicroFileParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scenarios = cases()
        node = os.environ.get('MIRABOX_TEST_NODE') or shutil.which("node")
        if not node:
            raise unittest.SkipTest("Node required for cross-runtime parity")
        runner = r"""
const fs=require('node:fs');
const {MicroEndpoint,MicroFileStore,encode,Decoder}=require('./src/micro-protocol.cjs');
const scenarios=JSON.parse(fs.readFileSync(0,'utf8'));
const results=scenarios.map(s=>{
  const files=new MicroFileStore(Object.fromEntries(Object.entries(s.initial||{}).map(([n,b])=>[n,Buffer.from(b,'base64')])));
  const endpoint=new MicroEndpoint({files});
  const roundtrip=value=>{const d=new Decoder();return encode(value).flatMap(report=>d.feed(report))[0];};
  return {replies:s.requests.map(r=>roundtrip(endpoint.handle(roundtrip(r)))),files:files.snapshot()};
});
process.stdout.write('PARITY_RESULT:'+JSON.stringify(results));
"""
        result = subprocess.run([node, "-e", runner], cwd=ROOT, input=json.dumps(cls.scenarios),
                                capture_output=True, text=True, timeout=30, check=True)
        cls.node_results = json.loads(result.stdout.split("PARITY_RESULT:", 1)[1])

    def test_file_rpc_replies_and_state_match_between_runtimes(self):
        for scenario, actual in zip(self.scenarios, self.node_results):
            with self.subTest(case=scenario["name"]):
                files=MicroFileStore({name:base64.b64decode(body) for name,body in scenario.get("initial",{}).items()})
                endpoint=HostRpcEndpoint(files=files)
                def roundtrip(value):
                    decoder=MicroMessageDecoder()
                    messages=[m for report in encode_micro_message(value) for m in decoder.feed(report)]
                    self.assertEqual(len(messages),1)
                    return messages[0]
                expected={"replies":[roundtrip(endpoint.handle(roundtrip(r))) for r in scenario["requests"]], "files":files.snapshot()}
                self.assertEqual(actual, expected)

    def test_reference_numeric_and_boolean_boundaries(self):
        results={s["name"]: r for s,r in zip(self.scenarios,self.node_results)}
        for name in ["length None", "length True", "length '3'", "length 4294967296"]:
            with self.subTest(case=name):
                data=results[name]["replies"][0]["result"]["data"]
                self.assertEqual(base64.b64decode(data),b'{"v":12345}')
        self.assertEqual(base64.b64decode(results["length 2.9"]["replies"][0]["result"]["data"]),b'{"')
        for value in [True,1]:
            self.assertEqual(results[f"completed {value!r}"]["replies"][1]["result"],{})
        for value in [False,0,"true","false",[],{},None]:
            self.assertEqual(results[f"completed {value!r}"]["replies"][1]["error"]["code"],-2)

    def test_invalid_json_files_and_missing_write_names_have_explicit_errors(self):
        for scenario,result in zip(self.scenarios,self.node_results):
            if scenario["name"].startswith("missing filename"):
                self.assertEqual(result["replies"][0]["error"]["code"],-3)
            if scenario["name"].startswith('b\'{"x":'):
                self.assertEqual(result["replies"][0]["error"]["code"],-2)


if __name__ == "__main__":
    unittest.main()
