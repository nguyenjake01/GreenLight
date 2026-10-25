"""``python -m verifier.ats.apply <fixture> | --list | --replay <application_id> [--tamper]``

Drives the mock ATS from a terminal (design doc J3). The mock must be running for a new
application; ``--replay`` talks to the verifier directly.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

from verifier.contracts.config import env
from verifier.core import store

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def list_fixtures() -> list[str]:
    return sorted(p.stem for p in (FIXTURES / "webhooks").glob("*.json"))


def apply_fixture(fixture: str, *, client: httpx.Client | None = None) -> tuple[int, dict]:
    """POST the mock's `/_mock/apply`; returns (HTTP status, JSON body)."""
    url = env("ATS_PUBLIC_URL").rstrip("/") + "/_mock/apply"
    owner = client or httpx.Client()
    try:
        resp = owner.post(url, json={"fixture": fixture}, timeout=30.0)
    finally:
        if client is None:
            owner.close()
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, {"body": resp.text[:300]}


def saved_webhook(application_id: str) -> tuple[bytes, dict[str, str]]:
    """The run's saved webhook: the raw body and the headers it came with."""
    rd = Path(env("RUNS_DIR")) / str(application_id)
    body_path, headers_path = rd / "webhook.json", rd / "webhook_headers.json"
    if not body_path.is_file() or not headers_path.is_file():
        raise FileNotFoundError(f"no saved webhook for application {application_id} in {rd}")
    saved = store.read_json(headers_path)
    return body_path.read_bytes(), {"Signature": saved["signature"],
                                    "Greenhouse-Event-ID": saved["event_id"],
                                    "Content-Type": "application/json"}


def tamper(raw: bytes) -> bytes:
    """Change exactly one byte (so the signature no longer matches)."""
    i = len(raw) // 2
    return raw[:i] + bytes([raw[i] ^ 0x01]) + raw[i + 1:]


def replay(application_id: str, *, tampered: bool = False,
           client: httpx.Client | None = None) -> tuple[int, dict]:
    """Re-send the saved webhook straight to VERIFIER_WEBHOOK_URL; returns (status, body)."""
    raw, headers = saved_webhook(application_id)
    if tampered:
        raw = tamper(raw)
    owner = client or httpx.Client()
    try:
        resp = owner.post(env("VERIFIER_WEBHOOK_URL"), content=raw, headers=headers, timeout=10.0)
    finally:
        if client is None:
            owner.close()
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, {"body": resp.text[:300]}


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(prog="python -m verifier.ats.apply",
                                 description="Simulate a new application against the mock ATS.")
    ap.add_argument("fixture", nargs="?", help="fixture name, e.g. henry (see --list)")
    ap.add_argument("--list", action="store_true", help="list the available fixtures")
    ap.add_argument("--replay", metavar="APPLICATION_ID",
                    help="re-send that run's saved webhook to the verifier")
    ap.add_argument("--tamper", action="store_true", help="with --replay: change one byte first")
    args = ap.parse_args(argv)

    if args.list:
        print("\n".join(list_fixtures()))
        return 0
    if args.tamper and not args.replay:
        ap.error("--tamper only works with --replay")
    try:
        if args.replay:
            status, body = replay(args.replay, tampered=args.tamper)
            print(f"HTTP {status} {json.dumps(body)}")
            return 0
        if not args.fixture:
            ap.error("give a fixture name (see --list), or --replay <application_id>")
        status, body = apply_fixture(args.fixture)
    except FileNotFoundError as e:
        print(f"apply: {e}", file=sys.stderr)
        return 1
    except httpx.HTTPError as e:
        print(f"apply: could not reach the server: {e}", file=sys.stderr)
        return 1
    print(json.dumps(body, indent=2))
    return 0 if status == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
