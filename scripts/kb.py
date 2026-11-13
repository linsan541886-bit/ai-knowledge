#!/usr/bin/env python3
"""Portable, evidence-bearing knowledge graph. Python 3.10+, standard library."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import uuid
import zipfile

KINDS = {"skill", "skill_version", "scenario", "procedure", "project", "usage", "observation", "lesson", "constraint", "tool", "evidence"}
STATES = {"candidate", "active", "validated", "contested", "deprecated", "archived"}
HIDDEN_STATES = {"contested", "deprecated", "archived"}
OUTCOMES = {"verified_help", "mixed", "failure", "unknown"}
ID_RE = re.compile(r"^[A-Za-z0-9_:./@+\-]{1,180}$")
MAX_RECORD_BYTES = 1024 * 1024
MAX_REQUEST_BYTES = 4 * 1024 * 1024
VERSION = "1.0.0"
CARD_USAGE_FIELDS = ("observed_episodes", "read_episodes", "executed_episodes", "observed_workspaces",
                     "reviewed_outcome_episodes", "verified_help_real_task_episodes", "last_observed")


class KBError(ValueError):
    pass


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)


def digest(value):
    return hashlib.sha256(dumps(value).encode("utf-8")).hexdigest()


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def need(condition, message):
    if not condition:
        raise KBError(message)


def checked_id(value):
    need(isinstance(value, str) and ID_RE.fullmatch(value) is not None, "id must be 1-180 ASCII identifier characters")
    return value


def json_file(path, max_bytes=MAX_REQUEST_BYTES):
    path = Path(path)
    need(path.stat().st_size <= max_bytes, "JSON input exceeds size limit")
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"), parse_constant=lambda x: (_ for _ in ()).throw(KBError("nonfinite JSON value")))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise KBError(f"invalid JSON: {exc}") from exc


def validate_evidence(value):
    need(isinstance(value, list) and len(value) <= 1000, "evidence must be a list of up to 1000 objects")
    for item in value:
        need(isinstance(item, dict), "each evidence reference must be an object")
        need(any(isinstance(item.get(key), str) and item[key] for key in ("path", "uri", "id", "source_id")), "evidence needs path, uri, id, or source_id")
        if "line" in item and item["line"] is not None:
            need(isinstance(item["line"], int) and not isinstance(item["line"], bool) and item["line"] >= 1, "evidence line must be a positive integer")
    return value


def validate_retrieval_rule(rule):
    """A small declarative filter, never expressions, regexes, commands or actions."""
    need(isinstance(rule, dict), "retrieval_rule must be an object")
    need(set(rule) <= {"query_any", "query_none", "exclude_skill_ids", "reason"}, "unknown retrieval_rule field")
    for key in ("query_any", "query_none"):
        terms = rule.get(key, [])
        need(isinstance(terms, list) and len(terms) <= 32, f"retrieval_rule.{key} must be a list of up to 32 terms")
        need(all(isinstance(term, str) and 0 < len(term.strip()) <= 200 for term in terms), f"retrieval_rule.{key} needs nonempty text terms")
    need(rule.get("query_any"), "retrieval_rule.query_any must not be empty")
    excludes = rule.get("exclude_skill_ids")
    need(isinstance(excludes, list) and 1 <= len(excludes) <= 100, "retrieval_rule.exclude_skill_ids needs 1..100 IDs")
    for node_id in excludes:
        checked_id(node_id)
    need(isinstance(rule.get("reason"), str) and 0 < len(rule["reason"].strip()) <= 2000, "retrieval_rule.reason required, max 2000 characters")
    return rule


def validate_node(raw, proposal=False):
    need(isinstance(raw, dict), "node must be an object")
    n = deepcopy(raw)
    checked_id(n.get("id"))
    need(n.get("kind") in KINDS, "invalid node kind")
    need(isinstance(n.get("title"), str) and 0 < len(n["title"]) <= 1000, "title required, max 1000 characters")
    n.setdefault("summary", "")
    need(isinstance(n["summary"], str), "summary must be a string")
    n.setdefault("status", "candidate")
    need(n["status"] in STATES, "invalid node status")
    if proposal:
        need(n["status"] == "candidate", "propose only creates candidate content; use review for activation")
    for key in ("aliases", "tags"):
        n.setdefault(key, [])
        need(isinstance(n[key], list) and all(isinstance(x, str) for x in n[key]), f"{key} must be a string list")
    n.setdefault("evidence", [])
    validate_evidence(n["evidence"])
    n.setdefault("properties", {})
    need(isinstance(n["properties"], dict), "properties must be an object")
    if "retrieval_rule" in n["properties"]:
        need(n["kind"] == "constraint", "retrieval_rule is allowed only on constraint nodes")
        validate_retrieval_rule(n["properties"]["retrieval_rule"])
    need(len(dumps(n).encode("utf-8")) <= MAX_RECORD_BYTES, "node exceeds 1 MiB")
    return n


def validate_edge(raw):
    need(isinstance(raw, dict), "edge must be an object")
    e = deepcopy(raw)
    for key in ("id", "source", "target"):
        checked_id(e.get(key))
    need(isinstance(e.get("relation"), str) and 0 < len(e["relation"]) <= 100, "edge relation required")
    e.setdefault("summary", "")
    need(isinstance(e["summary"], str), "edge summary must be text")
    e.setdefault("evidence", [])
    validate_evidence(e["evidence"])
    e.setdefault("properties", {})
    need(isinstance(e["properties"], dict), "edge properties must be an object")
    need(len(dumps(e).encode("utf-8")) <= MAX_RECORD_BYTES, "edge exceeds 1 MiB")
    return e


def read_jsonl(path):
    if not path.exists():
        return []
    out = []
    with path.open("rb") as stream:
        for number, raw in enumerate(stream, 1):
            need(len(raw) <= MAX_REQUEST_BYTES * 2, f"{path.name}:{number}: oversized record")
            need(raw.endswith(b"\n"), f"{path.name}:{number}: incomplete final record; repair from a verified backup")
            need(raw.strip(), f"{path.name}:{number}: blank record")
            try:
                value = json.loads(raw.decode("utf-8-sig"), parse_constant=lambda x: (_ for _ in ()).throw(KBError("nonfinite JSON value")))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise KBError(f"{path.name}:{number}: invalid JSON") from exc
            need(isinstance(value, dict), f"{path.name}:{number}: expected object")
            out.append(value)
    return out


def bounded(value, budget):
    """Keep useful assessment fields before bulky relations; never slice JSON."""
    need(isinstance(budget, int) and 256 <= budget <= 32000, "budget must be 256..32000 Unicode characters")
    candidate = deepcopy(value)
    candidate["budget_unit"] = "Unicode codepoints"
    text = dumps(candidate)
    if len(text) <= budget:
        return text
    node = value.get("node", {})
    node_id = node.get("id", value.get("id", ""))
    candidate = {"node": {k: node[k] for k in ("id", "kind", "status", "title") if k in node},
                 "revision": value.get("revision"), "truncated": True, "budget_unit": "Unicode codepoints",
                 "next": {"get": node_id, "expand": node_id}, "omitted_fields": []}
    if len(dumps(candidate)) > budget:
        candidate["node"].pop("title", None)
    if len(dumps(candidate)) > budget:
        minimal = {"id": node_id, "truncated": True, "next": "get"}
        text = dumps(minimal)
        need(len(text) <= budget, "identifier cannot fit context budget")
        return text
    omitted = []
    # Reserve enough space to declare omissions without sacrificing saved steps/metrics.
    reserve = min(180, max(30, budget // 10))
    def put(path, item):
        proposed = deepcopy(candidate)
        dest = proposed
        for key in path[:-1]:
            dest = dest.setdefault(key, {})
        dest[path[-1]] = item
        if len(dumps(proposed)) <= budget - reserve:
            candidate.clear()
            candidate.update(proposed)
            return True
        return False
    # A small context should explain suitability and limits before popularity.
    has_decision_context = False
    if "summary" in node:
        if put(("node", "summary"), node["summary"]):
            has_decision_context = bool(node["summary"])
        else:
            omitted.append("node.summary")
    priorities = ("limitations", "prerequisites", "effectiveness", "latest_review", "boundaries", "validation",
                  "when_to_use", "steps", "mechanism", "architecture", "decoupling", "knowledge_feedback",
                  "review_basis", "decisive_scenarios", "scenarios", "availability", "usage_metrics", "metrics")
    decision_keys = {"limitations", "prerequisites", "effectiveness", "latest_review", "boundaries", "validation", "when_to_use", "steps", "mechanism"}
    properties = node.get("properties", {})
    ordered = [key for key in priorities if key in properties] + [key for key in properties if key not in priorities]
    for key in ordered:
        item = properties[key]
        path = ("node", "properties", key)
        if key == "usage_metrics" and not has_decision_context:
            omitted.append("properties." + key)
            continue
        if put(path, item):
            if key in decision_keys and item:
                has_decision_context = True
            continue
        # Preserve meaningful subfields/items, with every omission identified.
        if isinstance(item, dict):
            for subkey, subvalue in item.items():
                if not put(path + (subkey,), subvalue):
                    omitted.append("properties." + key + "." + subkey)
                elif key in decision_keys and subvalue:
                    has_decision_context = True
        elif isinstance(item, list):
            kept = []
            for subvalue in item:
                if put(path, kept + [subvalue]):
                    kept.append(subvalue)
                    if key in decision_keys and subvalue:
                        has_decision_context = True
                else:
                    omitted.append("properties." + key + "[remaining]")
                    break
        else:
            omitted.append("properties." + key)
    for key in ("evidence", "tags", "aliases", "updated_at"):
        if key in node and not put(("node", key), node[key]):
            omitted.append("node." + key)
    if "relations" in value and not put(("relations",), value["relations"]):
        omitted.append("relations")
    with_count = {**candidate, "omitted_count": len(omitted)}
    if len(dumps(with_count)) <= budget:
        candidate = with_count
    for field in omitted:
        proposed = deepcopy(candidate)
        proposed["omitted_fields"].append(field)
        if len(dumps(proposed)) > budget:
            break
        candidate = proposed
    text = dumps(candidate)
    need(len(text) <= budget, "context metadata cannot fit budget")
    return text


class KnowledgeBase:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.data = self.root / "data"
        self.nodes = {}
        self.edges = {}
        self.events = []
        self.revision = 0
        self.manifest_verified = False
        self.baseline_hash = ""
        self.reload()

    def safe_data_path(self, name):
        path = self.data / name
        need(path.resolve().is_relative_to(self.root), "data path escapes knowledge root")
        return path

    def reload(self):
        self.nodes, self.edges, self.events = {}, {}, []
        self.revision = 0
        self.manifest_verified = False
        files = {}
        for name in ("nodes.jsonl", "edges.jsonl"):
            path = self.safe_data_path(name)
            raw = path.read_bytes() if path.exists() else b""
            files[name] = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        self.baseline_hash = digest(files)
        manifest_path = self.safe_data_path("baseline_manifest.json")
        if manifest_path.exists():
            manifest = json_file(manifest_path)
            need(manifest.get("schema") == "ai-knowledge-baseline/v1", "unsupported baseline manifest")
            for name, actual in files.items():
                need(manifest.get("files", {}).get(name) == actual, f"baseline hash or byte mismatch: {name}")
            self.manifest_verified = True
        for row in read_jsonl(self.safe_data_path("nodes.jsonl")):
            n = validate_node(row)
            need(n["id"] not in self.nodes, f"duplicate baseline node: {n['id']}")
            self.nodes[n["id"]] = n
        for row in read_jsonl(self.safe_data_path("edges.jsonl")):
            e = validate_edge(row)
            need(e["id"] not in self.edges, f"duplicate baseline edge: {e['id']}")
            need(e["source"] in self.nodes and e["target"] in self.nodes, f"dangling baseline edge: {e['id']}")
            self.edges[e["id"]] = e
        prev = "0" * 64
        keys = set()
        for event in read_jsonl(self.safe_data_path("ledger.jsonl")):
            unhashed = {k: v for k, v in event.items() if k != "hash"}
            need(event.get("hash") == digest(unhashed), "ledger event hash mismatch")
            need(event.get("prev_hash") == prev, "ledger hash chain broken")
            need(event.get("baseline_hash") == self.baseline_hash, "baseline changed after ledger creation; migrate into a new baseline generation")
            need(event.get("revision") == self.revision + 1, "ledger revision discontinuity")
            need(event.get("idempotency_key") not in keys, "duplicate ledger idempotency key")
            keys.add(event.get("idempotency_key"))
            self.apply_operations(event.get("operations", []))
            self.events.append(event)
            self.revision += 1
            prev = event["hash"]
        self.adjacency = defaultdict(list)
        for e in self.edges.values():
            self.adjacency[e["source"]].append(e)
            if e["target"] != e["source"]:
                self.adjacency[e["target"]].append(e)
        self.search_text = {key: dumps(n).lower() for key, n in self.nodes.items()}
        return self

    def apply_operations(self, operations):
        need(isinstance(operations, list) and operations, "ledger transaction requires operations")
        for op in operations:
            need(isinstance(op, dict), "invalid ledger operation")
            if op.get("op") == "put_node":
                n = validate_node(op.get("node"))
                self.nodes[n["id"]] = n
            elif op.get("op") == "put_edge":
                e = validate_edge(op.get("edge"))
                need(e["source"] in self.nodes and e["target"] in self.nodes, f"dangling proposed edge: {e['id']}")
                self.edges[e["id"]] = e
            else:
                raise KBError("unsupported ledger operation")

    @contextmanager
    def writer_lock(self):
        self.data.mkdir(parents=True, exist_ok=True)
        path = self.safe_data_path("writer.lock")
        token = uuid.uuid4().hex
        payload = {"token": token, "pid": os.getpid(), "created_at": utcnow()}
        try:
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise KBError("writer lock exists; retry later; never remove without verifying owner and saved transaction") from exc
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(dumps(payload))
                stream.flush()
                os.fsync(stream.fileno())
            yield
        finally:
            if path.exists() and json_file(path).get("token") == token:
                path.unlink()

    def _commit(self, request, actor, key, expected_revision, make_operations):
        need(isinstance(actor, str) and 0 < len(actor) <= 200, "actor required, max 200 characters")
        need(isinstance(key, str) and 0 < len(key) <= 200, "idempotency key required, max 200 characters")
        need(type(expected_revision) is int and expected_revision >= 0, "expected_revision must be a nonnegative integer")
        need(len(dumps(request).encode("utf-8")) <= MAX_REQUEST_BYTES, "request exceeds 4 MiB")
        request_hash = digest({"request": request, "actor": actor})
        with self.writer_lock():
            self.reload()
            for prior in self.events:
                if prior["idempotency_key"] == key:
                    need(prior["request_hash"] == request_hash, "idempotency key reused with different content or actor")
                    return {"revision": prior["revision"], "current_revision": self.revision, "replayed": True, "event_hash": prior["hash"]}
            need(expected_revision == self.revision, f"revision conflict: expected {expected_revision}, current {self.revision}")
            operations = make_operations()
            original_nodes, original_edges = self.nodes, self.edges
            self.nodes, self.edges = self.nodes.copy(), self.edges.copy()
            try:
                self.apply_operations(operations)  # Validate without altering the current in-memory state.
            finally:
                self.nodes, self.edges = original_nodes, original_edges
            event = {"schema": "ai-knowledge-event/v1", "revision": self.revision + 1, "timestamp": utcnow(), "actor": actor,
                     "idempotency_key": key, "request_hash": request_hash, "prev_hash": self.events[-1]["hash"] if self.events else "0" * 64,
                     "baseline_hash": self.baseline_hash, "operations": operations}
            event["hash"] = digest(event)
            path = self.safe_data_path("ledger.jsonl")
            previous = path.read_bytes() if path.exists() else b""
            temp = self.safe_data_path(".ledger." + uuid.uuid4().hex + ".tmp")
            try:
                with temp.open("xb") as stream:
                    stream.write(previous)
                    stream.write((dumps(event) + "\n").encode("utf-8"))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp, path)
            finally:
                if temp.exists():
                    temp.unlink()
            self.reload()
            return {"revision": self.revision, "current_revision": self.revision, "replayed": False, "event_hash": event["hash"],
                    "node_ids": [o["node"]["id"] for o in operations if o["op"] == "put_node"], "edge_ids": [o["edge"]["id"] for o in operations if o["op"] == "put_edge"]}

    def propose(self, proposal, actor, key):
        need(isinstance(proposal, dict), "proposal must be an object")
        need(set(proposal) <= {"nodes", "edges", "expected_revision"}, "unknown proposal fields")
        need(isinstance(proposal.get("nodes", []), list) and isinstance(proposal.get("edges", []), list), "nodes and edges must be lists")
        def operations():
            nodes = [validate_node(n, proposal=True) for n in proposal.get("nodes", [])]
            edges = [validate_edge(e) for e in proposal.get("edges", [])]
            need(len({n["id"] for n in nodes}) == len(nodes), "duplicate proposed node IDs")
            need(len({e["id"] for e in edges}) == len(edges), "duplicate proposed edge IDs")
            for n in nodes:
                n["updated_at"] = utcnow()
                n["properties"].pop("latest_review", None)
                if n["id"] in self.nodes:
                    need(n["kind"] == self.nodes[n["id"]]["kind"], "existing node kind cannot change; create a new ID and an explicit relation")
            for e in edges:
                e["properties"]["review_status"] = "candidate"
            return [{"op": "put_node", "node": n} for n in nodes] + [{"op": "put_edge", "edge": e} for e in edges]
        return self._commit({"type": "propose", "proposal": proposal}, actor, key, proposal.get("expected_revision"), operations)

    def review(self, node_id, state, reason, review_data, actor, key, expected_revision):
        checked_id(node_id)
        need(state in STATES - {"candidate"}, "invalid review state")
        need(isinstance(reason, str) and 0 < len(reason) <= 6000, "review reason required")
        need(isinstance(review_data, dict) and set(review_data) <= {"evidence", "validation"}, "review data accepts evidence and validation")
        evidence = validate_evidence(review_data.get("evidence", []))
        need(evidence, "review must include at least one evidence reference")
        validation = review_data.get("validation")
        if state == "validated":
            need(isinstance(validation, dict), "validated state requires structured validation")
            for key_name in ("method", "scope", "artifact"):
                need(isinstance(validation.get(key_name), str) and validation[key_name].strip(), f"validation.{key_name} required")
            need(validation.get("result") == "pass", "validated requires result=pass; partial/failed evidence remains active or contested")
        request = {"type": "review", "id": node_id, "state": state, "reason": reason, "data": review_data, "expected_revision": expected_revision}
        def operations():
            need(node_id in self.nodes, "unknown node")
            n = deepcopy(self.nodes[node_id])
            n["status"] = state
            n["updated_at"] = utcnow()
            n["properties"]["latest_review"] = {"actor": actor, "reason": reason, "evidence": evidence,
                                                   "validation": validation, "previous_status": self.nodes[node_id]["status"],
                                                   "semantics": "recorded reviewer assertion; runtime checks structure, not external truth"}
            return [{"op": "put_node", "node": n}]
        return self._commit(request, actor, key, expected_revision, operations)

    def card(self, n):
        p = n.get("properties", {})
        card = {"id": n["id"], "kind": n["kind"], "title": n["title"], "status": n["status"],
                "summary": n["summary"][:240], "evidence_count": len(n.get("evidence", []))}
        if len(n["summary"]) > 240:
            card["summary_shortened"] = True
        for key in ("availability", "metrics", "confidence", "review_basis"):
            if key in p:
                card[key] = p[key]
        if isinstance(p.get("usage_metrics"), dict):
            card["usage_metrics"] = {key: p["usage_metrics"][key] for key in CARD_USAGE_FIELDS if key in p["usage_metrics"]}
        if n["kind"] == "usage":
            outcome = p.get("outcome", {})
            if not isinstance(outcome, dict):
                outcome = {}
            card["outcome"] = {key: outcome.get(key, "unknown") for key in ("status", "scope", "validation_level")}
        return card

    def stats(self):
        return {"revision": self.revision, "nodes": len(self.nodes), "edges": len(self.edges), "events": len(self.events),
                "kinds": dict(Counter(n["kind"] for n in self.nodes.values())), "states": dict(Counter(n["status"] for n in self.nodes.values())),
                "baseline_manifest_verified": self.manifest_verified, "baseline_hash": self.baseline_hash,
                "interpretation": "status and outcomes are evidence-bearing assertions, not automatic proof of project success"}

    def overview(self):
        priorities = {"scenario": 0, "procedure": 1, "constraint": 2, "skill": 3}
        nodes = sorted((n for n in self.nodes.values() if n["status"] not in HIDDEN_STATES), key=lambda n: (priorities.get(n["kind"], 9), n["title"]))
        return {"stats": self.stats(), "items": [self.card(n) for n in nodes[:8]], "next": {"command": "search", "query": "具体任务或场景", "limit": 8},
                "read_order": ["search or recommend", "get one selected ID", "expand selected relations", "context within budget"],
                "write_order": ["propose candidate at expected_revision", "independently assess evidence", "review scoped outcome", "record use or correction"]}

    def get(self, node_id):
        need(node_id in self.nodes, "unknown node")
        return {"revision": self.revision, "node": self.nodes[node_id], "relation_count": len(self.adjacency[node_id]),
                "next": {"command": "expand", "id": node_id, "limit": 8}}

    def search(self, query, limit=8, offset=0, kind=None, include_inactive=False, outcome=None, exclude_synthetic=False, _eligible=None):
        need(isinstance(query, str) and len(query) <= 2000, "query must be text up to 2000 characters")
        need(type(limit) is int and 1 <= limit <= 50 and type(offset) is int and offset >= 0, "invalid pagination")
        need(kind is None or kind in KINDS, "invalid kind filter")
        need(outcome is None or outcome in OUTCOMES, "invalid outcome filter")
        need(type(exclude_synthetic) is bool, "exclude_synthetic must be boolean")
        query = query.strip().lower()
        terms = re.findall(r"[a-z0-9_./+-]+|[\u3400-\u9fff]+", query)
        grams = {s[i:i + 2] for s in terms if re.search(r"[\u3400-\u9fff]", s) for i in range(len(s) - 1)}
        ranked = []
        for node_id, n in self.nodes.items():
            if _eligible is not None and not _eligible(n):
                continue
            if kind and n["kind"] != kind:
                continue
            recorded_outcome = n["properties"].get("outcome", {})
            if not isinstance(recorded_outcome, dict):
                recorded_outcome = {}
            if outcome is not None and (n["kind"] != "usage" or recorded_outcome.get("status") != outcome):
                continue
            if exclude_synthetic and (recorded_outcome.get("scope") == "synthetic_validation" or n["properties"].get("scope") == "synthetic_validation"):
                continue
            if not include_inactive and n["status"] in HIDDEN_STATES:
                continue
            body = self.search_text[node_id]
            title = (n["title"] + " " + " ".join(n["aliases"] + n["tags"])).lower()
            matches = [term for term in terms if term in body]
            partial = [term for term in grams if term in body]
            score = (20 if query and query in title else 0) + (8 if query and query in body else 0)
            score += sum(7 if term in title else 2 for term in matches)
            score += min(5, len(partial))
            if query and score == 0:
                continue
            ranked.append((score, n, matches, partial))
        ranked.sort(key=lambda row: (-row[0], row[1]["title"], row[1]["id"]))
        items = []
        for score, n, matches, partial in ranked[offset:offset + limit]:
            item = self.card(n)
            item["retrieval"] = {"rank_score": score, "matched_terms": matches[:12], "matched_chinese_bigrams": partial[:12], "meaning": "lexical relevance only, not effectiveness"}
            items.append(item)
        return {"query": query, "revision": self.revision, "total": len(ranked), "offset": offset, "items": items,
                "filters": {"kind": kind, "outcome": outcome, "exclude_synthetic": exclude_synthetic, "include_inactive": include_inactive},
                "next_offset": offset + limit if offset + limit < len(ranked) else None}

    def recommend(self, query, limit=8):
        need(type(limit) is int and 1 <= limit <= 50, "limit must be 1..50")
        need(isinstance(query, str) and len(query) <= 2000, "query must be text up to 2000 characters")
        normalized_query = query.strip().casefold()
        applied = []
        exclusions = defaultdict(list)
        for n in sorted(self.nodes.values(), key=lambda item: item["id"]):
            if n["kind"] != "constraint" or n["status"] not in {"active", "validated"}:
                continue
            rule = n["properties"].get("retrieval_rule")
            if rule is None:
                continue
            matched = [term for term in rule["query_any"] if term.strip().casefold() in normalized_query]
            bypass = any(term.strip().casefold() in normalized_query for term in rule.get("query_none", []))
            if not matched or bypass:
                continue
            applied.append({"id": n["id"], "title": n["title"], "reason": rule["reason"], "matched_query_terms": matched})
            for skill_id in rule["exclude_skill_ids"]:
                exclusions[skill_id].append(n["id"])
        applied_ids = {rule["id"] for rule in applied}
        allowed = {"skill", "skill_version", "procedure", "lesson", "scenario", "constraint", "tool"}
        logical_skills = {n["properties"].get("logical_id", n["id"]) for n in self.nodes.values() if n["kind"] == "skill" and n["status"] not in HIDDEN_STATES}
        version_nodes = set()
        for edge in self.edges.values():
            if edge["relation"] == "has_version" and self.nodes[edge["source"]]["kind"] == "skill":
                version_nodes.add(edge["target"])
            if edge["relation"] == "version_of" and self.nodes[edge["target"]]["kind"] == "skill":
                version_nodes.add(edge["source"])
        def base_eligible(n):
            if n["kind"] not in allowed:
                return False
            p = n["properties"]
            if p.get("availability") in {"unavailable", "disabled", "backup", "stale_cache", "superseded"}:
                return False
            if n["kind"] == "skill_version" and (p.get("logical_id") in logical_skills or n["id"] in version_nodes):
                return False
            return True
        def excluded_by(n):
            if n["kind"] not in {"skill", "skill_version"}:
                return []
            identifiers = (n["id"], n["properties"].get("logical_id"))
            return sorted({constraint_id for skill_id in identifiers for constraint_id in exclusions.get(skill_id, [])})
        def eligible(n):
            return base_eligible(n) and not excluded_by(n) and n["id"] not in applied_ids
        # Filter the entire candidate population before ranking/pagination. Thousands
        # of matching usage rows must not crowd every callable capability out.
        result = self.search(query, limit=limit, _eligible=eligible)
        # Applicable reviewed constraints lead the list even if other entries have
        # stronger lexical matches. They route candidates, not execution authority.
        leading = []
        for rule in applied[:limit]:
            item = self.card(self.nodes[rule["id"]])
            item["retrieval"] = {"meaning": "applicable reviewed retrieval constraint; not execution authorization", "matched_terms": rule["matched_query_terms"]}
            leading.append(item)
        candidates = (leading + result["items"])[:limit]
        excluded = self.search(query, limit=5, _eligible=lambda n: base_eligible(n) and bool(excluded_by(n)))
        excluded_candidates = []
        reasons = {rule["id"]: rule["reason"] for rule in applied}
        for item in excluded["items"]:
            constraint_ids = excluded_by(self.nodes[item["id"]])
            excluded_candidates.append({"id": item["id"], "kind": item["kind"], "title": item["title"],
                                        "excluded_by": constraint_ids, "reasons": [reasons[key] for key in constraint_ids]})
        selected = []
        for item in candidates:
            n = self.nodes[item["id"]]
            p = n["properties"]
            availability = p.get("availability", "unknown")
            item["decision_checks"] = {"availability": availability, "prerequisites": p.get("prerequisites", []),
                                       "boundaries": p.get("boundaries", p.get("warnings", [])),
                                       "validation": p.get("latest_review", {}).get("validation"),
                                       "interpretation": "candidate requires task fit and current permissions; retrieval does not authorize execution"}
            selected.append(item)
        return {"query": query, "revision": self.revision, "items": selected, "total_retrieved": result["total"] + len(applied),
                "applied_constraints": applied, "excluded_candidates": excluded_candidates, "excluded_total": excluded["total"],
                "method": "reviewed declarative routing, then lexical relevance with lifecycle/availability exclusions; no learned success score",
                "next": "get selected ID and verify prerequisites"}

    def expand(self, node_id, limit=8, offset=0, relation=None):
        need(node_id in self.nodes, "unknown node")
        need(type(limit) is int and 1 <= limit <= 50 and type(offset) is int and offset >= 0, "invalid pagination")
        edges = sorted((e for e in self.adjacency[node_id] if relation is None or e["relation"] == relation), key=lambda e: (e["relation"], e["id"]))
        items = []
        for e in edges[offset:offset + limit]:
            other_id = e["target"] if e["source"] == node_id else e["source"]
            items.append({"edge": e, "direction": "outgoing" if e["source"] == node_id else "incoming", "node": self.card(self.nodes[other_id])})
        return {"id": node_id, "revision": self.revision, "total": len(edges), "offset": offset, "items": items,
                "next_offset": offset + limit if offset + limit < len(edges) else None}

    def context(self, node_id, budget=4000):
        result = self.get(node_id)
        result["relations"] = self.expand(node_id)["items"]
        return bounded(result, budget)

    def history(self, node_id):
        need(node_id in self.nodes or node_id in self.edges, "unknown node or edge")
        changes = []
        for event in self.events:
            operations = [op for op in event["operations"] if op.get("node", op.get("edge", {})).get("id") == node_id]
            if operations:
                changes.append({**{k: event[k] for k in ("revision", "timestamp", "actor", "hash", "idempotency_key")}, "operations": operations})
        return {"id": node_id, "baseline_hash": self.baseline_hash, "events": changes, "revision": self.revision}

    def health(self):
        return {"ok": True, **self.stats(), "writer_lock_present": self.safe_data_path("writer.lock").exists(),
                "guarantees": ["valid schema and endpoints", "baseline manifest if supplied", "hash-chain ledger and baseline binding"],
                "not_guaranteed": ["external evidence truth or currentness", "protection against a filesystem owner rewriting every hash", "automatic source monitoring"]}

    def export(self, output):
        output = Path(output).resolve()
        need(not output.exists(), "export target already exists")
        need(not output.is_relative_to(self.root), "export target must be outside knowledge root")
        allowed_data = {"nodes.jsonl", "edges.jsonl", "ledger.jsonl", "baseline_manifest.json", "inventory.jsonl", "usage.jsonl", "projects.jsonl", "curated.json"}
        files = []
        for path in self.root.rglob("*"):
            if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(self.root):
                continue
            rel = path.relative_to(self.root)
            if any(p in {"__pycache__", ".git", "node_modules"} for p in rel.parts):
                continue
            allowed = len(rel.parts) == 1 and path.suffix in {".md", ".json"}
            allowed |= rel.parts[0] in {"scripts", "docs", "ui", "tests"} and path.suffix in {".py", ".md", ".js", ".css", ".html", ".json"}
            allowed |= rel.parts[0] == "data" and len(rel.parts) == 2 and path.name in allowed_data
            allowed |= rel.parts[0] == "reports" and path.suffix in {".md", ".json"}
            allowed |= rel.parts[0] == "reports" and path.name in {"usage-evidence.jsonl", "high-frequency-review.jsonl"}
            if allowed:
                files.append((path, rel.as_posix()))
        output.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            for path, rel in sorted(files):
                archive.write(path, rel)
        return {"path": str(output), "files": len(files), "bytes": output.stat().st_size,
                "sha256": hashlib.sha256(output.read_bytes()).hexdigest(), "includes_raw_source_logs": False,
                "portability_note": "evidence paths remain references; transfer source evidence separately only when authorized"}


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    sub = p.add_subparsers(dest="command", required=True)
    for cmd in ("overview", "stats", "health"):
        sub.add_parser(cmd)
    for cmd in ("search", "recommend"):
        q = sub.add_parser(cmd)
        q.add_argument("query")
        q.add_argument("--limit", type=int, default=8)
        if cmd == "search":
            q.add_argument("--offset", type=int, default=0)
            q.add_argument("--kind", choices=sorted(KINDS))
            q.add_argument("--include-inactive", action="store_true")
            q.add_argument("--outcome", choices=sorted(OUTCOMES))
            q.add_argument("--exclude-synthetic", action="store_true")
    for cmd in ("get", "expand", "context", "history"):
        q = sub.add_parser(cmd)
        q.add_argument("id")
        if cmd in {"get", "context"}:
            q.add_argument("--budget", type=int, default=4000 if cmd == "context" else None)
        if cmd == "expand":
            q.add_argument("--limit", type=int, default=8)
            q.add_argument("--offset", type=int, default=0)
            q.add_argument("--relation")
    q = sub.add_parser("propose")
    q.add_argument("--file", required=True)
    q.add_argument("--actor", required=True)
    q.add_argument("--idempotency-key", required=True)
    q = sub.add_parser("review")
    q.add_argument("--id", required=True)
    q.add_argument("--state", required=True, choices=sorted(STATES - {"candidate"}))
    q.add_argument("--reason", required=True)
    q.add_argument("--evidence-file", required=True)
    q.add_argument("--actor", required=True)
    q.add_argument("--idempotency-key", required=True)
    q.add_argument("--expected-revision", required=True, type=int)
    q = sub.add_parser("export")
    q.add_argument("--out", required=True)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        kb = KnowledgeBase(args.root)
        cmd = args.command
        if cmd in {"overview", "stats", "health"}:
            result = getattr(kb, cmd)()
        elif cmd == "search":
            result = kb.search(args.query, args.limit, args.offset, args.kind, args.include_inactive, args.outcome, args.exclude_synthetic)
        elif cmd == "recommend":
            result = kb.recommend(args.query, args.limit)
        elif cmd == "get":
            result = kb.get(args.id)
            if args.budget is not None:
                print(bounded(result, args.budget))
                return 0
        elif cmd == "context":
            print(kb.context(args.id, args.budget))
            return 0
        elif cmd == "expand":
            result = kb.expand(args.id, args.limit, args.offset, args.relation)
        elif cmd == "history":
            result = kb.history(args.id)
        elif cmd == "propose":
            result = kb.propose(json_file(args.file), args.actor, args.idempotency_key)
        elif cmd == "review":
            result = kb.review(args.id, args.state, args.reason, json_file(args.evidence_file), args.actor, args.idempotency_key, args.expected_revision)
        elif cmd == "export":
            result = kb.export(args.out)
        print(dumps(result))
        return 0
    except (KBError, OSError, UnicodeError, TypeError) as exc:
        print(dumps({"ok": False, "error": str(exc)}))
        return 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
