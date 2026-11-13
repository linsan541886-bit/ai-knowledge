#!/usr/bin/env python3
"""Initialize a new local knowledge store from explicitly synthetic examples.

Python 3.10+; standard library only. Existing data directories are never replaced.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile

from kb import KnowledgeBase, validate_edge, validate_node


PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def initialize(root: Path, empty: bool = False) -> dict:
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = root / "data"
    if target.exists() or target.is_symlink():
        raise ValueError("Refusing to replace an existing data directory; choose a new root.")
    example = {"nodes": [], "edges": []} if empty else json.loads(
        (PACKAGE_ROOT / "examples" / "starter-graph.json").read_text(encoding="utf-8")
    )
    ids = set()
    for node in example["nodes"]:
        validate_node(node)
        if node["id"] in ids:
            raise ValueError("Duplicate example node ID")
        ids.add(node["id"])
    for edge in example["edges"]:
        validate_edge(edge)
        if edge["source"] not in ids or edge["target"] not in ids:
            raise ValueError("Example edge has an unknown endpoint")
    # A failed or interrupted build cannot expose a partially written baseline.
    # The final rename is same-volume and refuses an existing destination.
    with tempfile.TemporaryDirectory(prefix=".bootstrap-", dir=root) as staging:
        stage_root = Path(staging)
        stage = stage_root / "data"
        stage.mkdir()
        manifest = {"schema": "ai-knowledge-baseline/v1", "files": {}}
        for filename, key in (("nodes.jsonl", "nodes"), ("edges.jsonl", "edges")):
            payload = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
                              for row in example[key]).encode("utf-8")
            (stage / filename).write_bytes(payload)
            manifest["files"][filename] = {
                "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()
            }
        (stage / "baseline_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
        )
        (stage / "ledger.jsonl").write_bytes(b"")
        health = KnowledgeBase(stage_root).health()
        stage.rename(target)
    return {"ok": health["ok"], "root": str(root), "revision": 0,
            "nodes": len(ids), "edges": len(example["edges"]),
            "mode": "empty" if empty else "synthetic_examples",
            "notice": "Example cards are demonstrations, not observations or verified outcomes."}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=PACKAGE_ROOT)
    parser.add_argument("--empty", action="store_true", help="Create no example records")
    args = parser.parse_args()
    try:
        print(json.dumps(initialize(args.root, args.empty), ensure_ascii=False))
    except (ValueError, OSError) as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    main()
