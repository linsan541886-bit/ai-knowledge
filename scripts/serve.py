#!/usr/bin/env python3
"""Loopback, read-only UI and graph API. No source-file or write endpoints."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
from urllib.parse import parse_qs, urlsplit

from kb import KBError, KnowledgeBase, dumps


class GraphServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, root):
        self.root = Path(root).resolve()
        self.cache_lock = threading.Lock()
        self.cached_signature = None
        self.cached_kb = None
        super().__init__(address, Handler)

    def knowledge(self):
        names = ("nodes.jsonl", "edges.jsonl", "baseline_manifest.json", "ledger.jsonl")
        signature = []
        for name in names:
            p = self.root / "data" / name
            stat = p.stat() if p.exists() else None
            signature.append((name, stat.st_mtime_ns, stat.st_size) if stat else (name, None, None))
        with self.cache_lock:
            if signature != self.cached_signature:
                self.cached_kb = KnowledgeBase(self.root)
                self.cached_signature = signature
            return self.cached_kb


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def respond(self, status, body, content_type="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        allowed_hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        if self.headers.get("Host", "").lower() not in allowed_hosts:
            self.respond(403, dumps({"ok": False, "error": "local Host header required"}))
            return
        route = urlsplit(self.path)
        if route.path == "/favicon.ico":
            self.respond(204, b"")
            return
        static = {"/": ("index.html", "text/html; charset=utf-8"), "/index.html": ("index.html", "text/html; charset=utf-8"),
                  "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/style.css": ("style.css", "text/css; charset=utf-8")}
        try:
            if route.path in static:
                name, mime = static[route.path]
                path = self.server.root / "ui" / name
                if not path.resolve().is_relative_to(self.server.root / "ui"):
                    raise KBError("static path escapes UI directory")
                self.respond(200, path.read_bytes(), mime)
                return
            parameters = parse_qs(route.query, keep_blank_values=True)
            def param(name, default=None):
                values = parameters.get(name)
                if values is None:
                    return default
                if len(values) != 1:
                    raise KBError("duplicate query parameter")
                return values[0]
            kb = self.server.knowledge()
            action = route.path
            if action == "/api/overview":
                result = kb.overview()
            elif action == "/api/stats":
                result = kb.stats()
            elif action == "/api/health":
                result = kb.health()
            elif action == "/api/node":
                result = kb.get(param("id"))
            elif action == "/api/search":
                synthetic_flag = param("exclude_synthetic", "false")
                if synthetic_flag not in {"true", "false"}:
                    raise KBError("exclude_synthetic must be true or false")
                result = kb.search(param("q", ""), int(param("limit", "8")), int(param("offset", "0")), param("kind"), param("include_inactive", "false") == "true", param("outcome"), synthetic_flag == "true")
            elif action == "/api/recommend":
                result = kb.recommend(param("q", ""), int(param("limit", "8")))
            elif action == "/api/expand":
                result = kb.expand(param("id"), int(param("limit", "8")), int(param("offset", "0")), param("relation"))
            elif action == "/api/context":
                self.respond(200, kb.context(param("id"), int(param("budget", "4000"))))
                return
            elif action == "/api/history":
                result = kb.history(param("id"))
            else:
                self.respond(404, dumps({"ok": False, "error": "unknown route"}))
                return
            self.respond(200, dumps(result))
        except (KBError, ValueError, TypeError, OSError) as exc:
            self.respond(400, dumps({"ok": False, "error": str(exc)}))

    def do_POST(self):
        self.respond(405, dumps({"ok": False, "error": "read-only HTTP service; use validated CLI or MCP write tools"}))

    do_PUT = do_POST
    do_DELETE = do_POST
    do_PATCH = do_POST


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--port", type=int, default=18766)
    args = parser.parse_args()
    server = GraphServer(("127.0.0.1", args.port), args.root)
    print(json.dumps({"url": f"http://127.0.0.1:{server.server_port}/", "mode": "read-only", "root": str(server.root)}, ensure_ascii=False), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
