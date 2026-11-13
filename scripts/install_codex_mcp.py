#!/usr/bin/env python3
"""Install only mcp_servers.local_knowledge, with byte backups and guarded rollback.

Python 3.11+ (tomllib). Config contents and credentials are never printed.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import tomllib
import uuid

SERVER = 'local_knowledge'
SCHEMA = 'codex-local-knowledge-mcp-install/v1'


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def parse(raw):
    return tomllib.loads(raw.decode('utf-8-sig'))


def require(test, message):
    if not test:
        raise ValueError(message)


def atomic_write(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def save_report(path, report):
    atomic_write(path, (json.dumps(report, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))


def without_server(config):
    value = deepcopy(config)
    servers = value.get('mcp_servers')
    if isinstance(servers, dict):
        servers.pop(SERVER, None)
        if not servers:
            value.pop('mcp_servers', None)
    return value


def install(home, root, python, report_path, apply=False):
    require(python.is_file(), 'Selected Python executable does not exist.')
    entry = root / 'scripts' / 'mcp_server.py'
    require(entry.is_file(), 'Knowledge MCP server script does not exist.')
    expected = {'command': python.as_posix(), 'args': [entry.as_posix(), '--root', root.as_posix(), '--allow-write']}
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    batch = uuid.uuid4().hex[:8]
    rows = []
    plans = []
    report = {'schema': SCHEMA, 'created_at_utc': utcnow(), 'operation': 'install', 'server_name': SERVER,
              'root': str(root), 'server': expected, 'status': 'preparing', 'targets': rows,
              'secret_handling': 'Only config hashes, changed paths and this non-secret server table are reported. Byte backups remain in each Codex home.',
              'scope': 'Persistent MCP registration; does not prove model invocation or task-cycle completion.'}
    for name in ('.codex', '.codex-cli'):
        config = home / name / 'config.toml'
        if not config.is_file():
            continue
        before = config.read_bytes()
        doc = parse(before)
        found = doc.get('mcp_servers', {}).get(SERVER)
        require(found is None or found == expected, 'Existing local_knowledge differs; refusing to overwrite: ' + str(config))
        row = {'config': str(config), 'before_sha256': sha(before), 'before_bytes': len(before), 'changed': found is None}
        rows.append(row)
        if found is not None:
            row.update({'after_sha256': sha(before), 'after_bytes': len(before), 'changed_fields': [], 'status': 'already_equal'})
            continue
        nl = '\r\n' if b'\r\n' in before else '\n'
        addition = nl + nl + '[mcp_servers.' + SERVER + ']' + nl
        addition += 'command = ' + json.dumps(expected['command']) + nl
        addition += 'args = ' + json.dumps(expected['args']) + nl
        after = before + addition.encode('utf-8')
        after_doc = parse(after)
        require(after_doc.get('mcp_servers', {}).get(SERVER) == expected, 'New server parse mismatch.')
        require(without_server(after_doc) == without_server(doc), 'Unexpected config change outside target server.')
        # Preserve the original bytes as a prefix as well as semantic equality.
        require(after[:len(before)] == before, 'Original bytes were not preserved.')
        backup = home / name / 'backups' / ('local-knowledge-mcp-' + stamp + '-' + batch) / 'config.toml'
        row.update({'backup': str(backup), 'backup_sha256': sha(before), 'after_sha256': sha(after), 'after_bytes': len(after),
                    'changed_fields': ['mcp_servers.' + SERVER], 'unrelated_config_semantics_unchanged': True,
                    'original_bytes_preserved_as_prefix': True, 'status': 'backed_up'})
        plans.append((config, before, after, row))
    require(bool(rows), 'No existing .codex/config.toml or .codex-cli/config.toml under --home.')
    if not apply:
        report['status'] = 'dry_run'
        for row in rows:
            if row['changed']:
                row['status'] = 'would_install'
        return report
    # Validate every target before creating backups or changing configuration.
    for config, before, after, row in plans:
        backup = Path(row['backup'])
        backup.parent.mkdir(parents=True, exist_ok=False)
        with backup.open('xb') as stream:
            stream.write(before)
            stream.flush()
            os.fsync(stream.fileno())
        require(sha(backup.read_bytes()) == sha(before), 'Backup verification failed.')
    report['status'] = 'prepared'
    save_report(report_path, report)
    committed = []
    try:
        for config, before, after, row in plans:
            require(config.read_bytes() == before, 'Config changed concurrently; refusing to overwrite: ' + str(config))
            atomic_write(config, after)
            require(config.read_bytes() == after, 'Post-write byte verification failed: ' + str(config))
            row['status'] = 'installed'
            committed.append((config, before, after, row))
        report['status'] = 'installed' if plans else 'already_equal'
        report['completed_at_utc'] = utcnow()
        save_report(report_path, report)
        return report
    except Exception as exc:
        # Restore only files still equal to this process's exact write.
        for config, before, after, row in reversed(committed):
            if config.read_bytes() == after:
                atomic_write(config, before)
                row['status'] = 'restored_after_failed_install'
            else:
                row['status'] = 'changed_after_install_not_overwritten'
        report['status'] = 'failed'
        report['error_type'] = type(exc).__name__
        report['error'] = str(exc)
        save_report(report_path, report)
        raise


def rollback(manifest, output):
    report = json.loads(manifest.read_text(encoding='utf-8-sig'))
    require(report.get('schema') == SCHEMA, 'Unrecognized install manifest.')
    targets = []
    for row in report.get('targets', []):
        if not row.get('changed'):
            continue
        config, backup = Path(row['config']), Path(row['backup'])
        before = backup.read_bytes()
        require(sha(before) == row['before_sha256'] == row['backup_sha256'], 'Backup hash mismatch.')
        current = config.read_bytes()
        if sha(current) == row['before_sha256']:
            targets.append((config, before, row, 'already_original'))
            continue
        require(sha(current) == row['after_sha256'], 'Config changed since installation; refusing whole-file rollback: ' + str(config))
        parse(before)
        targets.append((config, before, row, 'restore'))
    result = {'schema': SCHEMA, 'operation': 'rollback', 'source_manifest': str(manifest), 'created_at_utc': utcnow(), 'targets': []}
    for config, before, row, state in targets:
        if state == 'restore':
            require(sha(config.read_bytes()) == row['after_sha256'], 'Concurrent config change during rollback.')
            atomic_write(config, before)
        require(sha(config.read_bytes()) == row['before_sha256'], 'Rollback verification failed.')
        result['targets'].append({'config': str(config), 'sha256': row['before_sha256'], 'status': 'restored' if state == 'restore' else state})
    result['status'] = 'complete'
    save_report(output, result)
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--home', type=Path, default=Path.home())
    p.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    p.add_argument('--python', type=Path)
    p.add_argument('--report', type=Path)
    p.add_argument('--apply', action='store_true', help='Apply the previewed registration; default is a read-only dry run.')
    p.add_argument('--rollback', type=Path, metavar='INSTALL_MANIFEST')
    args = p.parse_args()
    root, home = args.root.resolve(), args.home.resolve()
    python = (args.python or Path(sys.executable)).resolve()
    default_name = 'codex-mcp-rollback.json' if args.rollback else 'codex-mcp-install.json'
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '-' + uuid.uuid4().hex[:8]
    output = args.report or root / 'integrations/installations' / stamp / default_name
    require(not output.exists(), 'Report already exists; select a new --report path to preserve the rollback manifest.')
    result = rollback(args.rollback, output) if args.rollback else install(home, root, python, output, apply=args.apply)
    print(json.dumps({'status': result['status'], 'report': str(output) if result['status'] != 'dry_run' else None, 'server': result.get('server'), 'targets': [{k: v for k, v in row.items() if k in {'config', 'status', 'changed', 'changed_fields', 'before_sha256', 'after_sha256', 'sha256'}} for row in result['targets']]}, ensure_ascii=False))


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    try:
        main()
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({'ok': False, 'error_type': type(exc).__name__, 'error': str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
