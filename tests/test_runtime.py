import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from kb import KnowledgeBase, KBError, digest, dumps
from mcp_server import Session
from serve import GraphServer


def node(node_id="lesson:test", status="candidate", **extra):
    n = {"id": node_id, "kind": "lesson", "title": "原始坐标校验", "summary": "坐标桥接需要实际控制器协议和校准验证", "status": status,
         "tags": ["工业视觉", "坐标", "KEBA"], "evidence": [{"path": "evidence/fixture.md", "line": 1}], "properties": {"scope": "synthetic fixture"}}
    n.update(extra)
    return n


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "knowledge"
        (self.root / "data").mkdir(parents=True)
        (self.root / "data" / "nodes.jsonl").write_text(dumps(node("lesson:base", "active")) + "\n", encoding="utf-8")
        (self.root / "data" / "edges.jsonl").write_text("", encoding="utf-8")
        self.kb = KnowledgeBase(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def proposal(self, revision=0, new_node=None):
        return {"expected_revision": revision, "nodes": [new_node or node()], "edges": [{"id": "edge:derives", "source": "lesson:base", "target": "lesson:test", "relation": "supports"}]}

    def review_data(self):
        return {"evidence": [{"path": "tests/result.json", "line": 1}], "validation": {"method": "fixture test", "scope": "parser contract only", "result": "pass", "artifact": "tests/result.json"}}

    def test_baseline_and_chinese_search(self):
        result = self.kb.search("校准验证")
        self.assertEqual(result["items"][0]["id"], "lesson:base")
        self.assertIn("not effectiveness", result["items"][0]["retrieval"]["meaning"])
        self.assertEqual(self.kb.stats()["revision"], 0)

    def test_baseline_manifest_and_tampering(self):
        files = {}
        for name in ("nodes.jsonl", "edges.jsonl"):
            raw = (self.root / "data" / name).read_bytes()
            files[name] = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        (self.root / "data" / "baseline_manifest.json").write_text(dumps({"schema": "ai-knowledge-baseline/v1", "files": files}), encoding="utf-8")
        self.assertTrue(KnowledgeBase(self.root).manifest_verified)
        with (self.root / "data" / "nodes.jsonl").open("ab") as stream:
            stream.write(b"\n")
        with self.assertRaisesRegex(KBError, "hash or byte mismatch"):
            KnowledgeBase(self.root)

    def test_schema_and_dangling_edges(self):
        (self.root / "data" / "edges.jsonl").write_text(dumps({"id": "e:bad", "source": "lesson:base", "target": "missing", "relation": "bad"}) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(KBError, "dangling"):
            KnowledgeBase(self.root)

    def test_proposal_atomic_and_idempotent(self):
        result = self.kb.propose(self.proposal(), "fixture-agent", "request-1")
        self.assertEqual(result["revision"], 1)
        replay = self.kb.propose(self.proposal(), "fixture-agent", "request-1")
        self.assertTrue(replay["replayed"])
        self.assertEqual(len(self.kb.events), 1)
        self.assertEqual(self.kb.expand("lesson:test")["items"][0]["direction"], "incoming")
        self.assertEqual(self.kb.get("lesson:test")["node"]["status"], "candidate")

    def test_idempotency_conflict_and_revision_conflict(self):
        self.kb.propose(self.proposal(), "fixture-agent", "same-key")
        with self.assertRaisesRegex(KBError, "idempotency"):
            self.kb.propose(self.proposal(), "other-agent", "same-key")
        with self.assertRaisesRegex(KBError, "revision conflict"):
            self.kb.propose(self.proposal(), "fixture-agent", "new-key")
        self.assertEqual(KnowledgeBase(self.root).revision, 1)

    def test_no_promotion_by_proposal(self):
        with self.assertRaisesRegex(KBError, "candidate"):
            self.kb.propose(self.proposal(new_node=node(status="validated")), "agent", "bad-promotion")
        self.assertFalse((self.root / "data" / "ledger.jsonl").exists())

    def test_invalid_transaction_does_not_write(self):
        proposal = self.proposal()
        proposal["edges"][0]["target"] = "missing"
        with self.assertRaisesRegex(KBError, "dangling"):
            self.kb.propose(proposal, "agent", "bad-edge")
        self.assertNotIn("lesson:test", self.kb.nodes)
        self.assertEqual(KnowledgeBase(self.root).stats()["nodes"], 1)
        self.assertFalse((self.root / "data" / "writer.lock").exists())

    def test_lifecycle_review_retire_restore_and_history(self):
        self.kb.propose(self.proposal(), "agent", "create")
        self.kb.review("lesson:test", "validated", "fixture passed", self.review_data(), "reviewer", "review", 1)
        self.assertEqual(self.kb.nodes["lesson:test"]["status"], "validated")
        review = self.kb.nodes["lesson:test"]["properties"]["latest_review"]
        self.assertIn("not external truth", review["semantics"])
        self.kb.review("lesson:test", "deprecated", "new revision supersedes", self.review_data(), "reviewer", "retire", 2)
        self.assertNotIn("lesson:test", [n["id"] for n in self.kb.recommend("坐标")["items"]])
        self.assertIn("lesson:test", [n["id"] for n in self.kb.search("坐标", include_inactive=True)["items"]])
        self.kb.review("lesson:test", "active", "scope restored", self.review_data(), "reviewer", "restore", 3)
        self.assertEqual(len(self.kb.history("lesson:test")["events"]), 4)
        self.assertEqual(self.kb.nodes["lesson:test"]["status"], "active")

    def test_validated_requires_scoped_pass(self):
        data = {"evidence": [{"path": "claim.md"}]}
        with self.assertRaisesRegex(KBError, "structured validation"):
            self.kb.review("lesson:base", "validated", "trust me", data, "agent", "v1", 0)
        data = self.review_data()
        data["validation"]["result"] = "partial"
        with self.assertRaisesRegex(KBError, "result=pass"):
            self.kb.review("lesson:base", "validated", "partial", data, "agent", "v2", 0)
        self.assertEqual(self.kb.revision, 0)

    def test_updates_reset_candidate_and_keep_old_history(self):
        self.kb.review("lesson:base", "validated", "verified", self.review_data(), "reviewer", "verify", 0)
        self.kb.propose({"expected_revision": 1, "nodes": [node("lesson:base", "candidate", summary="changed recipe")], "edges": []}, "writer", "change")
        self.assertEqual(self.kb.nodes["lesson:base"]["status"], "candidate")
        self.assertNotIn("latest_review", self.kb.nodes["lesson:base"]["properties"])
        self.assertEqual(len(self.kb.history("lesson:base")["events"]), 2)

    def test_truncated_and_tampered_ledger_fail_closed(self):
        self.kb.propose(self.proposal(), "agent", "create")
        path = self.root / "data" / "ledger.jsonl"
        original = path.read_bytes()
        path.write_bytes(original[:-1])
        with self.assertRaisesRegex(KBError, "incomplete"):
            KnowledgeBase(self.root)
        row = json.loads(original)
        row["actor"] = "different"
        path.write_text(dumps(row) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(KBError, "hash mismatch"):
            KnowledgeBase(self.root)

    def test_baseline_is_bound_to_ledger(self):
        self.kb.propose(self.proposal(), "agent", "create")
        path = self.root / "data" / "nodes.jsonl"
        row = json.loads(path.read_text(encoding="utf-8"))
        row["summary"] = "different baseline"
        path.write_text(dumps(row) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(KBError, "baseline changed"):
            KnowledgeBase(self.root)

    def test_existing_lock_not_removed(self):
        lock = self.root / "data" / "writer.lock"
        lock.write_text('{"pid":123,"token":"someone-else"}', encoding="utf-8")
        with self.assertRaisesRegex(KBError, "writer lock exists"):
            self.kb.propose(self.proposal(), "agent", "create")
        self.assertTrue(lock.exists())

    def test_replace_failure_preserves_ledger(self):
        self.kb.propose(self.proposal(), "agent", "create")
        ledger = self.root / "data" / "ledger.jsonl"
        old = ledger.read_bytes()
        with patch("kb.os.replace", side_effect=OSError("simulated replace failure")):
            with self.assertRaises(OSError):
                self.kb.review("lesson:test", "active", "reason", self.review_data(), "agent", "second", 1)
        self.assertEqual(ledger.read_bytes(), old)
        self.assertFalse((self.root / "data" / "writer.lock").exists())
        self.assertEqual(list((self.root / "data").glob(".ledger.*.tmp")), [])

    def test_concurrent_writers_commit_once(self):
        input_path = self.root / "proposal.json"
        input_path.write_text(dumps(self.proposal()), encoding="utf-8")
        commands = [[sys.executable, str(SCRIPTS / "kb.py"), "--root", str(self.root), "propose", "--file", str(input_path), "--actor", "concurrent", "--idempotency-key", key] for key in ("race-1", "race-2")]
        processes = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for command in commands]
        results = [process.communicate(timeout=20) for process in processes]
        self.assertEqual(sorted(p.returncode for p in processes), [0, 1], results)
        self.assertEqual(KnowledgeBase(self.root).revision, 1)

    def test_context_budget_and_valid_json(self):
        huge = node("lesson:large", summary="非常长的说明" * 2000, properties={"detail": "x" * 30000})
        self.kb.propose({"expected_revision": 0, "nodes": [huge]}, "agent", "large")
        for budget in (256, 300, 1000, 4000, 32000):
            text = self.kb.context("lesson:large", budget)
            self.assertLessEqual(len(text), budget)
            self.assertIsInstance(json.loads(text), dict)
        with self.assertRaises(KBError):
            self.kb.context("lesson:large", 128)

    def test_context_prioritizes_procedure_and_assessment(self):
        huge = node("procedure:bounded", kind="procedure", summary="long summary " * 2000,
                    properties={"raw_import": "bulk " * 10000, "steps": ["校验控制器实际状态", "验证坐标校准", "比较相同输入回放"],
                                "usage_metrics": {"reviewed_help": 2, "unknown_outcome": 5}, "limitations": ["隔离测试不能证明现场验收"]})
        self.kb.propose({"expected_revision": 0, "nodes": [huge]}, "agent", "bounded-assessment")
        payload = json.loads(self.kb.context("procedure:bounded", 1200))
        properties = payload["node"]["properties"]
        self.assertEqual(properties["steps"], huge["properties"]["steps"])
        self.assertEqual(properties["usage_metrics"], huge["properties"]["usage_metrics"])
        self.assertNotIn("raw_import", properties)
        self.assertTrue(payload["truncated"])

    def test_context_budget_across_identifier_lengths(self):
        from kb import bounded
        for length in range(1, 181):
            value = {"revision": 0, "node": node("x" * length, summary="text " * 3000, properties={"steps": ["step " * 100]})}
            for budget in (256, 280, 400):
                encoded = bounded(value, budget)
                self.assertLessEqual(len(encoded), budget)
                self.assertIsInstance(json.loads(encoded), dict)

    def test_recommend_filters_before_top_n_and_deduplicates_versions(self):
        nodes = [node(f"usage:{i}", kind="usage", title="坐标" * 50) for i in range(60)]
        nodes += [node("skill:coordinates", kind="skill", title="坐标", properties={"logical_id": "coordinates", "availability": "available"}),
                  node("version:coordinates", kind="skill_version", title="坐标" * 50, properties={"logical_id": "coordinates"}),
                  node("skill:disabled", kind="skill", title="坐标" * 50, properties={"availability": "disabled"})]
        self.kb.propose({"expected_revision": 0, "nodes": nodes}, "agent", "many-usage")
        hits = [x["id"] for x in self.kb.recommend("坐标")["items"]]
        self.assertIn("skill:coordinates", hits)
        self.assertNotIn("version:coordinates", hits)
        self.assertNotIn("skill:disabled", hits)
        self.assertFalse(any(x.startswith("usage:") for x in hits))

    def routing_fixture(self, activate=True, many=2):
        skill_ids = [f"skill:input-{i}" for i in range(many)]
        rule = {"query_any": ["chrome", "chromium", "浏览器", "browser"], "query_none": ["非浏览器", "non-browser"],
                "exclude_skill_ids": skill_ids, "reason": "当前用户规则：浏览器使用指定插件，不使用通用输入工具"}
        nodes = [node(skill_id, kind="skill", title="Chrome browser 浏览器通用输入", properties={"logical_id": skill_id, "availability": "available"}) for skill_id in skill_ids]
        nodes += [node("skill:browser-plugin", kind="skill", title="浏览器插件 Chrome browser", properties={"availability": "available"}),
                  node("constraint:browser-route", kind="constraint", title="浏览器操作规则", properties={"retrieval_rule": rule})]
        self.kb.propose({"expected_revision": 0, "nodes": nodes}, "fixture", "routing-fixture")
        if activate:
            self.kb.review("constraint:browser-route", "active", "current rule recorded", self.review_data(), "reviewer", "activate-route", 1)
        return skill_ids

    def test_recommend_applies_reviewed_rule_and_keeps_search_visible(self):
        excluded = self.routing_fixture()
        result = self.kb.recommend("当前 Windows 自动控制 CHROME")
        hits = [x["id"] for x in result["items"]]
        self.assertEqual(hits[0], "constraint:browser-route")
        self.assertFalse(set(excluded) & set(hits))
        self.assertEqual(result["applied_constraints"][0]["id"], "constraint:browser-route")
        self.assertEqual({x["id"] for x in result["excluded_candidates"]}, set(excluded))
        self.assertTrue(all(x["reasons"] for x in result["excluded_candidates"]))
        self.assertTrue(set(excluded).issubset({x["id"] for x in self.kb.search("Chrome")["items"]}))
        self.assertEqual(self.kb.recommend("Chrome", limit=1)["items"][0]["id"], "constraint:browser-route")

    def test_retrieval_rule_query_none_and_unrelated_query(self):
        excluded = self.routing_fixture()
        for query in ("非浏览器 Windows 控制", "non-browser window control", "工业视觉坐标校准"):
            result = self.kb.recommend(query)
            self.assertEqual(result["applied_constraints"], [])
            self.assertEqual(result["excluded_candidates"], [])
        # Explicit exception wins even if the same query contains a trigger.
        result = self.kb.recommend("非浏览器 Chrome 背景资料")
        self.assertEqual(result["applied_constraints"], [])
        self.assertTrue(set(excluded).issubset({x["id"] for x in result["items"]}))

    def test_candidate_and_retired_constraints_do_not_route(self):
        excluded = self.routing_fixture(activate=False)
        result = self.kb.recommend("Chrome")
        self.assertEqual(result["applied_constraints"], [])
        self.assertTrue(set(excluded).issubset({x["id"] for x in result["items"]}))
        self.kb.review("constraint:browser-route", "active", "enable", self.review_data(), "reviewer", "active", 1)
        self.assertTrue(self.kb.recommend("Chrome")["applied_constraints"])
        self.kb.review("constraint:browser-route", "deprecated", "no longer applicable", self.review_data(), "reviewer", "retired", 2)
        result = self.kb.recommend("Chrome")
        self.assertEqual(result["applied_constraints"], [])
        self.assertTrue(set(excluded).issubset({x["id"] for x in result["items"]}))

    def test_retrieval_rule_exclusions_are_bounded_and_schema_is_limited(self):
        self.routing_fixture(many=8)
        result = self.kb.recommend("Chrome")
        self.assertEqual(len(result["excluded_candidates"]), 5)
        self.assertEqual(result["excluded_total"], 8)
        bad = node("constraint:bad", kind="constraint", properties={"retrieval_rule": {"query_any": [""], "exclude_skill_ids": ["skill:x"], "reason": "bad"}})
        with self.assertRaisesRegex(KBError, "nonempty"):
            self.kb.propose({"expected_revision": 2, "nodes": [bad]}, "agent", "invalid-rule")
        bad["properties"]["retrieval_rule"] = {"query_any": ["test"], "exclude_skill_ids": ["skill:x"], "reason": "bad", "execute": "command"}
        with self.assertRaisesRegex(KBError, "unknown retrieval_rule"):
            self.kb.propose({"expected_revision": 2, "nodes": [bad]}, "agent", "invalid-operation")

    def test_short_cards_keep_only_selected_usage_metrics(self):
        metrics = {"observed_episodes": 20, "read_episodes": 12, "executed_episodes": 8, "observed_workspaces": 3,
                   "reviewed_outcome_episodes": 2, "verified_help_real_task_episodes": 1, "last_observed": "2026-11-12",
                   "all_episode_ids": ["usage:1"] * 100, "extra_evaluation": {"long": "details"}}
        self.kb.propose({"expected_revision": 0, "nodes": [node("skill:metrics", kind="skill", properties={"usage_metrics": metrics})]}, "agent", "card-metrics")
        hit = self.kb.search("", kind="skill")["items"][0]
        self.assertEqual(len(hit["usage_metrics"]), 7)
        self.assertNotIn("all_episode_ids", hit["usage_metrics"])
        self.assertEqual(self.kb.get("skill:metrics")["node"]["properties"]["usage_metrics"], metrics)

    def test_small_context_keeps_scope_before_usage_popularity(self):
        small = node("skill:small", kind="skill", summary="用于离线回放；不证明现场验收。",
                     properties={"usage_metrics": {"observed_episodes": 900, "executed_episodes": 800, "last_observed": "2026-11-12"},
                                 "limitations": ["需检查真实设备状态"], "prerequisites": ["可访问回放日志"],
                                 "effectiveness": {"scope": "离线测试"}, "steps": ["读取日志", "同输入比对"]})
        self.kb.propose({"expected_revision": 0, "nodes": [small]}, "agent", "small-context")
        text = self.kb.context("skill:small", 600)
        self.assertLessEqual(len(text), 600)
        value = json.loads(text)
        self.assertEqual(value["node"]["summary"], small["summary"])
        self.assertEqual(value["node"]["properties"]["limitations"], small["properties"]["limitations"])
        self.assertNotEqual(set(value["node"]["properties"]), {"usage_metrics"})

    def outcome_fixture(self):
        rows = [
            node("usage:unknown", kind="usage", summary="没有帮助证据，不计入 verified_help。", properties={"outcome": {"status": "unknown", "scope": "unknown", "validation_level": "not_reviewed"}}),
            node("usage:real", kind="usage", properties={"outcome": {"status": "verified_help", "scope": "real_artifact", "validation_level": "real_tool_result"}}),
            node("usage:synthetic", kind="usage", properties={"outcome": {"status": "verified_help", "scope": "synthetic_validation", "validation_level": "synthetic_forward_test"}}),
            node("skill:outcome-prose", kind="skill", summary="verified_help 是一个指标名", properties={"outcome": {"status": "verified_help"}}),
        ]
        self.kb.propose({"expected_revision": 0, "nodes": rows}, "agent", "outcome-fixture")

    def test_outcome_filter_uses_structured_status_not_negative_prose(self):
        self.outcome_fixture()
        ordinary = {x["id"] for x in self.kb.search("verified_help")["items"]}
        self.assertIn("usage:unknown", ordinary)
        result = self.kb.search("verified_help", outcome="verified_help", exclude_synthetic=True)
        self.assertEqual([x["id"] for x in result["items"]], ["usage:real"])
        self.assertEqual(result["items"][0]["outcome"], {"status": "verified_help", "scope": "real_artifact", "validation_level": "real_tool_result"})
        self.assertEqual(self.kb.search("", kind="skill", outcome="verified_help")["total"], 0)
        self.assertEqual(self.kb.search("", outcome="unknown")["total"], 1)
        with self.assertRaisesRegex(KBError, "invalid outcome"):
            self.kb.search("", outcome="success")
        with self.assertRaisesRegex(KBError, "boolean"):
            self.kb.search("", exclude_synthetic="true")

    def test_cli_http_and_mcp_expose_outcome_filter(self):
        from mcp_server import call
        self.outcome_fixture()
        raw = subprocess.check_output([sys.executable, str(SCRIPTS / "kb.py"), "--root", str(self.root), "search", "verified_help", "--outcome", "verified_help", "--exclude-synthetic"], timeout=10)
        self.assertEqual([x["id"] for x in json.loads(raw)["items"]], ["usage:real"])
        mcp = call(self.root, "kb_search", {"query": "verified_help", "outcome": "verified_help", "exclude_synthetic": True})
        self.assertEqual([x["id"] for x in mcp["items"]], ["usage:real"])
        server = GraphServer(("127.0.0.1", 0), self.root)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            connection.request("GET", "/api/search?q=verified_help&outcome=verified_help&exclude_synthetic=true")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            result = json.loads(response.read())
            self.assertEqual([x["id"] for x in result["items"]], ["usage:real"])
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_payload_paths_are_data_not_file_operations(self):
        attack = node("../harmless", properties={"command": "Remove-Item", "path": "../../outside.txt"}, evidence=[{"path": "../../outside.txt", "line": 1}])
        self.kb.propose({"expected_revision": 0, "nodes": [attack]}, "test", "path-as-data")
        self.assertIn("../harmless", self.kb.nodes)
        self.assertFalse((self.root.parent / "outside.txt").exists())
        self.assertEqual(self.kb.nodes["../harmless"]["properties"]["command"], "Remove-Item")

    def test_export_relocates_graph_and_excludes_source_logs(self):
        self.kb.propose(self.proposal(), "agent", "create")
        (self.root / "sources").mkdir()
        (self.root / "sources" / "raw.jsonl").write_text("raw secret transcript", encoding="utf-8")
        (self.root / "scripts").mkdir()
        shutil.copy2(SCRIPTS / "kb.py", self.root / "scripts" / "kb.py")
        out = self.root.parent / "portable.zip"
        result = self.kb.export(out)
        self.assertFalse(result["includes_raw_source_logs"])
        restored = self.root.parent / "restored"
        with zipfile.ZipFile(out) as archive:
            self.assertNotIn("sources/raw.jsonl", archive.namelist())
            archive.extractall(restored)
        relocated = KnowledgeBase(restored)
        self.assertEqual(relocated.get("lesson:test"), self.kb.get("lesson:test"))
        output = subprocess.check_output([sys.executable, str(restored / "scripts" / "kb.py"), "overview"], timeout=10)
        self.assertEqual(json.loads(output)["stats"]["revision"], 1)

    def test_http_api_host_boundary_and_no_file_access(self):
        (self.root / "ui").mkdir()
        (self.root / "ui" / "index.html").write_text("<html>test fixture</html>", encoding="utf-8")
        server = GraphServer(("127.0.0.1", 0), self.root)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            connection.request("GET", "/api/overview")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())["stats"]["nodes"], 1)
            connection.request("GET", "/api/stats", headers={"Host": "evil.example"})
            response = connection.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            connection.request("GET", "/../../data/nodes.jsonl")
            response = connection.getresponse()
            self.assertEqual(response.status, 404)
            response.read()
            connection.request("POST", "/api/propose", body=b"{}")
            response = connection.getresponse()
            self.assertEqual(response.status, 405)
            response.read()
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_http_reload_observes_committed_ledger(self):
        server = GraphServer(("127.0.0.1", 0), self.root)
        try:
            self.assertEqual(server.knowledge().revision, 0)
            self.kb.propose(self.proposal(), "agent", "create")
            self.assertEqual(server.knowledge().revision, 1)
        finally:
            server.server_close()

    def test_mcp_session_readonly_and_tools(self):
        session = Session(self.root)
        self.assertIn("error", session.handle({"jsonrpc": "2.0", "id": 0, "method": "tools/list"}))
        result = session.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}})
        self.assertEqual(result["result"]["protocolVersion"], "2025-11-25")
        self.assertIsNone(session.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        tools = session.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        self.assertNotIn("kb_propose", [t["name"] for t in tools])
        result = session.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "kb_search", "arguments": {"query": "工业视觉"}}})
        self.assertFalse(result["result"]["isError"])
        self.assertEqual(result["result"]["structuredContent"]["items"][0]["id"], "lesson:base")

    def test_mcp_stdio_real_process_with_write(self):
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "kb_propose", "arguments": {"proposal": self.proposal(), "actor": "mcp-test", "idempotency_key": "mcp-write"}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "kb_health", "arguments": {}}},
        ]
        raw = ("\n".join(dumps(m) for m in messages) + "\n").encode("utf-8")
        process = subprocess.run([sys.executable, str(SCRIPTS / "mcp_server.py"), "--root", str(self.root), "--allow-write"], input=raw, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
        self.assertEqual(process.returncode, 0, process.stderr)
        results = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual(len(results), 3)
        self.assertFalse(results[1]["result"]["isError"])
        self.assertEqual(results[2]["result"]["structuredContent"]["revision"], 1)


if __name__ == "__main__":
    unittest.main()
