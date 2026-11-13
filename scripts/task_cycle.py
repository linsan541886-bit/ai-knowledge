#!/usr/bin/env python3
"""Small task-start retrieval and evidence-bearing task-finish writeback interface."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid

from kb import KBError, KnowledgeBase, digest, dumps, json_file, need, utcnow

VERSION = "ai-knowledge-task-cycle/v1"
MAX_EVIDENCE_BYTES = 16 * 1024 * 1024
RECEIPT_NAME = re.compile(r"[0-9a-f]{32}\.json\Z")
SENSITIVE_NAMES = {"auth.json", ".credentials.yaml", ".credentials.yml", "credentials.json", ".env"}


def project_key(path):
    value = str(Path(path).resolve()).replace("\\", "/").rstrip("/")
    return value.casefold() if os.name == "nt" else value


def nonempty(value, label, maximum=6000):
    need(isinstance(value, str) and value.strip() and len(value) <= maximum, f"{label} requires nonempty text, max {maximum} characters")
    return value.strip()


class TaskCycle:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.runs = self.root / "integrations" / "task-runs"
        need(self.runs.resolve().is_relative_to(self.root), "task receipt directory escapes knowledge root")

    def receipt_path(self, value):
        path = Path(value)
        if not path.is_absolute():
            path = self.runs / path
        need(RECEIPT_NAME.fullmatch(path.name) is not None, "receipt must be a generated UUID .json filename")
        need(not path.is_symlink(), "receipt symlinks are not accepted")
        need(path.resolve().parent == self.runs.resolve(), "receipt must be directly inside this knowledge root's integrations/task-runs")
        need(self.runs.resolve().is_relative_to(self.root), "receipt directory escapes knowledge root")
        return path.resolve()

    @contextmanager
    def receipt_lock(self, path):
        self.runs.mkdir(parents=True, exist_ok=True)
        lock = path.with_suffix(".lock")
        token = uuid.uuid4().hex
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise KBError("task receipt is locked; retry after owner finishes; do not delete an unverified lock") from exc
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(dumps({"pid": os.getpid(), "token": token, "created_at": utcnow()}))
                stream.flush(); os.fsync(stream.fileno())
            yield
        finally:
            if lock.exists() and json_file(lock).get("token") == token:
                lock.unlink()

    def write_receipt(self, path, data):
        path = self.receipt_path(path)
        temp = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with temp.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(dumps(data) + "\n"); stream.flush(); os.fsync(stream.fileno())
            os.replace(temp, path)
        finally:
            if temp.exists(): temp.unlink()

    def load_receipt(self, path):
        data = json_file(path)
        need(data.get("schema") == VERSION, "unsupported receipt schema")
        need(data.get("receipt_id") == path.stem, "receipt identity does not match its filename")
        need(data.get("knowledge_root") == str(self.root), "receipt belongs to a different knowledge root")
        return data

    def project_id(self, kb, project_root):
        key = project_key(project_root)
        for n in sorted(kb.nodes.values(), key=lambda n: n["id"]):
            if n["kind"] != "project": continue
            p = n["properties"]
            location = p.get("project_root") or p.get("cwd")
            if location and project_key(location) == key:
                return n["id"]
        return "project:task-" + digest(key)[:24]

    def retrieval(self, kb, query, project_root):
        result = kb.recommend(query, 4)
        recommendations = result["items"]
        lessons = kb.search(query, limit=3, kind="lesson")["items"]
        projects = kb.search(query, limit=2, kind="project")["items"]
        pid = self.project_id(kb, project_root)
        local_lessons = []
        if pid in kb.nodes:
            projects.insert(0, kb.card(kb.nodes[pid]))
            related = {e["source"] if e["target"] == pid else e["target"] for e in kb.adjacency[pid]}
            for nid in sorted(related):
                n = kb.nodes[nid]
                if n["kind"] in {"lesson", "observation", "usage"} and n["status"] not in {"contested", "deprecated", "archived"}:
                    local_lessons.append(kb.card(n))
        ordered = [x for x in recommendations if x["kind"] == "constraint"]
        ordered += local_lessons[:2] + lessons[:2]
        ordered += [x for x in recommendations if x["kind"] != "constraint"][:2] + projects[:1]
        # If one bucket is empty, fill from the remaining bounded candidates.
        ordered += recommendations + projects + lessons
        cards, seen = [], set()
        for c in ordered:
            if c["id"] in seen: continue
            seen.add(c["id"])
            n = kb.nodes[c["id"]]; p = n["properties"]
            card = {key: c[key] for key in ("id", "kind", "title", "summary", "status", "availability", "outcome") if key in c}
            scope = p.get("scope") or p.get("validation", {}).get("scope") or p.get("outcome", {}).get("scope")
            if scope: card["scope"] = scope
            if p.get("prerequisites"): card["prerequisites"] = p["prerequisites"][:3]
            if c["id"] in {x["id"] for x in local_lessons}: card["match_basis"] = "same observed project directory; check applicability"
            card["next"] = "get/context " + c["id"]
            cards.append(card)
            if len(cards) == 6: break
        return cards, result.get("applied_constraints", [])

    def render_begin(self, receipt, path, budget, replayed):
        need(type(budget) is int and 512 <= budget <= 16000, "begin budget must be 512..16000 Unicode characters")
        result = {"ok": True, "receipt": str(path), "task_id": receipt["task_id"], "revision_at_begin": receipt["revision_at_begin"],
                  "cards": [], "replayed": replayed, "next": "Read selected method with get/context; finish this receipt with an evidence-bearing outcome or explicit no-new-knowledge reason."}
        omitted = 0
        for c in receipt["cards"]:
            candidate = {**result, "cards": result["cards"] + [c], "omitted_cards": 6}
            if len(dumps(candidate)) <= budget:
                result["cards"].append(c)
            else: omitted += 1
        result["omitted_cards"] = omitted
        if receipt.get("applied_constraints"):
            proposed = {**result, "applied_constraints": receipt["applied_constraints"]}
            if len(dumps(proposed)) <= budget: result = proposed
        if len(dumps(result)) > budget:
            result = {"ok": True, "receipt": str(path), "cards": [], "omitted_cards": len(receipt["cards"]), "next": "Use a larger begin budget; then get/context by selected ID."}
        need(len(dumps(result)) <= budget, "receipt path cannot fit requested budget; increase budget")
        return result

    def record_delivery(self, receipt, response):
        # Once finish starts, its provenance belongs to the saved exact transaction.
        # A later begin replay must not rewrite that transaction or historical graph.
        if receipt.get("finish_input_hash") or receipt["status"] == "finished":
            return False
        before = dumps(receipt)
        if "candidate_node_ids" not in receipt:
            receipt["candidate_node_ids"] = list(receipt.get("selected_node_ids", []))
        if "delivered_node_ids" not in receipt:
            receipt["delivered_node_ids"] = []
            receipt["legacy_delivery_note"] = "Legacy selected IDs are retrieval candidates; prior delivery was not recorded and is unknown."
        delivered = receipt["delivered_node_ids"]
        for card in response["cards"]:
            if card["id"] not in delivered:
                delivered.append(card["id"])
        # Compatibility alias now means actually emitted, never pre-budget candidates.
        receipt["selected_node_ids"] = list(delivered)
        return dumps(receipt) != before

    def begin(self, client, project_root, query, task_id=None, budget=5000):
        need(client in {"codex", "dsh", "other"}, "client must be codex, dsh, or other")
        root = Path(project_root).resolve(); need(root.is_dir(), "project-root must be an existing directory")
        nonempty(query, "query", 2000)
        task_id = task_id or uuid.uuid4().hex
        nonempty(task_id, "task-id", 180)
        identity = {"client": client, "project_root": project_key(root), "task_id": task_id}
        rid = uuid.uuid5(uuid.NAMESPACE_URL, dumps(identity)).hex
        path = self.receipt_path(rid + ".json")
        inputs = {**identity, "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest()}
        with self.receipt_lock(path):
            if path.exists():
                receipt = self.load_receipt(path)
                need(receipt["input_hash"] == digest(inputs), "task-id was already used with different begin inputs")
                response = self.render_begin(receipt, path, budget, True)
                if self.record_delivery(receipt, response):
                    self.write_receipt(path, receipt)
                return response
            kb = KnowledgeBase(self.root)
            cards, constraints = self.retrieval(kb, query, root)
            receipt = {"schema": VERSION, "receipt_id": rid, "task_id": task_id, "client": client, "knowledge_root": str(self.root), "project_root": str(root),
                       "query_sha256": inputs["query_sha256"], "input_hash": digest(inputs), "revision_at_begin": kb.revision, "started_at": utcnow(),
                       "status": "started", "candidate_node_ids": [c["id"] for c in cards], "delivered_node_ids": [],
                       "selected_node_ids": [], "cards": cards, "applied_constraints": constraints}
            response = self.render_begin(receipt, path, budget, False)
            self.record_delivery(receipt, response)
            self.write_receipt(path, receipt)
            return response

    def normalize_outcome(self, raw, project_root):
        need(isinstance(raw, dict), "outcome must be a JSON object")
        need(set(raw) <= {"summary", "outcome", "learnings", "skip_reason"}, "unknown outcome fields; status/validated promotion is not accepted")
        summary = nonempty(raw.get("summary"), "summary", 3000)
        need(raw.get("outcome") in {"success", "partial", "failure", "unknown"}, "outcome must be success, partial, failure, or unknown")
        learnings = raw.get("learnings")
        need(isinstance(learnings, list) and len(learnings) <= 8, "learnings must be a list of up to 8 meaningful findings")
        if not learnings:
            nonempty(raw.get("skip_reason"), "skip_reason", 1500)
        result = {"summary": summary, "outcome": raw["outcome"], "learnings": [], "skip_reason": raw.get("skip_reason")}
        seen_content = set()
        for item in learnings:
            need(isinstance(item, dict), "learning must be an object")
            need(set(item) <= {"title", "lesson", "scope", "when_to_use", "failure_reason", "validation", "evidence"}, "unknown learning fields; no self-promotion fields permitted")
            n = {key: nonempty(item.get(key), key, 6000 if key == "lesson" else 1200) for key in ("title", "lesson", "scope")}
            condition = item.get("when_to_use", [])
            if isinstance(condition, str): condition = [condition]
            need(isinstance(condition, list) and len(condition) <= 10 and all(isinstance(x, str) and x.strip() and len(x) <= 1200 for x in condition), "when_to_use must contain up to 10 short conditions")
            n["when_to_use"] = condition
            failure = item.get("failure_reason")
            need(failure is None or isinstance(failure, str) and len(failure) <= 3000, "failure_reason must be text or null")
            n["failure_reason"] = failure
            content_key = digest({k: n[k] for k in ("title", "lesson", "scope", "failure_reason")})
            need(content_key not in seen_content, "duplicate learning in one outcome; combine its evidence into one entry")
            seen_content.add(content_key)
            validation = item.get("validation")
            need(isinstance(validation, dict) and set(validation) == {"method", "result", "scope"}, "validation must contain method, result, scope")
            n["validation"] = {k: nonempty(validation.get(k), "validation." + k, 2000) for k in ("method", "result", "scope")}
            need(n["validation"]["result"] in {"pass", "fail", "partial", "not_run", "unknown"}, "validation.result must be pass, fail, partial, not_run, or unknown")
            evidence = item.get("evidence")
            need(isinstance(evidence, list) and 1 <= len(evidence) <= 12, "each learning needs 1..12 existing local evidence files")
            n["evidence"] = []
            for ref in evidence:
                need(isinstance(ref, dict) and set(ref) <= {"path", "line"}, "evidence accepts path and optional line only; hash is computed")
                path = Path(nonempty(ref.get("path"), "evidence.path", 4000))
                path = (path if path.is_absolute() else Path(project_root) / path).resolve()
                need(path.is_file(), "evidence file does not exist: " + str(path))
                need(path.name.lower() not in SENSITIVE_NAMES and not path.name.lower().startswith('.env.'), "credential/config secret files are not evidence inputs")
                stat = path.stat(); need(stat.st_size <= MAX_EVIDENCE_BYTES, "evidence file exceeds 16 MiB; produce a bounded evidence artifact")
                line = ref.get("line")
                need(line is None or type(line) is int and line >= 1, "evidence line must be a positive integer")
                hasher = hashlib.sha256()
                with path.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""): hasher.update(block)
                after = path.stat()
                need((after.st_size, after.st_mtime_ns) == (stat.st_size, stat.st_mtime_ns), "evidence changed while hashing; retry with a stable artifact")
                normalized = {"path": str(path), "sha256": hasher.hexdigest(), "bytes": stat.st_size, "kind": "local_task_artifact", "captured_at": utcnow()}
                if line is not None: normalized["line"] = line
                n["evidence"].append(normalized)
            result["learnings"].append(n)
        return result

    def plan(self, kb, receipt, outcome):
        nodes, edges, lesson_ids, existing_ids, observation_ids = [], [], [], [], []
        # Legacy receipts cannot establish which cards survived their response budget.
        delivered_ids = receipt.get("delivered_node_ids", [])
        pid = self.project_id(kb, receipt["project_root"])
        def edge(source, target, relation, evidence):
            identifier = "edge:task-" + digest([source, target, relation])[:32]
            if identifier not in kb.edges:
                edges.append({"id": identifier, "source": source, "target": target, "relation": relation, "summary": "Task evidence link; candidate scope still requires review", "evidence": evidence})
        for i, learning in enumerate(outcome["learnings"]):
            identity = {k: learning[k] for k in ("title", "lesson", "scope", "failure_reason")}
            fp = digest({"project": project_key(receipt["project_root"]), "content": identity})
            nid = "lesson:task-" + fp[:32]
            evidence = learning["evidence"]
            signature = digest({"validation": learning["validation"], "outcome": outcome["outcome"], "evidence": [{k: v for k, v in e.items() if k != "captured_at"} for e in evidence]})
            existing = kb.nodes.get(nid)
            if existing:
                existing_ids.append(nid)
                need(existing["properties"].get("fingerprint") == fp, "learning ID collision; refusal to overwrite")
                previously_observed = any(n["kind"] == "observation" and n["properties"].get("lesson_id") == nid and n["properties"].get("observation_signature") == signature for n in kb.nodes.values())
                if existing["properties"].get("observation_signature") == signature or previously_observed:
                    continue
                oid = "observation:task-" + receipt["receipt_id"] + "-" + str(i)
                need(oid not in kb.nodes, "observation ID already exists without matching transaction")
                observation_ids.append(oid)
                nodes.append({"id": oid, "kind": "observation", "title": learning["title"] + " · 新证据", "summary": outcome["summary"], "status": "candidate", "tags": ["task-cycle", receipt["client"]], "evidence": evidence,
                              "properties": {"lesson_id": nid, "project_id": pid, "scope": learning["scope"], "failure_reason": learning["failure_reason"], "validation": learning["validation"], "task_outcome": outcome["outcome"], "client": receipt["client"], "task_id": receipt["task_id"], "needs_review": True, "observation_signature": signature}})
                edge(oid, nid, "reobserves", evidence); edge(oid, pid, "occurred_in", evidence)
                continue
            lesson_ids.append(nid)
            nodes.append({"id": nid, "kind": "lesson", "title": learning["title"], "summary": learning["lesson"], "status": "candidate", "tags": ["task-cycle", receipt["client"], Path(receipt["project_root"]).name], "evidence": evidence,
                          "properties": {"fingerprint": fp, "observation_signature": signature, "project_id": pid, "project_root": receipt["project_root"], "scope": learning["scope"], "when_to_use": learning["when_to_use"], "failure_reason": learning["failure_reason"], "validation": learning["validation"], "task_outcome": outcome["outcome"], "client": receipt["client"], "task_id": receipt["task_id"], "retrieved_node_ids": list(delivered_ids), "effectiveness": {"status": "candidate_observation", "interpretation": "Evidence path/hash checked; truth and general applicability are not automatically certified."}}})
            edge(nid, pid, "observed_after", evidence)
            for source in delivered_ids[:3]:
                if source in kb.nodes:
                    edge(nid, source, "retrieval_context", evidence)
        if nodes and pid not in kb.nodes:
            refs = outcome["learnings"][0]["evidence"]
            nodes.insert(0, {"id": pid, "kind": "project", "title": Path(receipt["project_root"]).name or receipt["project_root"], "summary": "任务工作目录；不自动视为独立业务项目或生产验收", "status": "candidate", "tags": ["task-cycle", "工作目录"], "evidence": refs, "properties": {"project_root": receipt["project_root"], "cwd": receipt["project_root"], "scope": "observed task workspace"}})
        return {"nodes": nodes, "edges": edges, "expected_revision": kb.revision}, {"new_lesson_ids": lesson_ids, "existing_lesson_ids": existing_ids, "new_observation_ids": observation_ids, "project_id": pid, "duplicate_detection": "exact content fingerprint within project; paraphrases need semantic review"}

    def finish(self, receipt_file, raw):
        path = self.receipt_path(receipt_file)
        need(path.is_file(), "receipt does not exist")
        input_hash = digest(raw)
        with self.receipt_lock(path):
            receipt = self.load_receipt(path)
            if receipt.get("finish_input_hash"):
                need(receipt["finish_input_hash"] == input_hash, "receipt already has different finish content")
            if receipt["status"] == "finished":
                return {**receipt["finish_result"], "replayed": True}
            if receipt.get("pending"):
                normalized = receipt["pending"]["normalized_outcome"]
            else:
                normalized = self.normalize_outcome(raw, receipt["project_root"])
                receipt["finish_input_hash"] = input_hash
                receipt["pending"] = {"normalized_outcome": normalized}
                self.write_receipt(path, receipt)
            if not normalized["learnings"]:
                result = {"ok": True, "receipt": str(path), "written": False, "reason": normalized["skip_reason"], "status": "finished", "new_lesson_ids": [], "replayed": False}
            else:
                actor = "task-cycle:" + receipt["client"]
                key = "task-cycle-finish:" + receipt["receipt_id"]
                result = None
                for attempt in range(8):
                    kb = KnowledgeBase(self.root)
                    pending = receipt["pending"]
                    if "proposal" not in pending:
                        proposal, detail = self.plan(kb, receipt, normalized)
                        pending.update({"proposal": proposal, "detail": detail})
                        self.write_receipt(path, receipt)
                    proposal, detail = pending["proposal"], pending["detail"]
                    if not proposal["nodes"] and not proposal["edges"]:
                        result = {"ok": True, "receipt": str(path), "written": False, "reason": "same project/content and evidence already recorded; no duplicate graph entry", "status": "finished", **detail, "replayed": False}
                        break
                    try:
                        committed = kb.propose(proposal, actor, key)
                        result = {"ok": True, "receipt": str(path), "written": True, "status": "finished", "knowledge_status": "candidate", **detail, **committed}
                        break
                    except KBError as exc:
                        message = str(exc)
                        if "revision conflict:" in message:
                            # Only independent additions are replanned. Existing nodes are never overwritten.
                            pending.pop("proposal", None); pending.pop("detail", None)
                            self.write_receipt(path, receipt)
                        elif "writer lock exists" not in message:
                            raise
                        time.sleep(min(0.04 * (2 ** attempt), 0.4))
                need(result is not None, "knowledge writer busy after bounded retries; repeat the same finish input later")
            receipt["status"] = "finished"; receipt["finished_at"] = utcnow(); receipt["finish_result"] = result
            receipt.pop("pending", None)
            self.write_receipt(path, receipt)
            return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    sub = parser.add_subparsers(dest="command", required=True)
    begin = sub.add_parser("begin")
    begin.add_argument("--client", choices=["codex", "dsh", "other"], required=True)
    begin.add_argument("--project-root", required=True); begin.add_argument("--query", required=True)
    begin.add_argument("--task-id"); begin.add_argument("--budget", type=int, default=5000)
    finish = sub.add_parser("finish"); finish.add_argument("--receipt", required=True); finish.add_argument("--file", required=True)
    args = parser.parse_args(argv)
    try:
        cycle = TaskCycle(args.root)
        result = cycle.begin(args.client, args.project_root, args.query, args.task_id, args.budget) if args.command == "begin" else cycle.finish(args.receipt, json_file(args.file))
        print(dumps(result)); return 0
    except (KBError, OSError, ValueError, TypeError, KeyError) as exc:
        print(dumps({"ok": False, "error": str(exc)})); return 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"): sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
