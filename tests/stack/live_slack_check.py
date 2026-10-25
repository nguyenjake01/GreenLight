"""Live H3 done-line check (not collected by pytest). Needs SLACK_BOT_TOKEN and SLACK_CHANNEL_ID
in .env, and the bot invited to the channel (scopes chat:write, files:write, files:read).

    .venv/bin/python tests/stack/live_slack_check.py

Posts the card from render_card(sample verdict), edits it in place, replies in its thread,
uploads fixtures/resumes/henry.pdf into the thread, then fetches the card's permalink.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from verifier.contracts import registry  # noqa: E402
from verifier.contracts.config import env  # noqa: E402
from verifier.stack import slack  # noqa: E402


def main() -> int:
    if not env("SLACK_BOT_TOKEN") or not env("SLACK_CHANNEL_ID"):
        print("SLACK_BOT_TOKEN / SLACK_CHANNEL_ID not set in .env; nothing to check.")
        return 2
    channel = env("SLACK_CHANNEL_ID")

    verdict = json.loads((ROOT / "fixtures/verdicts/henrychen4736.json").read_text())
    claims = json.loads((ROOT / "fixtures/claims/henrychen4736.json").read_text())
    app = {
        "application_id": verdict["application_id"], "candidate_id": "1", "candidate_name": "Henry Chen",
        "emails": [], "website_addresses": [], "social_media_addresses": [],
        "job_title": "Software Engineer", "applied_at": "2026-10-25T10:00:00Z",
        "profile_url": "http://localhost:8100/candidates/1", "resume": None, "event_id": "live-check",
    }
    meta = {k: app[k] for k in ("application_id", "candidate_id", "candidate_name", "job_title",
                                "event_id", "applied_at")}
    opts = {"profile_url": app["profile_url"], "annotated_pdf_url": None, "action_buttons": False,
            "menu": []}
    render = registry.get("render")  # stub or real
    card = render.render_card(app, claims, verdict, meta, opts)

    ref = slack.post_card(channel, render.render_placeholder(app))
    print("1. posted placeholder:", ref)
    slack.update_card(ref, card)
    print(f"2. updated in place with the card ({len(card['blocks'])} blocks)")
    reply = slack.post_thread(ref, {"text": "Thread reply from the verifier (H3 live check)",
                                    "blocks": []})
    print("3. thread reply:", reply)
    up = slack.upload_file(ref, ROOT / "fixtures/resumes/henry.pdf", title="henry.pdf",
                           comment="Resume PDF (H3 live check)")
    print("4. uploaded:", up)
    print("5. card permalink:", slack.get_permalink(ref))
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
