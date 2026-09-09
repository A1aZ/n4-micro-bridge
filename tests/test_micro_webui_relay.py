import pathlib
import sys
import unittest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'src'))
from micro_webui_relay import LocalApi,Relay
from codexmicro_sideband import encode_micro_message

class MicroWebuiRelayTests(unittest.TestCase):
    def test_local_origin_only(self):
        for url in ['https://127.0.0.1:8792','http://example.com','http://user@localhost','http://localhost/path','http://localhost?x=1']:
            with self.assertRaises(ValueError):LocalApi(url)
        self.assertEqual(LocalApi('http://127.0.0.1:8792').base,'http://127.0.0.1:8792')

    def test_old_input_discard_and_bidirectional_forwarding(self):
        request=encode_micro_message({'method':'sys.version','id':1})[0]
        reply=encode_micro_message({'id':1,'result':{'version':'test'}})[0]
        calls=[];sent=[];events=[]
        class Api:
            def request(self,route,body=None):
                calls.append((route,body))
                if 'limit=512' in route:return {'reports':[{'bytes':list(reply)}]}
                if route.endswith('/output'):return {'messages':[{'method':'sys.version','id':1,'private':'not logged'}],'replies':[]}
                return {'reports':[{'bytes':list(reply)}]}
        class Transport:
            def read_output(self,timeout_ms):return request
            def push_input(self,report):sent.append(report)
        relay=Relay(Api(),Transport(),events.append)
        relay.discard_backlog();self.assertEqual(sent,[]);self.assertEqual(relay.discarded,1)
        relay.tick();self.assertEqual(sent,[reply]);self.assertEqual(relay.received,1)
        self.assertEqual(calls[1][1]['reports'],[list(request)])
        self.assertNotIn('private',str(events))

    def test_failed_write_is_not_replayed(self):
        calls=[]
        class Api:
            def request(self,route,body=None):return {'reports':[{'bytes':list(encode_micro_message({'id':1})[0])}]}
        class Transport:
            def read_output(self,timeout_ms):return None
            def push_input(self,report):calls.append(report);raise OSError('failed')
        relay=Relay(Api(),Transport())
        with self.assertRaises(OSError):relay.tick()
        self.assertEqual(len(calls),1)
