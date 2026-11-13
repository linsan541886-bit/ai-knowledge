#!/usr/bin/env python3
"""Thin locator; implementation belongs to the independent knowledge root."""
import json
import os
from pathlib import Path
import subprocess
import sys

def main():
    root = os.environ.get('AI_KNOWLEDGE_ROOT')
    locator = Path.home() / '.ai-knowledge.json'
    if not root and locator.is_file():
        root = json.loads(locator.read_text(encoding='utf-8-sig')).get('root')
    if not root:
        raise ValueError('Set AI_KNOWLEDGE_ROOT or ~/.ai-knowledge.json root; no knowledge root was selected.')
    root = Path(root).expanduser().resolve()
    entry = root / 'scripts' / 'kb.py'
    if not entry.is_file() or entry.resolve() == Path(__file__).resolve():
        raise ValueError('Selected independent knowledge root has no runtime scripts/kb.py')
    return subprocess.call([sys.executable, str(entry), '--root', str(root), *sys.argv[1:]])

if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, TypeError) as exc:
        print(json.dumps({'ok':False,'error':str(exc)},ensure_ascii=False))
        raise SystemExit(1)
