"""Fake verifier internal API for testing the OpenClaw plugin (H4/H5) without Jake's service.

Stdlib only. Implements the endpoints of design doc 8.4 well enough to exercise the plugin:

    POST /internal/actions   logs the payload, returns {"ack_payload": null}
    GET  /internal/verdict   ?candidate=   200 sample verdict | 404
    POST /internal/recheck   {"candidate", "by"}   202 | 404
    GET  /internal/explain   ?candidate=&claim=    200 sample explain | 404
    GET  /internal/health

Every /internal/* request needs header X-Verifier-Token (401 {"error": "unauthorized"} otherwise).
Every request is logged as one JSON line on stdout and kept in memory:

    GET  /__requests   -> JSON list of what was received (method, path, query, headers, body)
    POST /__reset      -> clears the list

Test hooks (data-driven, so the plugin tests need no extra config):
    candidate "Henry" / "12345"      known application (verdict has other_matches = [])
    candidate "Alex"                 known, verdict.other_matches is non-empty
    candidate "boom"                 500
    user.id "U_SLOW" in /actions     sleeps 3 s before answering (timeout test)
    edit_claim submit whose `behavior` input is "INVALID"
                                     answers {"ack_payload": {"response_action": "errors", ...}}

Run:  python tests/stack/fake_actions_server.py --port 18299 [--token test-token]
      --port 0 picks a free port; the chosen port is printed as "LISTENING <port>".
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

KNOWN = {"henry", "12345", "alex", "cubesolver-candidate"}

VERDICT = {
    "application_id": "12345",
    "candidate_name": "Henry Chen",
    "job_title": "Software Engineer",
    "card_permalink": "https://example.slack.com/archives/C0HIRING/p1761400000000100",
    "runs": {"c3": "done"},
    "claims": [
        {
            "claim_id": "c3",
            "type": "project",
            "source_text": "Built cubesolver, a full Rubik's cube solver in C++",
            "label": "yellow",
        }
    ],
    "verdict": {
        "claims": [{"claim_id": "c3", "label": "yellow", "top_reason": "Repo is 3 KB with 2 commits on one day."}],
        "tally": {"green": 0, "yellow": 1, "red": 0, "white": 0},
    },
    "other_matches": [],
}

EXPLAIN = {
    "claim_id": "c3",
    "type": "project",
    "source_text": "Built cubesolver, a full Rubik's cube solver in C++",
    "label": "yellow",
    "top_reason": "The resume calls it a full solver, but the repo is 3 KB with 2 commits on one day.",
    "checks": [
        {"name": "repo_exists", "result": "pass", "reason": "Repo created Dec 2024, matching the claim."},
        {"name": "substance", "result": "weak", "reason": "3 KB, 2 commits, both by the candidate."},
    ],
    "notes": [],
    "flags": [],
    "evidence_links": ["https://github.com/henrychen4736/cubesolver"],
    "run": None,
}

_requests: list[dict] = []
_lock = threading.Lock()
_token = "test-token"


def _is_known(candidate: str) -> bool:
    return candidate.strip().lower() in KNOWN


class Handler(BaseHTTPRequestHandler):
    server_version = "FakeActions/1"

    def log_message(self, format, *args):  # noqa: A002 - silence default stderr logging
        pass

    # ---- helpers -------------------------------------------------------------------------
    def _send(self, status: int, body) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        if not raw:
            return None
        if ctype == "application/x-www-form-urlencoded":
            form = parse_qs(raw.decode())
            if "payload" in form:
                return json.loads(form["payload"][0])
            return {k: v[0] for k, v in form.items()}
        try:
            return json.loads(raw.decode())
        except json.JSONDecodeError:
            return {"_raw": raw.decode(errors="replace")}

    def _record(self, parsed, body) -> None:
        entry = {
            "method": self.command,
            "path": parsed.path,
            "query": {k: v[0] for k, v in parse_qs(parsed.query).items()},
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": body,
        }
        with _lock:
            _requests.append(entry)
        print(json.dumps({"fake_actions": entry}), flush=True)

    def _authorized(self) -> bool:
        return self.headers.get("X-Verifier-Token") == _token

    # ---- routing -------------------------------------------------------------------------
    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/__requests":
            with _lock:
                return self._send(200, list(_requests))
        self._record(parsed, None)
        if not self._authorized():
            return self._send(401, {"error": "unauthorized"})
        q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        if parsed.path == "/internal/health":
            return self._send(200, {"status": "ok", "real_modules": [], "fetch_mode": "replay"})
        if parsed.path == "/internal/verdict":
            cand = q.get("candidate", "")
            if cand.lower() == "boom":
                return self._send(500, {"error": "internal"})
            if not _is_known(cand):
                return self._send(404, {"error": "not_found"})
            body = dict(VERDICT)
            if cand.lower() == "alex":
                body = {**VERDICT, "candidate_name": "Alex Doe", "other_matches": [
                    {"application_id": "777", "candidate_name": "Alex Roe"}]}
            return self._send(200, body)
        if parsed.path == "/internal/explain":
            cand = q.get("candidate", "")
            if cand.lower() == "boom":
                return self._send(500, {"error": "internal"})
            if not _is_known(cand):
                return self._send(404, {"error": "not_found"})
            return self._send(200, EXPLAIN)
        return self._send(404, {"error": "not_found"})

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/__reset":
            with _lock:
                _requests.clear()
            return self._send(200, {"ok": True})
        body = self._read_body()
        self._record(parsed, body)
        if not self._authorized():
            return self._send(401, {"error": "unauthorized"})
        if parsed.path == "/internal/actions":
            return self._actions(body)
        if parsed.path == "/internal/recheck":
            cand = str((body or {}).get("candidate", ""))
            if cand.lower() == "boom":
                return self._send(500, {"error": "internal"})
            if not _is_known(cand):
                return self._send(404, {"error": "not_found"})
            return self._send(202, {"application_id": "12345", "status": "started"})
        return self._send(404, {"error": "not_found"})

    def _actions(self, body):
        body = body or {}
        if (body.get("user") or {}).get("id") == "U_SLOW":
            time.sleep(3)
        if body.get("type") == "view_submission":
            view = body.get("view") or {}
            values = ((view.get("state") or {}).get("values")) or {}
            behavior = (((values.get("behavior") or {}).get("v")) or {}).get("value")
            if str(view.get("callback_id", "")).startswith("openclaw:rv:edit_claim") and behavior == "INVALID":
                return self._send(200, {"ack_payload": {
                    "response_action": "errors",
                    "errors": {"behavior": "Behavior can't be INVALID"},
                }})
        return self._send(200, {"ack_payload": None})


def main(argv=None) -> int:
    global _token
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8299)
    ap.add_argument("--token", default="test-token", help="expected X-Verifier-Token")
    args = ap.parse_args(argv)
    _token = args.token
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    print(f"LISTENING {server.server_address[1]}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
