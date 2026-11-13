import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT=Path(__file__).resolve().parents[1]/'scripts/assemble.py'
sys.path.insert(0,str(SCRIPT.parent))
from kb import KnowledgeBase

class AssemblyTests(unittest.TestCase):
    def fixture(self,root):
        for p in ('data','reports','docs'):(root/p).mkdir()
        inv={'id':'skill:test/demo@abc','logical_id':'skill:test/demo','name':'demo','description':'局部方法','path':'C:/fixture/demo/SKILL.md','locations':['C:/fixture/demo/SKILL.md'],'sha256':'a'*64,'availability':'local_discoverable','content_summary':'演示候选方法','tags':['测试场景'],'scenarios':['检索并核对条件'],'evidence':[]}
        (root/'data/inventory.jsonl').write_text(json.dumps(inv,ensure_ascii=False)+'\n',encoding='utf-8')
        uses=[{'id':'usage:read','logical_id':'skill:test/demo','name':'demo','thread_id':'t1','level':'read','levels_observed':['read'],'status':'success','timestamp':'2026-11-01T00:00:00Z','summary':'文件读取','evidence':[],'outcome':{'status':'unknown'}}, {'id':'usage:declared','logical_id':'skill:test/demo','name':'demo','thread_id':'t2','level':'declared','status':'unknown','timestamp':'2026-11-02T00:00:00Z','summary':'使用宣告','evidence':[],'outcome':{'status':'unknown'}}]
        (root/'data/usage.jsonl').write_text(''.join(json.dumps(u,ensure_ascii=False)+'\n' for u in uses),encoding='utf-8')
        (root/'data/projects.jsonl').write_text('',encoding='utf-8')
        (root/'reports/inventory-coverage.json').write_text(json.dumps({'physical_skill_files':1,'logical_skills':1,'unique_document_hashes':1,'availability_counts':{'local_discoverable':1}}),encoding='utf-8')
        (root/'reports/usage-coverage.json').write_text(json.dumps({'history_cutoff_exclusive_utc':'2026-11-12T00:00:00Z','source_files':2,'complete_bytes':100}),encoding='utf-8')
        for name in ('local-constraints.md','evaluation-method.md'):(root/'docs'/name).write_text('fixture documentation\n',encoding='utf-8')

    def run_build(self,root):
        return subprocess.run([sys.executable,str(SCRIPT),'--source',str(root)],capture_output=True,encoding='utf-8')

    def test_observation_is_not_effectiveness_and_version_is_separate(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);self.fixture(root)
            p=self.run_build(root);self.assertEqual(p.returncode,0,p.stderr)
            kb=KnowledgeBase(root)
            n=kb.get('skill:test/demo')['node'];m=n['properties']['usage_metrics']
            self.assertEqual(m['observed_episodes'],1)
            self.assertEqual(m['declared_only_episodes'],1)
            self.assertEqual(m['successful_tool_episodes'],1)
            self.assertEqual(m['verified_help_episodes'],0)
            self.assertIsNone(n['properties']['effectiveness']['representative_success_rate'])
            self.assertEqual(kb.get('skill:test/demo@abc')['node']['kind'],'skill_version')
            self.assertFalse(any(e['source']=='skill:test/demo@abc' and e['target']=='usage:read' for e in kb.edges.values()))
            self.assertEqual(kb.get('observation:global-summary')['node']['properties']['skills_with_verified_help'],0)

    def test_refuses_to_overwrite_written_generation(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);self.fixture(root)
            self.assertEqual(self.run_build(root).returncode,0)
            kb=KnowledgeBase(root);kb.propose({'expected_revision':0,'nodes':[{'id':'lesson:new','kind':'lesson','title':'New','status':'candidate'}],'edges':[]},'test-agent','new-1')
            before=(root/'data/nodes.jsonl').read_bytes()
            self.assertNotEqual(self.run_build(root).returncode,0)
            self.assertEqual((root/'data/nodes.jsonl').read_bytes(),before)
            self.assertEqual(KnowledgeBase(root).revision,1)

    def test_invalid_import_cannot_publish_bad_baseline(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);self.fixture(root)
            self.assertEqual(self.run_build(root).returncode,0)
            before=(root/'data/nodes.jsonl').read_bytes()
            path=root/'data/inventory.jsonl';inv=json.loads(path.read_text(encoding='utf-8'));inv['id']='skill:$unresolved';path.write_text(json.dumps(inv)+'\n',encoding='utf-8')
            self.assertNotEqual(self.run_build(root).returncode,0)
            self.assertEqual((root/'data/nodes.jsonl').read_bytes(),before)

if __name__=='__main__':unittest.main()
