import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
from kb import KBError, KnowledgeBase, dumps
from task_cycle import TaskCycle


class CycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / 'knowledge'
        self.project = self.base / 'AX19-fixture'
        self.project.mkdir(); (self.root / 'data').mkdir(parents=True)
        self.evidence = self.project / 'validation.json'
        self.evidence.write_text('{"test":"fixture-only","result":"pass"}\n', encoding='utf-8')
        method = {'id':'procedure:test','kind':'procedure','title':'AX19校验方法','summary':'先确认输入契约，再按fixture范围验证','status':'active','tags':['AX19'],'properties':{'prerequisites':['输入来源可定位'],'scope':'fixture-only'}}
        (self.root / 'data/nodes.jsonl').write_text(dumps(method)+'\n',encoding='utf-8')
        (self.root / 'data/edges.jsonl').write_text('',encoding='utf-8')
        self.cycle = TaskCycle(self.root)

    def tearDown(self): self.temp.cleanup()

    def begin(self, task='task-one', client='codex', budget=5000):
        return self.cycle.begin(client, self.project, 'AX19 校验', task, budget)

    def outcome(self, title='AX19 值域边界', result='pass'):
        return {'summary':'本地受控测试完成','outcome':'success','learnings':[{'title':title,'lesson':'AX19的有效值是包含零的非负整数','scope':'integration_acceptance fixture only','when_to_use':['处理AX19输入'],'validation':{'method':'python local fixture','result':result,'scope':'integration_acceptance, no production validation'},'evidence':[{'path':'validation.json','line':1}]}]}

    def test_begin_bounded_private_and_idempotent(self):
        first=self.begin(budget=700)
        self.assertLessEqual(len(dumps(first)),700)
        receipt=json.loads(Path(first['receipt']).read_text(encoding='utf-8'))
        self.assertNotIn('query', receipt)
        self.assertIn('query_sha256',receipt)
        self.assertEqual(receipt['candidate_node_ids'],['procedure:test'])
        self.assertEqual(receipt['delivered_node_ids'],[c['id'] for c in first['cards']])
        self.assertEqual(receipt['selected_node_ids'],receipt['delivered_node_ids'])
        self.assertFalse((self.root/'data/ledger.jsonl').exists())
        repeated=self.begin(budget=5000)
        self.assertTrue(repeated['replayed'])
        self.assertEqual(repeated['receipt'],first['receipt'])
        with self.assertRaisesRegex(KBError,'different begin'):
            self.cycle.begin('codex',self.project,'different query','task-one')

    def test_omitted_cards_never_become_retrieval_provenance(self):
        start=self.begin(budget=512)
        self.assertEqual(start['cards'],[])
        receipt=json.loads(Path(start['receipt']).read_text(encoding='utf-8'))
        self.assertEqual(receipt['candidate_node_ids'],['procedure:test'])
        self.assertEqual(receipt['delivered_node_ids'],[])
        end=self.cycle.finish(start['receipt'],self.outcome())
        kb=KnowledgeBase(self.root)
        self.assertEqual(kb.nodes[end['new_lesson_ids'][0]]['properties']['retrieved_node_ids'],[])
        self.assertFalse(any(e['relation']=='retrieval_context' for e in kb.edges.values()))

    def test_budget_retry_accumulates_only_emitted_cards_and_freezes_at_finish(self):
        first=self.begin(budget=512);large=self.begin(budget=5000);small=self.begin(budget=512)
        self.assertEqual(first['receipt'],large['receipt']);self.assertEqual(small['cards'],[])
        receipt=json.loads(Path(first['receipt']).read_text(encoding='utf-8'))
        self.assertEqual(receipt['delivered_node_ids'],['procedure:test'])
        end=self.cycle.finish(first['receipt'],self.outcome());kb=KnowledgeBase(self.root)
        self.assertEqual(kb.nodes[end['new_lesson_ids'][0]]['properties']['retrieved_node_ids'],['procedure:test'])
        self.assertEqual([e['target'] for e in kb.edges.values() if e['relation']=='retrieval_context'],['procedure:test'])
        frozen=Path(first['receipt']).read_bytes();revision=kb.revision
        self.begin(budget=512)
        self.assertEqual(Path(first['receipt']).read_bytes(),frozen)
        self.assertEqual(KnowledgeBase(self.root).revision,revision)

    def test_legacy_selected_ids_are_candidates_not_assumed_delivery(self):
        def legacy(task):
            start=self.begin(task,budget=512);path=Path(start['receipt'])
            receipt=json.loads(path.read_text(encoding='utf-8'))
            receipt.pop('delivered_node_ids');receipt.pop('candidate_node_ids')
            receipt['selected_node_ids']=['procedure:test'];path.write_text(dumps(receipt),encoding='utf-8')
            return start
        first=legacy('legacy-direct')
        end=self.cycle.finish(first['receipt'],self.outcome());kb=KnowledgeBase(self.root)
        self.assertEqual(kb.nodes[end['new_lesson_ids'][0]]['properties']['retrieved_node_ids'],[])
        self.assertFalse(any(e['relation']=='retrieval_context' for e in kb.edges.values()))
        retry=legacy('legacy-retry');self.begin('legacy-retry',budget=512)
        receipt=json.loads(Path(retry['receipt']).read_text(encoding='utf-8'))
        self.assertEqual(receipt['candidate_node_ids'],['procedure:test'])
        self.assertEqual(receipt['delivered_node_ids'],[])
        self.assertIn('unknown',receipt['legacy_delivery_note'])
        self.begin('legacy-retry',budget=5000)
        receipt=json.loads(Path(retry['receipt']).read_text(encoding='utf-8'))
        self.assertEqual(receipt['delivered_node_ids'],[c['id'] for c in receipt['cards']])

    def test_finish_candidate_evidence_and_other_client_retrieval(self):
        start=self.begin(); end=self.cycle.finish(start['receipt'],self.outcome())
        kb=KnowledgeBase(self.root)
        lesson=kb.nodes[end['new_lesson_ids'][0]]
        self.assertEqual(lesson['status'],'candidate')
        self.assertEqual(lesson['evidence'][0]['sha256'],hashlib.sha256(self.evidence.read_bytes()).hexdigest())
        self.assertEqual(lesson['properties']['validation']['result'],'pass')
        second=self.cycle.begin('dsh',self.project,'AX19新任务','next-client')
        self.assertIn(lesson['id'],[c['id'] for c in second['cards']])
        matched=next(c for c in second['cards'] if c['id']==lesson['id'])
        self.assertIn('integration_acceptance',matched['scope'])
        self.assertEqual(sum(n['kind']=='usage' for n in kb.nodes.values()),0)

    def test_failure_is_preserved_without_promoting(self):
        raw=self.outcome(result='fail');raw['outcome']='failure';raw['learnings'][0]['failure_reason']='当前fixture零值被错误拒绝'
        result=self.cycle.finish(self.begin()['receipt'],raw)
        p=KnowledgeBase(self.root).nodes[result['new_lesson_ids'][0]]
        self.assertEqual(p['status'],'candidate');self.assertEqual(p['properties']['task_outcome'],'failure')
        self.assertIn('零值',p['properties']['failure_reason'])

    def test_no_value_does_not_write_and_cannot_change_finished_input(self):
        start=self.begin()
        outcome={'summary':'没有新的可复用知识','outcome':'success','learnings':[],'skip_reason':'只确认已知状态，没有新增事实'}
        result=self.cycle.finish(start['receipt'],outcome)
        self.assertFalse(result['written']);self.assertEqual(KnowledgeBase(self.root).revision,0)
        self.assertTrue(self.cycle.finish(start['receipt'],outcome)['replayed'])
        with self.assertRaisesRegex(KBError,'different finish'):
            self.cycle.finish(start['receipt'],self.outcome())

    def test_missing_evidence_and_self_promotion_rejected_before_pending(self):
        start=self.begin();raw=self.outcome();raw['learnings'][0]['evidence'][0]['path']='absent.json'
        with self.assertRaisesRegex(KBError,'does not exist'): self.cycle.finish(start['receipt'],raw)
        raw=self.outcome();raw['learnings'][0]['status']='validated'
        with self.assertRaisesRegex(KBError,'unknown learning fields'):self.cycle.finish(start['receipt'],raw)
        self.assertNotIn('finish_input_hash',json.loads(Path(start['receipt']).read_text(encoding='utf-8')))
        self.assertEqual(KnowledgeBase(self.root).revision,0)

    def test_receipt_path_injection_and_identity_rejected(self):
        with self.assertRaises(KBError):self.cycle.finish(self.base/'outside.json',self.outcome())
        start=self.begin();path=Path(start['receipt']);data=json.loads(path.read_text(encoding='utf-8'));data['receipt_id']='0'*32;path.write_text(dumps(data),encoding='utf-8')
        with self.assertRaisesRegex(KBError,'identity'):self.cycle.finish(path,self.outcome())

    def test_finish_is_idempotent_and_exact_duplicate_does_not_rewrite(self):
        start=self.begin();raw=self.outcome();first=self.cycle.finish(start['receipt'],raw);rev=KnowledgeBase(self.root).revision
        second=self.cycle.finish(start['receipt'],raw)
        self.assertTrue(second['replayed']);self.assertEqual(KnowledgeBase(self.root).revision,rev)
        other=self.begin('another-task','dsh');duplicate=self.cycle.finish(other['receipt'],raw)
        self.assertFalse(duplicate['written']);self.assertEqual(duplicate['existing_lesson_ids'],first['new_lesson_ids'])
        self.assertEqual(KnowledgeBase(self.root).revision,rev)

    def test_new_evidence_preserves_existing_lesson_and_observation(self):
        first=self.cycle.finish(self.begin()['receipt'],self.outcome());nid=first['new_lesson_ids'][0]
        original=KnowledgeBase(self.root).nodes[nid]
        self.evidence.write_text('{"result":"fail","new":"counterexample"}\n',encoding='utf-8')
        changed=self.outcome(result='fail');changed['outcome']='partial'
        result=self.cycle.finish(self.begin('new-evidence')['receipt'],changed)
        kb=KnowledgeBase(self.root);self.assertEqual(kb.nodes[nid],original)
        self.assertEqual(len(result['new_observation_ids']),1)
        self.assertTrue(kb.nodes[result['new_observation_ids'][0]]['properties']['needs_review'])
        repeated=self.cycle.finish(self.begin('repeat-evidence')['receipt'],changed)
        self.assertFalse(repeated['written'])

    def test_recovery_after_commit_before_receipt_update(self):
        start=self.begin();raw=self.outcome();real=self.cycle.write_receipt
        def fail_finished(path,data):
            if data.get('status')=='finished':raise OSError('simulated receipt replace failure')
            return real(path,data)
        with patch.object(self.cycle,'write_receipt',side_effect=fail_finished):
            with self.assertRaises(OSError):self.cycle.finish(start['receipt'],raw)
        self.assertEqual(KnowledgeBase(self.root).revision,1)
        result=self.cycle.finish(start['receipt'],raw)
        self.assertTrue(result['replayed']);self.assertEqual(KnowledgeBase(self.root).revision,1)
        self.assertEqual(json.loads(Path(start['receipt']).read_text(encoding='utf-8'))['status'],'finished')

    def test_concurrent_new_tasks_append_without_overwriting(self):
        a=self.begin('concurrent-a');b=self.begin('concurrent-b','dsh')
        files=[]
        for i,title in enumerate(('AX19 A','AX19 B')):
            path=self.project/f'outcome-{i}.json';path.write_text(dumps(self.outcome(title)),encoding='utf-8');files.append(path)
        processes=[subprocess.Popen([sys.executable,str(SCRIPTS/'task_cycle.py'),'--root',str(self.root),'finish','--receipt',x['receipt'],'--file',str(path)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8') for x,path in zip((a,b),files)]
        results=[]
        for p in processes:
            out,err=p.communicate(timeout=20);self.assertEqual(p.returncode,0,err+out);results.append(json.loads(out))
        kb=KnowledgeBase(self.root);self.assertEqual(kb.revision,2)
        self.assertTrue(all(result['new_lesson_ids'][0] in kb.nodes for result in results))
        self.assertEqual(sum(n['kind']=='project' for n in kb.nodes.values()),1)

if __name__=='__main__':unittest.main()
