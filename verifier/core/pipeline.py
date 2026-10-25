"""Orchestrator v0: the thin runner (design doc §5.1, J1).

``run`` walks every §5.1 stage in order, in the calling thread. No threads, locks,
``meta.json`` or stage error handling: an exception stops the run. J7 replaces this file.
Modules are reached only through ``registry.get``; ``runs/`` is written only through ``store``.
"""
from __future__ import annotations

import re
from pathlib import Path

from verifier.contracts import registry
from verifier.contracts.config import env
from verifier.contracts.errors import ResumeError
from verifier.contracts.types import (
    Application, CardOptions, ClaimsDoc, Meta, RunTrace, Verdict,
)
from verifier.core import store


def _adapter():
    return registry.get("ats").GreenhouseAdapter(
        webhook_secret=env("GREENHOUSE_WEBHOOK_SECRET"),
        harvest_auth_url=env("HARVEST_AUTH_URL"),
        harvest_base_url=env("HARVEST_BASE_URL"),
        client_id=env("HARVEST_CLIENT_ID"),
        client_secret=env("HARVEST_CLIENT_SECRET"),
        note_visibility=env("NOTE_VISIBILITY"),
    )


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "_", name.lower())


def _passing_repos(verdict: Verdict) -> list[tuple[str, str]]:
    """(claim_id, "owner/repo") for each project claim whose `repo` check passed."""
    out: list[tuple[str, str]] = []
    for vc in verdict["claims"]:
        if vc["type"] != "project":
            continue
        for chk in vc["checks"]:
            repo = (chk.get("facts") or {}).get("repo")
            if chk["check"] == "repo" and chk["result"] == "pass" and repo:
                out.append((vc["claim_id"], repo))
                break
    return out


def run(app: Application, *, wait: bool = False) -> None:
    """Run every stage for one application. `wait` is accepted and ignored (J7 gives it meaning)."""
    resume_m, extract_m = registry.get("resume"), registry.get("extract")
    slack, render = registry.get("slack"), registry.get("render")
    fetch_m, rubric = registry.get("fetch"), registry.get("rubric")
    annotate, harness = registry.get("annotate"), registry.get("harness")
    adapter = _adapter()

    app_id = app["application_id"]
    rd = store.run_dir(app_id)
    mode = env("GITHUB_FETCH_MODE")
    meta: Meta = {
        "application_id": app_id, "candidate_id": app["candidate_id"],
        "candidate_name": app["candidate_name"], "job_title": app["job_title"],
        "event_id": app["event_id"], "applied_at": app["applied_at"],
        "latest": {"claims_version": None, "snapshot_id": None, "verdict_n": None},
        "slack": {"channel": env("SLACK_CHANNEL_ID"), "card_ts": None,
                  "card_permalink": None, "pdf_permalink": None},
        "runs": {}, "stage_errors": [], "writebacks": [],
    }
    opts: CardOptions = {
        "profile_url": app["profile_url"], "annotated_pdf_url": None,
        "action_buttons": True, "menu": ["more_context", "suggest_questions", "edit_claim"],
    }

    # 1. Resume: download, read, save text and links, link the GitHub login.
    if app.get("resume") is None:
        raise ResumeError("application has no resume")
    pdf = adapter.fetch_resume(app, rd / "resume.pdf")
    resume = resume_m.read_resume(pdf)
    store.write_text(rd / "resume.txt", resume["text"])
    store.write_json(rd / "resume_links.json", resume["links"])
    candidate = resume_m.link_github(resume, app)

    # 2. Placeholder card.
    card = slack.post_card(env("SLACK_CHANNEL_ID"), render.render_placeholder(app))
    meta["slack"]["card_ts"] = card["ts"]
    meta["slack"]["card_permalink"] = slack.get_permalink(card)

    # 3. Claims.
    claims: ClaimsDoc = extract_m.extract_claims(resume, candidate, application_id=app_id)
    store.write_json(rd / f"claims.v{claims['claims_version']}.json", claims)
    meta["latest"]["claims_version"] = claims["claims_version"]
    claims_by_id = {c["claim_id"]: c for c in claims["claims"]}

    # 4. Evidence and first verdict.
    snapshot = fetch_m.fetch_evidence(claims, run_dir=rd, mode=mode)
    meta["latest"]["snapshot_id"] = snapshot.snapshot_id
    traces: list[RunTrace] = []

    def new_verdict() -> Verdict:
        n = store.next_verdict_n(rd)
        v = rubric.compute_verdict(claims, snapshot, traces, verdict_n=n,
                                   computed_at=store.now_iso())
        store.write_json(rd / "verdicts" / f"verdict_{n}.json", v)
        meta["latest"]["verdict_n"] = n
        return v

    def annotated(v: Verdict) -> Path:
        n = meta["latest"]["verdict_n"]
        out = rd / "annotated" / f"{_slug(app['candidate_name'])}_resume_verified_{n}.pdf"
        annotate.annotate_pdf(rd / "resume.pdf", claims, v, out)
        return out

    def refresh_card(v: Verdict) -> None:
        slack.update_card(card, render.render_card(app, claims, v, meta, opts))

    verdict = new_verdict()

    # 5. Card in place, annotated PDF into the thread.
    refresh_card(verdict)
    pdf_out = annotated(verdict)
    up = slack.upload_file(card, pdf_out, title=pdf_out.name)
    meta["slack"]["pdf_permalink"] = opts["annotated_pdf_url"] = up["permalink"]
    refresh_card(verdict)

    # 6. Run check per project claim whose repo check passed.
    for claim_id, repo in _passing_repos(verdict):
        claim = claims_by_id[claim_id]
        meta["runs"][claim_id] = "running"
        line = slack.post_thread(card, render.render_run_update(app_id, claim, "started", None))
        trace = harness.run_check(snapshot, claim, repo, mode=mode)
        meta["runs"][claim_id] = "done"
        slack.update_card(line, render.render_run_update(app_id, claim, "done", trace))
        traces.append(trace)
        verdict = new_verdict()
        refresh_card(verdict)
        pdf_out = annotated(verdict)

    # 7. Write-back: latest PDF into the thread, then the note and the PDF to the ATS.
    up = slack.upload_file(card, pdf_out, title=pdf_out.name)
    meta["slack"]["pdf_permalink"] = up["permalink"]
    note = render.render_note(app, claims, verdict, up["permalink"])
    result = adapter.write_back(app, note, pdf_out)
    meta["writebacks"].append({"verdict_n": meta["latest"]["verdict_n"],
                               "note_id": result["note_id"],
                               "attachment_id": result["attachment_id"],
                               "at": store.now_iso()})
