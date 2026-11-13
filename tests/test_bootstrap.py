"""Portable initialization must never replace an existing knowledge generation."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from bootstrap import initialize
from kb import KnowledgeBase


class BootstrapTests(unittest.TestCase):
    def test_example_graph_is_searchable_and_expandable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = initialize(root)
            self.assertEqual(result["nodes"], 3)
            kb = KnowledgeBase(root)
            self.assertTrue(kb.health()["ok"])
            self.assertTrue(kb.manifest_verified)
            self.assertTrue(kb.search("发布", kind="procedure")["items"])
            self.assertEqual(kb.get("scenario:example-release-review")["relation_count"], 1)
            self.assertTrue(all(n["status"] == "candidate" for n in kb.nodes.values()))

    def test_existing_written_store_is_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            initialize(root)
            KnowledgeBase(root).propose({"expected_revision": 0, "nodes": [
                {"id": "lesson:later", "kind": "lesson", "title": "Later", "status": "candidate"}
            ], "edges": []}, "test", "test-new-knowledge")
            before = {p.name: p.read_bytes() for p in (root / "data").iterdir()}
            with self.assertRaisesRegex(ValueError, "existing data"):
                initialize(root)
            self.assertEqual(before, {p.name: p.read_bytes() for p in (root / "data").iterdir()})
            self.assertEqual(KnowledgeBase(root).revision, 1)

    def test_empty_store_and_cli(self):
        with tempfile.TemporaryDirectory() as temp:
            result = subprocess.run([sys.executable, str(SCRIPTS / "bootstrap.py"),
                                     "--root", temp, "--empty"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["nodes"], 0)
            self.assertTrue(KnowledgeBase(Path(temp)).health()["ok"])

    def test_cli_unicode_root_under_legacy_console_encoding(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "知识库"
            env = dict(os.environ, PYTHONIOENCODING="cp1252", PYTHONUTF8="0")
            result = subprocess.run([sys.executable, str(SCRIPTS / "bootstrap.py"),
                                     "--root", str(root)], capture_output=True,
                                    encoding="utf-8", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            # Windows CI may supply TEMP through an 8.3 alias (e.g. RUNNER~1).
            # Verify the created directory's identity, not its spelling.
            self.assertTrue(root.samefile(json.loads(result.stdout)["root"]))
            self.assertTrue(KnowledgeBase(root).health()["ok"])

    def test_explicit_inventory_build_report_without_history(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake_home = root / "home"
            for host in (".codex", ".codex-cli"):
                (fake_home / host).mkdir(parents=True)
                (fake_home / host / "config.toml").write_text("invalid TOML [", encoding="utf-8")
            source = root / "import"
            generation = root / "generation"
            commands = [
                ["inventory.py", "--home", str(fake_home), "--only-extra-roots", "--extra-root",
                 str(SCRIPTS.parent / "examples" / "skills"), "--out", str(source)],
                ["assemble.py", "--source", str(source), "--out", str(generation)],
                ["report.py", "--root", str(generation)],
            ]
            for script, *args in commands:
                result = subprocess.run([sys.executable, str(SCRIPTS / script), *args],
                                        capture_output=True, encoding="utf-8")
                self.assertEqual(result.returncode, 0, result.stderr)
            kb = KnowledgeBase(generation)
            self.assertTrue(kb.health()["ok"])
            self.assertNotIn("constraint:windows-browser-policy", kb.nodes)
            summary = kb.get("observation:global-summary")["node"]["properties"]
            self.assertIsNone(summary["history_cutoff"])
            self.assertEqual(summary["observed_episodes"], 0)
            self.assertTrue((generation / "reports" / "评估报告.md").is_file())
            coverage = json.loads((source / "reports" / "inventory-coverage.json").read_text(encoding="utf-8"))
            self.assertIsNone(coverage["config_evidence"]["sha256"])
            self.assertIsNone(coverage["config_evidence"]["additional_config_sha256"])
            self.assertEqual(coverage["config_evidence"]["inspected_fields"], [])

    def test_history_requires_explicit_selection(self):
        with tempfile.TemporaryDirectory() as temp:
            result = subprocess.run([sys.executable, str(SCRIPTS / "mine_usage.py"),
                                     "--root", temp], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Select --source", result.stderr)
            self.assertFalse((Path(temp) / "data").exists())


if __name__ == "__main__":
    unittest.main()
