"""Tests for false attribution and observable-only skill-history extraction."""
import contextlib
import hashlib
import importlib.util
import io
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

SPEC=importlib.util.spec_from_file_location('mine_usage',pathlib.Path(__file__).parents[1]/'scripts/mine_usage.py')
M=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)
SKILL='C:/Users/test/.codex/skills/example/SKILL.md'

def meta(ident,stamp='2026-01-01T00:00:00.000Z'):
    return {'timestamp':stamp,'type':'session_meta','payload':{'id':ident,'timestamp':stamp,'cwd':'/fictional-project'}}

def call(ident='call-1',stamp='2026-01-02T00:00:00.000Z'):
    return {'timestamp':stamp,'type':'response_item','payload':{'type':'function_call','call_id':ident,'name':'exec_command','arguments':json.dumps({'cmd':"Get-Content '"+SKILL+"'"})}}

class UsageExtractionTests(unittest.TestCase):
    def run_source(self,files,source_family='codex_live'):
        with tempfile.TemporaryDirectory() as tmp:
            root=pathlib.Path(tmp);src=root/'sources';src.mkdir()
            for name,lines in files.items():
                (src/name).write_bytes(b''.join((json.dumps(x)+'\n').encode() for x in lines))
            args=['mine_usage.py','--root',str(root),'--source',source_family+'='+str(src),'--cutoff','2026-01-10T00:00:00.000Z']
            with mock.patch.object(sys,'argv',args),contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):M.main()
            rows=[json.loads(x) for x in (root/'data/usage.jsonl').read_text(encoding='utf-8').splitlines()]
            coverage=json.loads((root/'reports/usage-coverage.json').read_text(encoding='utf-8'))
            return rows,coverage

    def test_catalog_and_output_copies_do_not_count(self):
        injected={'timestamp':'2026-01-02T00:00:00.000Z','type':'response_item','payload':{'type':'message','role':'developer','content':[{'type':'input_text','text':"Use Get-Content '"+SKILL+"'"}]}}
        output={'timestamp':'2026-01-02T00:00:00.000Z','type':'response_item','payload':{'type':'function_call_output','call_id':'unrelated','output':json.dumps(call())}}
        reasoning={'timestamp':'2026-01-02T00:00:00.000Z','type':'response_item','payload':{'type':'reasoning','summary':"Get-Content '"+SKILL+"'"}}
        rows,_=self.run_source({'a.jsonl':[meta('a'),injected,output,reasoning]})
        self.assertEqual(rows,[])

    def test_first_meta_keeps_child_identity(self):
        rows,_=self.run_source({'a.jsonl':[meta('child'),meta('parent','2025-12-01T00:00:00.000Z'),call()]})
        self.assertEqual(rows[0]['thread_id'],'child')

    def test_pre_creation_inherited_call_excluded(self):
        rows,c=self.run_source({'a.jsonl':[meta('child','2026-01-03T00:00:00.000Z'),call()]})
        self.assertEqual(rows,[])
        self.assertEqual(c['excluded_types']['copied_events_before_file_session_created'],1)

    def test_duplicate_call_in_export_is_one_episode(self):
        rows,c=self.run_source({'a.jsonl':[meta('a'),call()],'b.jsonl':[meta('a'),call()]})
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['operation_count'],1)
        self.assertEqual(c['deduplicated_call_records'],1)

    def test_current_audit_excluded_at_cutoff(self):
        rows,_=self.run_source({'a.jsonl':[meta('a'),call(stamp='2026-01-10T00:00:00.000Z')]})
        self.assertEqual(rows,[])

    def test_exit_success_is_not_verified_help(self):
        end={'timestamp':'2026-01-02T00:00:01.000Z','type':'response_item','payload':{'type':'function_call_output','call_id':'call-1','output':'Process exited with code 0'}}
        rows,_=self.run_source({'a.jsonl':[meta('a'),call(),end]})
        self.assertEqual(rows[0]['status'],'success')
        self.assertEqual(rows[0]['outcome']['status'],'unknown')
        self.assertIsNone(rows[0]['skill_id'])
        self.assertEqual(rows[0]['skill_version_at_use'],'unknown')

    def test_quoted_read_verb_and_variable_path_not_reads(self):
        self.assertEqual(M.classify("rg 'Get-Content' '"+SKILL+"'",'shell'),[])
        self.assertEqual(M.classify("Get-Content 'C:/Users/test/.codex/skills/$s/SKILL.md'",'shell'),[])
        self.assertEqual(M.classify("Set-Content '"+SKILL+"' 'new'",'shell'),[])

    def test_discarded_read_is_flagged(self):
        c=call();c['payload']['arguments']=json.dumps({'cmd':"Get-Content '"+SKILL+"' | Out-Null"})
        rows,cov=self.run_source({'a.jsonl':[meta('a'),c]})
        self.assertEqual(rows[0]['discarded_output_operations'],1)
        self.assertEqual(cov['episodes_with_only_discarded_output'],1)

    def test_script_run_distinct_from_document_read(self):
        self.assertEqual(M.classify("python 'C:/Users/test/.codex/skills/example/scripts/check.py'",'shell'),[('C:/Users/test/.codex/skills/example/SKILL.md','executed')])

    def test_plugin_helper_without_skill_ancestor_not_skill_use(self):
        self.assertEqual(M.classify("node 'C:/Users/test/.codex/plugins/cache/provider/browser/26.123/bin/control.js'",'shell'),[])

    def test_wrapper_parses_command_not_document_creation(self):
        code='await tools.exec_command({cmd:'+json.dumps("Get-Content '"+SKILL+"'")+'});'
        self.assertEqual(M.extract_commands('functions.exec',code)[0][0],"Get-Content '"+SKILL+"'")
        self.assertEqual(M.extract_commands('functions.exec','await tools.apply_patch("'+SKILL+'");'),[])


if __name__=='__main__':unittest.main()
