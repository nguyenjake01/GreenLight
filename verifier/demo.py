"""``python -m verifier.demo --stub [--candidate henry|fake] [--real fetch,rubric,…]`` (design doc §8.8).

Gives the candidate's fixture webhook fresh ids, signs it, passes it through the ``ats``
adapter's ``handle_webhook``, and runs the pipeline. Then checks the run itself and exits
1 with what failed.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import random
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SLACK_KEYS = {"seq", "call", "channel", "ts", "thread_ts", "text", "n_blocks", "path"}


def _fresh_webhook(candidate: str, secret: str) -> tuple[bytes, dict[str, str]]:
    """The fixture webhook with fresh ids and event id, serialized (ASCII-escaped) and signed."""
    from verifier.contracts.config import env

    body = json.loads((FIXTURES / "webhooks" / f"{candidate}.json").read_text(encoding="utf-8"))
    app = body["payload"]["application"]
    app_id = int(time.time() * 1000) % 10**9 * 10 + random.randrange(10)
    cand_id = app_id + 1
    app["id"] = app_id
    app["candidate"]["id"] = cand_id
    app["applied_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    app["url"] = f"{env('ATS_PUBLIC_URL')}/candidates/{cand_id}?application_id={app_id}"
    raw = json.dumps(body).encode("utf-8")
    signature = "sha256 " + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {"Signature": signature, "Greenhouse-Event-ID": uuid.uuid4().hex}


def _slack_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _find(lines: list[dict], start: int, pred) -> int | None:
    for i in range(start, len(lines)):
        if pred(lines[i]):
            return i
    return None


def check_run(rd: Path, new_slack: list[dict] | None, store) -> list[str]:
    """Return a list of failure messages (empty = pass). `new_slack` None skips Slack checks."""
    failures: list[str] = []

    def need(ok: bool, msg: str) -> None:
        if not ok:
            failures.append(msg)

    need((rd / "claims.v1.json").is_file(), f"{rd}/claims.v1.json missing")
    snaps = [p for p in (rd / "snapshots").glob("*/evidence.json")] if (rd / "snapshots").is_dir() else []
    need(bool(snaps), "no snapshot with evidence.json")
    verdicts = sorted((rd / "verdicts").glob("verdict_*.json"),
                      key=lambda p: int(p.stem.split("_")[1])) if (rd / "verdicts").is_dir() else []
    need(bool(verdicts), "no verdicts/verdict_*.json")
    need(bool(list((rd / "annotated").glob("*.pdf"))) if (rd / "annotated").is_dir() else False,
         "no annotated/*.pdf")
    need((rd / "writeback_stub").is_dir() and any((rd / "writeback_stub").iterdir()),
         "writeback_stub/ missing or empty")

    if new_slack is None or not verdicts:
        return failures

    for line in new_slack:
        if set(line) != SLACK_KEYS:
            failures.append(f"slack stub line has wrong keys: {sorted(line)}")
            break

    latest = store.read_json(verdicts[-1])
    has_runs = False
    for vc in latest["claims"]:
        if vc["type"] == "project" and any(
                c["check"] == "repo" and c["result"] == "pass" for c in vc["checks"]):
            has_runs = True

    # Ordered: post_card, update_card (same ts), [post_thread under it, update_card], upload_file.
    i = _find(new_slack, 0, lambda l: l["call"] == "post_card")
    if i is None:
        failures.append("slack: no placeholder post_card")
        return failures
    card_ts = new_slack[i]["ts"]
    on_card = lambda l: l["call"] == "update_card" and l["ts"] == card_ts  # noqa: E731
    j = _find(new_slack, i + 1, on_card)
    if j is None:
        failures.append("slack: no update_card of the placeholder (same ts) after post_card")
        return failures
    k = j
    if has_runs:
        k = _find(new_slack, j + 1,
                  lambda l: l["call"] == "post_thread" and l["thread_ts"] == card_ts)
        if k is None:
            failures.append("slack: verdict has a passing repo check but no run-line post_thread "
                            "under the card")
            return failures
        k = _find(new_slack, k + 1, on_card)
        if k is None:
            failures.append("slack: no later update_card of the card after the run line")
            return failures
    if _find(new_slack, k + 1,
             lambda l: l["call"] == "upload_file" and l["thread_ts"] == card_ts) is None:
        failures.append("slack: no upload_file into the card's thread after the last card update")
    return failures


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(prog="python -m verifier.demo", description=__doc__.splitlines()[0])
    ap.add_argument("--stub", action="store_true", help="run every module as a stub")
    ap.add_argument("--candidate", default="henry", choices=["henry", "fake"])
    ap.add_argument("--real", default="", help="comma list of modules to run real")
    args = ap.parse_args(argv)

    real = [r.strip() for r in args.real.split(",") if r.strip()]
    if not args.stub and not real:
        ap.error("pass --stub, or --real <modules>")
    if "ats" in real or "all" in real:
        print("demo: --real ats is not supported; use the mock ATS and `python -m verifier.ats.apply` "
              "(J3), because the real adapter downloads resumes from the running mock.",
              file=sys.stderr)
        return 2
    os.environ["VERIFIER_REAL"] = ",".join(real)

    from verifier.contracts import registry
    from verifier.contracts.config import env
    from verifier.core import pipeline, store

    secret = env("GREENHOUSE_WEBHOOK_SECRET") or "dev-secret"
    raw, headers = _fresh_webhook(args.candidate, secret)

    slack_log = Path(env("RUNS_DIR")) / "_slack_stub.jsonl"
    before = len(_slack_lines(slack_log))

    adapter = registry.get("ats").GreenhouseAdapter(
        webhook_secret=secret, harvest_auth_url=env("HARVEST_AUTH_URL"),
        harvest_base_url=env("HARVEST_BASE_URL"), client_id=env("HARVEST_CLIENT_ID"),
        client_secret=env("HARVEST_CLIENT_SECRET"), note_visibility=env("NOTE_VISIBILITY"))
    captured: list[dict] = []
    adapter.on_new_application(captured.append)
    status = adapter.handle_webhook(raw, headers)
    if status != "accepted" or len(captured) != 1:
        print(f"demo FAILED: webhook was {status!r}, {len(captured)} application(s) captured")
        return 1
    app = captured[0]
    print(f"demo: {args.candidate} -> application {app['application_id']} "
          f"(real: {','.join(real) or 'none'})")

    started = time.time()
    pipeline.run(app, wait=True)
    print(f"demo: pipeline finished in {time.time() - started:.2f}s")

    rd = store.run_dir(app["application_id"])
    new_slack = None if "slack" in real else _slack_lines(slack_log)[before:]
    failures = check_run(rd, new_slack, store)
    if failures:
        print("demo FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"demo OK: {rd}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
