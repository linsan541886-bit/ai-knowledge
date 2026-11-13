"""Installer checks use temporary homes and a fake DSH composer, never real profiles."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home, self.root = self.base / 'home', self.base / 'knowledge'
        (self.root / 'scripts').mkdir(parents=True)
        (self.root / 'scripts' / 'mcp_server.py').write_text('# fixture\n', encoding='utf-8')
        (self.root / 'scripts' / 'task_cycle.py').write_text('# fixture\n', encoding='utf-8')
        self.python = Path(sys.executable).resolve()
        self.codex = module('install_codex_mcp')

    def codex_config(self, name='.codex', content=b'model = "fixture-model"\r\n# preserve exact bytes\r\n'):
        path = self.home / name / 'config.toml'
        path.parent.mkdir(parents=True)
        path.write_bytes(content)
        return path

    def test_codex_dry_run_does_not_write_and_single_host_is_supported(self):
        path = self.codex_config()
        before = path.read_bytes()
        output = self.root / 'report.json'
        result = self.codex.install(self.home, self.root, self.python, output)
        self.assertEqual(result['status'], 'dry_run')
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(output.exists())
        self.assertFalse((path.parent / 'backups').exists())

    def test_codex_apply_is_idempotent_and_rollback_preserves_original(self):
        path = self.codex_config()
        before = path.read_bytes()
        manifest = self.root / 'installed.json'
        result = self.codex.install(self.home, self.root, self.python, manifest, apply=True)
        self.assertEqual(result['status'], 'installed')
        self.assertTrue(path.read_bytes().startswith(before))
        parsed = self.codex.parse(path.read_bytes())
        self.assertEqual(parsed['model'], 'fixture-model')
        self.assertEqual(parsed['mcp_servers']['local_knowledge']['command'], self.python.as_posix())
        repeated = self.codex.install(self.home, self.root, self.python, self.root / 'repeat.json', apply=True)
        self.assertEqual(repeated['status'], 'already_equal')
        self.codex.rollback(manifest, self.root / 'rollback.json')
        self.assertEqual(path.read_bytes(), before)

    def test_codex_invalid_second_profile_causes_no_config_or_backup_writes(self):
        path = self.codex_config()
        before = path.read_bytes()
        self.codex_config('.codex-cli', b'model = [invalid\n')
        with self.assertRaises(ValueError):
            self.codex.install(self.home, self.root, self.python, self.root / 'report.json', apply=True)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse((path.parent / 'backups').exists())

    def test_codex_rollback_refuses_later_edit(self):
        path = self.codex_config()
        manifest = self.root / 'installed.json'
        self.codex.install(self.home, self.root, self.python, manifest, apply=True)
        later = path.read_bytes() + b'\n# another user edit\n'
        path.write_bytes(later)
        with self.assertRaisesRegex(ValueError, 'changed since installation'):
            self.codex.rollback(manifest, self.root / 'rollback.json')
        self.assertEqual(path.read_bytes(), later)

    def policy_command(self, *args):
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/install_host_policy.py'),
                                 '--root', str(self.root), '--home', str(self.home), *args],
                                capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_policy_preview_install_and_guarded_rollback(self):
        (self.home / '.codex').mkdir(parents=True)
        path = self.home / '.codex' / 'AGENTS.md'
        before = 'Respect existing project rules.\n'.encode()
        path.write_bytes(before)
        self.assertEqual(self.policy_command()['mode'], 'dry_run')
        self.assertEqual(path.read_bytes(), before)
        applied = self.policy_command('--apply')
        self.assertIn('kb_task_begin', path.read_text(encoding='utf-8'))
        self.assertTrue(path.read_bytes().startswith(before))
        self.policy_command('--rollback', applied['manifest'])
        self.assertEqual(path.read_bytes(), before)

    def dsh_fixture(self):
        dsh = module('install_dsh_mcp')
        home = self.home / '.dsh'
        install = self.base / 'app'
        install.mkdir()
        (install / 'DeepSeek Harness.exe').write_bytes(b'fixture never executed')
        for profile in ('desktop', 'headless'):
            directory = home / 'profiles' / profile
            directory.mkdir(parents=True)
            (directory / 'cordis.patch.yml').write_bytes(b'# existing settings\n[]\n')
            (directory / 'package.json').write_bytes(b'{}')
            (directory / 'cordis.yml').write_bytes(b'[]\n')
        dsh.configure(self.root, home, install, self.python)

        def inspect(profile):
            installed = b'mcp-local-knowledge' in (home / 'profiles' / profile / 'cordis.patch.yml').read_bytes()
            hashes = {'existing-model': 'preserved'}
            if installed:
                hashes[dsh.ENTRY_ID] = 'new-entry'
            return {'entry_hashes': hashes, 'entry_order': list(hashes),
                    'knowledge_entry': dsh.ENTRY if installed else None,
                    'mcp_config_valid': True if installed else None, 'skipped_bundles': [],
                    'schema': {'diagnostics_sha256': 'same-diagnostics', 'complete': True}}
        return dsh, home, inspect

    def test_dsh_preview_apply_idempotence_and_rollback_with_fake_composer(self):
        dsh, home, inspect = self.dsh_fixture()
        originals = {p: p.read_bytes() for p in home.rglob('*') if p.is_file()}
        manifest = self.root / 'dsh-install.json'
        with patch.object(dsh, 'inspect_profile', side_effect=inspect):
            preview = dsh.install(manifest)
            self.assertEqual(preview['status'], 'dry_run')
            self.assertFalse(manifest.exists())
            self.assertEqual({p: p.read_bytes() for p in originals}, originals)
            self.assertEqual(dsh.install(manifest, apply=True)['status'], 'installed')
            self.assertEqual(dsh.install(self.root / 'repeat.json', apply=True)['status'], 'already_equal')
        self.assertEqual(dsh.rollback(manifest)['status'], 'rolled_back')
        self.assertEqual({p: p.read_bytes() for p in originals}, originals)

    def test_dsh_rollback_refuses_later_edit_before_restoring_any_profile(self):
        dsh, home, inspect = self.dsh_fixture()
        manifest = self.root / 'dsh-install.json'
        with patch.object(dsh, 'inspect_profile', side_effect=inspect):
            dsh.install(manifest, apply=True)
        path = home / 'profiles/headless/cordis.patch.yml'
        path.write_bytes(path.read_bytes() + b'# later user change\n')
        before = {p: p.read_bytes() for p in home.rglob('*') if p.is_file()}
        with self.assertRaisesRegex(RuntimeError, 'later changes detected'):
            dsh.rollback(manifest)
        self.assertEqual({p: p.read_bytes() for p in before}, before)

    def test_dsh_rejects_profile_path_traversal(self):
        dsh, home, inspect = self.dsh_fixture()
        with self.assertRaises(ValueError):
            dsh.configure(self.root, home, self.base / 'app', self.python, ['../other'])


if __name__ == '__main__':
    unittest.main()
