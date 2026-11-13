import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
from kb import KnowledgeBase, dumps
from mcp_server import Session, TOOLS


class StdioClient:
    def __init__(self, root, write=False):
        args=[sys.executable,str(SCRIPTS/'mcp_server.py'),'--root',str(root)]
        if write:args.append('--allow-write')
        self.process=subprocess.Popen(args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8',bufsize=1)
        self.lines=queue.Queue();self.next_id=0
        def read():
            for line in self.process.stdout:self.lines.put(line)
            self.lines.put(None)
        self.reader=threading.Thread(target=read,daemon=True);self.reader.start()
        self.request('initialize',{'protocolVersion':'2025-11-25','clientInfo':{'name':'task-cycle-integration-test','version':'1'},'capabilities':{}})
        self.send({'jsonrpc':'2.0','method':'notifications/initialized'})

    def send(self,message):
        self.process.stdin.write(dumps(message)+'\n');self.process.stdin.flush()

    def request(self,method,params=None):
        self.next_id+=1;rid=self.next_id
        self.send({'jsonrpc':'2.0','id':rid,'method':method,'params':params or {}})
        line=self.lines.get(timeout=10)
        if line is None:raise AssertionError('MCP process exited without response')
        result=json.loads(line)
        if result.get('id')!=rid:raise AssertionError('Unexpected RPC ID or non-protocol stdout')
        return result

    def call(self,name,args):
        return self.request('tools/call',{'name':name,'arguments':args})

    def close(self):
        self.process.stdin.close()
        try:self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:self.process.kill();self.process.wait(timeout=5)
        self.reader.join(timeout=3)
        self.process.stdout.close();self.process.stderr.close()


class TaskCycleMcpTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)/'kb';self.project=Path(self.temp.name)/'CL42'
        (self.root/'data').mkdir(parents=True);self.project.mkdir()
        self.artifact=self.project/'verification.json';self.artifact.write_text('{"scope":"integration_acceptance","passed":true}\n',encoding='utf-8')
        self.outcome={'summary':'CL42局部校验结束','outcome':'success','learnings':[{'title':'CL42零值输入','lesson':'CL42允许0，缺失与0要分开。','scope':'integration_acceptance fixture only','validation':{'method':'local fixture','result':'pass','scope':'no production or hardware acceptance'},'evidence':[{'path':'verification.json','line':1}]}]}

    def tearDown(self):self.temp.cleanup()

    def ready(self,write):
        s=Session(self.root,write)
        s.handle({'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-11-25'}})
        s.handle({'jsonrpc':'2.0','method':'notifications/initialized'})
        return s

    def payload(self,response):
        self.assertNotIn('error',response,response)
        self.assertFalse(response['result'].get('isError'),response)
        actual=response['result']['structuredContent']
        self.assertEqual(json.loads(response['result']['content'][0]['text']),actual)
        return actual

    def test_schema_discovery_and_annotations(self):
        catalog={t['name']:t for t in TOOLS}
        for name in ('kb_task_begin','kb_task_finish'):
            self.assertFalse(catalog[name]['annotations']['readOnlyHint'])
            self.assertFalse(catalog[name]['inputSchema']['additionalProperties'])
        begin=catalog['kb_task_begin'];finish=catalog['kb_task_finish']
        self.assertEqual(begin['inputSchema']['required'],['client','project_root','query'])
        self.assertFalse(begin['annotations']['idempotentHint'])
        outcome=finish['inputSchema']['properties']['outcome'];item=outcome['properties']['learnings']['items']
        self.assertEqual(item['properties']['validation']['properties']['result']['enum'],['pass','fail','partial','not_run','unknown'])
        self.assertEqual(item['properties']['evidence']['items']['required'],['path'])
        self.assertNotIn('status',item['properties'])

    def test_readonly_session_cannot_create_receipt_or_finish(self):
        session=self.ready(False)
        listed=session.handle({'jsonrpc':'2.0','id':2,'method':'tools/list'})['result']['tools']
        self.assertFalse({'kb_task_begin','kb_task_finish'} & {x['name'] for x in listed})
        for name,args in [('kb_task_begin',{'client':'codex','project_root':str(self.project),'query':'CL42'}),('kb_task_finish',{'receipt':'x','outcome':self.outcome})]:
            r=session.handle({'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':name,'arguments':args}})
            self.assertEqual(r['error']['code'],-32602)
        self.assertFalse((self.root/'integrations').exists());self.assertEqual(KnowledgeBase(self.root).revision,0)

    def test_real_stdio_closed_loop_and_cross_client_retrieval(self):
        client=StdioClient(self.root,True)
        try:
            names={x['name'] for x in client.request('tools/list')['result']['tools']}
            self.assertTrue({'kb_task_begin','kb_task_finish'}<=names)
            args={'client':'codex','project_root':str(self.project),'query':'CL42零值','task_id':'producer','budget':1200}
            begun=self.payload(client.call('kb_task_begin',args));self.assertLessEqual(len(dumps(begun)),1200)
            repeated=self.payload(client.call('kb_task_begin',args));self.assertTrue(repeated['replayed']);self.assertEqual(repeated['receipt'],begun['receipt'])
            done=self.payload(client.call('kb_task_finish',{'receipt':begun['receipt'],'outcome':self.outcome}))
            self.assertTrue(done['written']);nid=done['new_lesson_ids'][0]
            node=self.payload(client.call('kb_get',{'id':nid}))['node']
            self.assertEqual(node['status'],'candidate');self.assertIn('integration_acceptance',node['properties']['scope'])
            self.assertEqual(node['evidence'][0]['path'],str(self.artifact.resolve()))
            retry=self.payload(client.call('kb_task_finish',{'receipt':begun['receipt'],'outcome':self.outcome}));self.assertTrue(retry['replayed'])
            consumer=self.payload(client.call('kb_task_begin',{'client':'dsh','project_root':str(self.project),'query':'CL42后续使用','task_id':'consumer'}))
            self.assertIn(nid,[x['id'] for x in consumer['cards']])
            no_new={'summary':'复用了已有知识，无新验证','outcome':'success','learnings':[],'skip_reason':'仅复用先前边界，未新增证据'}
            skipped=self.payload(client.call('kb_task_finish',{'receipt':consumer['receipt'],'outcome':no_new}))
            self.assertFalse(skipped['written']);self.assertEqual(KnowledgeBase(self.root).revision,1)
        finally:client.close()

    def test_real_readonly_stdio_hides_and_denies_task_writes(self):
        client=StdioClient(self.root,False)
        try:
            names={x['name'] for x in client.request('tools/list')['result']['tools']}
            self.assertNotIn('kb_task_begin',names)
            response=client.call('kb_task_begin',{'client':'dsh','project_root':str(self.project),'query':'CL42'})
            self.assertEqual(response['error']['code'],-32602)
            self.assertFalse((self.root/'integrations').exists())
        finally:client.close()

    def test_bad_inputs_and_missing_evidence_are_tool_errors_not_promotions(self):
        session=self.ready(True)
        def invoke(name,args):return session.handle({'jsonrpc':'2.0','id':4,'method':'tools/call','params':{'name':name,'arguments':args}})
        bad=invoke('kb_task_begin',{'client':'dsh','project_root':str(self.project),'query':'CL42','task_id':0})
        self.assertTrue(bad['result']['isError'])
        started=self.payload(invoke('kb_task_begin',{'client':'dsh','project_root':str(self.project),'query':'CL42'}))
        self.outcome['learnings'][0]['evidence'][0]['path']='absent.json'
        missing=invoke('kb_task_finish',{'receipt':started['receipt'],'outcome':self.outcome})
        self.assertTrue(missing['result']['isError']);self.assertEqual(KnowledgeBase(self.root).revision,0)
        self.outcome['learnings'][0]['evidence'][0]['path']='verification.json';self.outcome['learnings'][0]['status']='validated'
        promotion=invoke('kb_task_finish',{'receipt':started['receipt'],'outcome':self.outcome})
        self.assertTrue(promotion['result']['isError']);self.assertEqual(KnowledgeBase(self.root).revision,0)

if __name__=='__main__':unittest.main()
