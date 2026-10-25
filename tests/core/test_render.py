"""Structural and golden tests for the Slack renderers (design doc §8.3, §8.5, J6)."""
import copy
import json
import os
from pathlib import Path

import pytest

from verifier.core import render

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
GOLDEN = FIXTURES / "render" / "henry_card.json"


def load(rel):
    return json.loads((FIXTURES / rel).read_text(encoding="utf-8"))


CLAIMS = load("claims/henrychen4736.json")
VERDICT = load("verdicts/henrychen4736.json")
TRACE = load("run_traces/c3.json")

APP = {
    "application_id": "12345", "candidate_id": "2000001", "candidate_name": "Henry Chen",
    "emails": ["henry@example.com"], "website_addresses": [], "social_media_addresses": [],
    "job_title": "Software Engineer Intern", "applied_at": "2026-10-25T14:01:50Z",
    "profile_url": "http://localhost:8100/candidates/2000001?application_id=12345",
    "resume": None, "event_id": "evt1",
}
META = {
    "application_id": "12345", "candidate_id": "2000001", "candidate_name": "Henry Chen",
    "job_title": "Software Engineer Intern", "event_id": "evt1", "applied_at": APP["applied_at"],
    "latest": {}, "slack": {}, "runs": {}, "stage_errors": [], "writebacks": [],
}
PDF_URL = "https://example.slack.com/files/U1/F1/henry_resume_verified_1.pdf"
OPTS = {"profile_url": APP["profile_url"], "annotated_pdf_url": PDF_URL, "action_buttons": True,
        "menu": ["more_context", "suggest_questions", "edit_claim"]}

ALLOWED_ACTIONS = {"openclaw:rv:recheck", "openclaw:rv:open_ats", "openclaw:rv:open_pdf",
                   "openclaw:rv:claim_menu", "openclaw:rv:transcript"}
BLOCK_TYPES = {"header", "section", "context", "actions", "divider"}
MENU_KINDS = {"more_context", "suggest_questions", "edit_claim"}


# ── structural checker ───────────────────────────────────────────────────────

def _no_none(node, path="$"):
    if node is None:
        raise AssertionError(f"None value at {path}")
    if isinstance(node, dict):
        for k, v in node.items():
            _no_none(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _no_none(v, f"{path}[{i}]")


def _check_text(t, limit=3000):
    assert t["type"] in ("mrkdwn", "plain_text")
    assert isinstance(t["text"], str) and t["text"]
    assert len(t["text"]) <= limit


def _check_element(el, app_id):
    if el["type"] == "button":
        assert el["action_id"] in ALLOWED_ACTIONS
        _check_text(el["text"], 75)
        assert el["text"]["type"] == "plain_text"
        if el["action_id"] == "openclaw:rv:recheck":
            assert el["value"] == app_id and "url" not in el
        elif el["action_id"] in ("openclaw:rv:open_ats", "openclaw:rv:open_pdf"):
            assert el["url"].startswith("http") and "value" not in el
        elif el["action_id"] == "openclaw:rv:claim_menu":
            kind, a, c = el["value"].split("|")
            assert kind in MENU_KINDS and a == app_id and c
        elif el["action_id"] == "openclaw:rv:transcript":
            a, c = el["value"].split("|")
            assert a == app_id and c
    elif el["type"] == "overflow":
        assert el["action_id"] == "openclaw:rv:claim_menu"
        assert 2 <= len(el["options"]) <= 5
        for o in el["options"]:
            _check_text(o["text"], 75)
            kind, a, c = o["value"].split("|")
            assert kind in MENU_KINDS and a == app_id and c
            assert len(o["value"]) <= 75
    else:
        raise AssertionError(f"unknown element {el['type']}")


def check_message(msg, app_id="12345"):
    assert isinstance(msg["text"], str) and msg["text"].strip()
    blocks = msg["blocks"]
    assert 1 <= len(blocks) <= 50
    _no_none(msg)
    ids = [b["block_id"] for b in blocks]
    assert len(ids) == len(set(ids)), "block_ids must be unique"
    for b in blocks:
        assert b["type"] in BLOCK_TYPES
        assert len(b["block_id"]) <= 255
        if b["type"] == "header":
            assert b["text"]["type"] == "plain_text"
            _check_text(b["text"], 150)
        elif b["type"] == "section":
            _check_text(b["text"])
            if "accessory" in b:
                _check_element(b["accessory"], app_id)
        elif b["type"] == "context":
            assert 1 <= len(b["elements"]) <= 10
            for e in b["elements"]:
                _check_text(e)
        elif b["type"] == "actions":
            assert 1 <= len(b["elements"]) <= 25
            for e in b["elements"]:
                _check_element(e, app_id)
    return blocks


def texts(msg):
    out = []
    for b in msg["blocks"]:
        if "text" in b:
            out.append(b["text"]["text"])
        for e in b.get("elements", []):
            if "text" in e and isinstance(e["text"], (str, dict)):
                out.append(e["text"]["text"] if isinstance(e["text"], dict) else e["text"])
        if "accessory" in b:
            out.append("<acc>")
    return out


def by_id(msg):
    return {b["block_id"]: b for b in msg["blocks"]}


def card(**kw):
    args = dict(app=APP, claims=CLAIMS, verdict=VERDICT, meta=META, opts=OPTS)
    args.update(kw)
    return render.render_card(args["app"], args["claims"], args["verdict"], args["meta"], args["opts"])


# ── Henry's card: golden and layout ──────────────────────────────────────────

def test_golden_card():
    msg = card()
    check_message(msg)
    if os.environ.get("UPDATE_GOLDEN") == "1":
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(msg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    assert json.loads(GOLDEN.read_text(encoding="utf-8")) == msg


def test_henry_card_matches_mockup():
    msg = card()
    b = by_id(msg)
    assert msg["text"] == "New application: Henry Chen: 🟢 6 🟡 1 🔴 0 ⚪ 2"
    assert b["header"]["text"]["text"] == "New application: Henry Chen · Software Engineer Intern"
    acct = [e["text"] for e in b["account"]["elements"]]
    assert acct == ["GitHub @henrychen4736 · 5 public repos · account since Sep 2021",
                    "Most public activity is from the last 30 days."]
    assert b["tally"]["text"]["text"] == "🟢 6   🟡 1   🔴 0   ⚪ 2"
    order = [x["block_id"] for x in msg["blocks"]]
    assert order == ["header", "account", "tally", "group_projects", "claim_c1", "notes_claim_c1",
                     "claim_c2", "notes_claim_c2", "claim_c3", "group_skills", "claim_c4", "claim_c5", "claim_c6",
                     "claim_c7", "group_unchecked", "unchecked", "actions", "footer"]
    assert b["group_projects"]["text"]["text"] == "*PROJECTS*"
    assert b["claim_c1"]["text"]["text"].startswith("🟢 *Polytrack-ML*: 35 of 35 commits by the candidate")
    assert b["notes_claim_c1"]["elements"][0]["text"] == "↳ Commits not linked to the GitHub account"
    assert b["claim_c3"]["text"]["text"] == (
        "🟡 *cubesolver*: Described as a complete project, but the repo is 3 KB with 2 commits on 1 day")
    assert b["claim_c4"]["text"]["text"] == "🟢 *Python*: In 3 repos, up to 45% of the code"
    assert b["group_unchecked"]["text"]["text"] == "*NOT CHECKED (2)*"
    assert b["unchecked"]["text"]["text"] == "⚪ Employment, education. Not verifiable from public code."
    assert "accessory" not in b["unchecked"]
    acts = b["actions"]["elements"]
    assert [(a["text"]["text"], a["action_id"]) for a in acts] == [
        ("Re-check now", "openclaw:rv:recheck"), ("Open in Greenhouse", "openclaw:rv:open_ats"),
        ("Open annotated resume", "openclaw:rv:open_pdf")]
    assert acts[0]["value"] == "12345"
    assert acts[1]["url"] == APP["profile_url"] and acts[2]["url"] == PDF_URL
    assert b["footer"]["elements"][0]["text"] == (
        "Evidence summary, not a hiring decision · rubric-v1 · snap_20261025T185533Z")


def test_claim_menu_overflow():
    acc = by_id(card())["claim_c3"]["accessory"]
    assert acc["type"] == "overflow" and acc["action_id"] == "openclaw:rv:claim_menu"
    assert [(o["text"]["text"], o["value"]) for o in acc["options"]] == [
        ("More context", "more_context|12345|c3"), ("Suggest questions", "suggest_questions|12345|c3"),
        ("Edit claim", "edit_claim|12345|c3")]


# ── CardOptions variants ─────────────────────────────────────────────────────

def test_options_off():
    opts = {**OPTS, "action_buttons": False, "menu": [], "annotated_pdf_url": None}
    msg = card(opts=opts)
    check_message(msg)
    b = by_id(msg)
    assert not any("accessory" in x for x in msg["blocks"])
    assert [a["action_id"] for a in b["actions"]["elements"]] == ["openclaw:rv:open_ats"]
    assert b["hint"]["elements"][0]["text"] == "Mention me to re-check or ask about a claim."
    ids = [x["block_id"] for x in msg["blocks"]]
    assert ids.index("hint") == ids.index("actions") - 1


@pytest.mark.parametrize("menu", [
    ["more_context", "suggest_questions", "edit_claim"],   # plugin / agent
    ["suggest_questions", "edit_claim"],                   # CUT=generate? (variants)
    ["more_context", "suggest_questions"],                 # CUT=edit
    ["edit_claim"],                                        # one option
    [],
])
def test_menu_variants(menu):
    msg = card(opts={**OPTS, "menu": menu})
    check_message(msg)
    acc = by_id(msg)["claim_c1"].get("accessory")
    if not menu:
        assert acc is None
    elif len(menu) == 1:
        assert acc["type"] == "button" and acc["action_id"] == "openclaw:rv:claim_menu"
        assert acc["value"] == "edit_claim|12345|c1" and acc["text"]["text"] == "Edit claim"
    else:
        assert acc["type"] == "overflow" and len(acc["options"]) == len(menu)


def test_no_pdf_button_while_url_unset():
    msg = card(opts={**OPTS, "annotated_pdf_url": None})
    ids = [a["action_id"] for a in by_id(msg)["actions"]["elements"]]
    assert ids == ["openclaw:rv:recheck", "openclaw:rv:open_ats"]


# ── context lines ────────────────────────────────────────────────────────────

def test_no_account_and_extra_context_lines():
    v = copy.deepcopy(VERDICT)
    v["account"] = None
    v["context"] = ["Most public activity is from the last 30 days.", "Account created Sep 2021."]
    lines = [e["text"] for e in by_id(card(verdict=v))["account"]["elements"]]
    assert lines == ["No GitHub account found on the resume or application.",
                     "Most public activity is from the last 30 days."]


def test_stage_errors_and_run_progress():
    meta = copy.deepcopy(META)
    meta["stage_errors"] = [
        {"stage": "fetch", "error": "x", "at": "t"},
        {"stage": "annotate", "error": "x", "at": "t"},
        {"stage": "run_check", "claim_id": "c3", "error": "x", "at": "t"},
        {"stage": "writeback", "error": "x", "at": "t"},
        {"stage": "slack", "error": "x", "at": "t"},
    ]
    meta["runs"] = {"c1": "done", "c3": "running"}
    msg = card(meta=meta)
    check_message(msg)
    lines = [e["text"] for e in by_id(msg)["account"]["elements"]]
    assert lines[2:] == [
        "GitHub evidence unavailable; affected checks show ⚪.",
        "Annotated resume unavailable.",
        "Functional run check unavailable for cubesolver.",
        "Greenhouse note not written.",
        "Run checks: 1 of 2 done",
    ]


def test_run_progress_hidden_when_all_finished():
    meta = {**META, "runs": {"c3": "done"}}
    lines = [e["text"] for e in by_id(card(meta=meta))["account"]["elements"]]
    assert not any(line.startswith("Run checks") for line in lines)


def test_context_overflow_goes_to_second_block():
    meta = {**META, "stage_errors": [{"stage": "run_check", "claim_id": f"x{i}"} for i in range(14)]}
    msg = card(meta=meta)
    b = by_id(msg)
    check_message(msg)
    assert len(b["account"]["elements"]) == 10 and "account_2" in b


# ── flags, needs-review, frameworks, contributions ───────────────────────────

def _extended():
    claims = copy.deepcopy(CLAIMS)
    verdict = copy.deepcopy(VERDICT)

    def add(claim, label, reason, flags=(), notes=()):
        claims["claims"].append(claim)
        verdict["claims"].append({"claim_id": claim["claim_id"], "type": claim["type"], "label": label,
                                  "flags": list(flags), "top_reason": reason, "checks": [],
                                  "notes": list(notes), "evidence_links": []})

    add({"claim_id": "c10", "type": "skill", "section": "Skills", "source_text": "Tools: React, Docker",
         "needs_review": False, "assertions": {"name": "React", "kind": "framework_tool"}},
        "no_evidence", "Not checked")
    add({"claim_id": "c11", "type": "skill", "section": "Skills", "source_text": "Tools: React, Docker",
         "needs_review": False, "assertions": {"name": "Docker", "kind": "framework_tool"}},
        "no_evidence", "Not checked")
    add({"claim_id": "c12", "type": "oss_contribution", "section": "OSS", "source_text": "PRs to foo/bar",
         "needs_review": False,
         "assertions": {"repo": "foo/bar", "role": "contributor", "min_prs": None, "description": None}},
        "supported", "3 merged PRs and 0 commits by @henrychen4736 in foo/bar")
    add({"claim_id": "c13", "type": "unchecked", "section": "Misc",
         "source_text": "Led R&D for several teams and stakeholders on a very ambitious initiative <3 yes",
         "needs_review": True, "assertions": {"category": "other"}},
        "no_evidence", "Not verifiable")
    add({"claim_id": "c14", "type": "project", "section": "Projects", "source_text": "Flaky <app> & more",
         "needs_review": False,
         "assertions": {"repo_name": "flaky", "repo_url": None, "role": None, "language": None,
                        "min_stars": None, "start_year": None, "scale": None, "public_signal": "none",
                        "behavior": None}},
        "partial", "x" * 400, flags=["run_failed"])
    return claims, verdict


def test_groups_flags_frameworks_needs_review():
    claims, verdict = _extended()
    msg = card(claims=claims, verdict=verdict)
    check_message(msg)
    b = by_id(msg)
    order = [x["block_id"] for x in msg["blocks"]]
    groups = [i for i in order if i.startswith("group_")]
    assert groups == ["group_projects", "group_contributions", "group_skills",
                      "group_needs_review", "group_unchecked"]
    assert b["claim_c12"]["text"]["text"].startswith("🟢 *foo/bar*:")
    assert "accessory" in b["claim_c12"]
    fw = b["frameworks"]
    assert fw["text"]["text"] == "⚪ React, Docker: frameworks and tools aren't checked in v1."
    assert "accessory" not in fw
    nr = b["claim_c13"]
    assert nr["text"]["text"].endswith(": couldn't classify this claim; check the resume.")
    assert "accessory" not in nr and "&amp;" in nr["text"]["text"]
    flagged = b["claim_c14"]["text"]["text"]
    assert flagged.startswith("⚠️ 🟡 *flaky*: ") and flagged.endswith("…")
    assert len(flagged) < 340
    assert b["group_unchecked"]["text"]["text"] == "*NOT CHECKED (2)*"


def test_names_fall_back_to_matched_repo_then_source_text():
    claims = copy.deepcopy(CLAIMS)
    claims["claims"][2]["assertions"]["repo_name"] = None
    row = by_id(card(claims=claims))["claim_c3"]["text"]["text"]
    assert row.startswith("🟡 *cubesolver*:")       # from the `repo` check facts
    verdict = copy.deepcopy(VERDICT)
    verdict["claims"][2]["checks"] = []
    row = by_id(card(claims=claims, verdict=verdict))["claim_c3"]["text"]["text"]
    assert row.startswith("🟡 *Built cubesolver, a full Rubik's cube solver in C++"[:40])


# ── block budget ─────────────────────────────────────────────────────────────

def _big(n_projects, n_skills, notes=True):
    claims = copy.deepcopy(CLAIMS)
    verdict = copy.deepcopy(VERDICT)
    claims["claims"], verdict["claims"] = [], []
    for i in range(n_projects):
        cid = f"p{i}"
        a = copy.deepcopy(CLAIMS["claims"][0]["assertions"])
        a["repo_name"] = f"proj{i}"
        claims["claims"].append({"claim_id": cid, "type": "project", "section": "Projects",
                                 "source_text": f"proj{i}", "needs_review": False, "assertions": a})
        verdict["claims"].append({"claim_id": cid, "type": "project", "label": "supported", "flags": [],
                                  "top_reason": "35 of 35 commits by the candidate",
                                  "checks": [], "evidence_links": [],
                                  "notes": ["Commits not linked to the GitHub account"] if notes else []})
    for i in range(n_skills):
        cid = f"s{i}"
        claims["claims"].append({"claim_id": cid, "type": "skill", "section": "Skills",
                                 "source_text": f"Languages: s{i}", "needs_review": False,
                                 "assertions": {"name": f"Skill{i}", "kind": "language"}})
        verdict["claims"].append({"claim_id": cid, "type": "skill", "label": "supported", "flags": [],
                                  "top_reason": "Main language in 1 repo", "checks": [],
                                  "notes": [], "evidence_links": []})
    return claims, verdict


def test_budget_folds_notes_first():
    claims, verdict = _big(20, 4)         # 20 + 20 notes + 4 skills + chrome > 50 unfolded
    msg = card(claims=claims, verdict=verdict)
    check_message(msg)
    ids = [b["block_id"] for b in msg["blocks"]]
    assert not any(i.startswith("notes_") for i in ids)
    assert "\n↳ Commits not linked to the GitHub account" in by_id(msg)["claim_p0"]["text"]["text"]
    assert "accessory" in by_id(msg)["claim_s0"]            # skills still have menus


def test_budget_collapses_skills_next():
    claims, verdict = _big(40, 10)
    msg = card(claims=claims, verdict=verdict)
    check_message(msg)
    b = by_id(msg)
    assert "skills_collapsed" in b and "claim_s0" not in b
    assert b["skills_collapsed"]["text"]["text"].startswith("🟢 Skill0 · 🟢 Skill1")
    assert "accessory" not in b["skills_collapsed"]
    assert "claim_p39" in b


def test_budget_truncates_with_more_line():
    claims, verdict = _big(45, 12)
    msg = card(claims=claims, verdict=verdict)
    blocks = check_message(msg)
    assert len(blocks) <= 50
    more = by_id(msg)["more"]["elements"][0]["text"]
    assert more.startswith("+ ") and more.endswith("more claims in the annotated resume")
    assert "group_projects" in by_id(msg)


def test_synthetic_40_claim_verdict_within_50_blocks():
    claims, verdict = _big(20, 15)
    claims["claims"] += [
        {"claim_id": f"u{i}", "type": "unchecked", "section": "x", "source_text": "x",
         "needs_review": False, "assertions": {"category": "employment"}} for i in range(5)]
    verdict["claims"] += [
        {"claim_id": f"u{i}", "type": "unchecked", "label": "no_evidence", "flags": [],
         "top_reason": "Not verifiable from public code", "checks": [], "notes": [],
         "evidence_links": []} for i in range(5)]
    assert len(verdict["claims"]) == 40
    msg = card(claims=claims, verdict=verdict)
    check_message(msg)
    assert len(msg["blocks"]) <= 50


def test_long_reasons_and_section_limits():
    claims, verdict = _big(1, 0)
    verdict["claims"][0]["top_reason"] = "y" * 5000
    msg = card(claims=claims, verdict=verdict)
    check_message(msg)
    assert by_id(msg)["claim_p0"]["text"]["text"].endswith("…")


def test_long_header_cut():
    app = {**APP, "candidate_name": "N" * 400}
    msg = card(app=app)
    assert len(msg["blocks"][0]["text"]["text"]) <= 150
    check_message(msg)


def test_header_without_job_title():
    app = {**APP, "job_title": None}
    assert card(app=app)["blocks"][0]["text"]["text"] == "New application: Henry Chen"


# ── placeholder and could-not-read ───────────────────────────────────────────

def test_placeholder():
    msg = render.render_placeholder(APP)
    check_message(msg)
    assert msg["text"] == "New application: Henry Chen: verifying…"
    assert msg["blocks"][0]["text"]["text"] == "New application: Henry Chen · Software Engineer Intern"
    assert msg["blocks"][1]["elements"][0]["text"] == "Verifying public evidence… this takes about a minute."


def test_could_not_read_card():
    meta = {**META, "stage_errors": [{"stage": "extract", "error": "x", "at": "t"}]}
    msg = card(claims=None, verdict=None, meta=meta)
    check_message(msg)
    b = by_id(msg)
    assert [e["text"] for e in b["account"]["elements"]] == [
        "Couldn't read claims from this resume. Re-check retries reading it."]
    assert [a["action_id"] for a in b["actions"]["elements"]] == [
        "openclaw:rv:recheck", "openclaw:rv:open_ats"]
    assert "snap" not in b["footer"]["elements"][0]["text"]
    assert "claim_c1" not in b


def test_could_not_read_card_without_buttons():
    msg = card(claims=None, verdict=None, opts={**OPTS, "action_buttons": False})
    check_message(msg)
    assert [a["action_id"] for a in by_id(msg)["actions"]["elements"]] == ["openclaw:rv:open_ats"]


# ── run lines ────────────────────────────────────────────────────────────────

C3 = CLAIMS["claims"][2]


def _trace(**kw):
    t = copy.deepcopy(TRACE)
    for k, v in kw.items():
        t[k] = v
    return t


def test_run_started_skipped_error():
    for status, text in [("started", "▶ cubesolver: running…"), ("skipped", "▶ cubesolver: skipped"),
                         ("error", "▶ cubesolver: run check unavailable")]:
        msg = render.render_run_update("12345", C3, status, None)
        check_message(msg)
        assert msg["text"] == text and "accessory" not in msg["blocks"][0]


def test_run_done_full_line_with_transcript_button():
    msg = render.render_run_update("12345", C3, "done", TRACE)
    check_message(msg)
    assert msg["text"] == "▶ cubesolver: built (0.8s) → ran 5 commands → works as claimed"
    acc = msg["blocks"][0]["accessory"]
    assert acc["action_id"] == "openclaw:rv:transcript" and acc["value"] == "12345|c3"
    assert "⚠" not in msg["text"]


def test_run_done_without_buttons_agent_or_judge():
    msg = render.render_run_update("12345", C3, "done", _trace(agent=None, judge=None), buttons=False)
    check_message(msg)
    assert msg["text"] == "▶ cubesolver: built (0.8s)"
    assert "accessory" not in msg["blocks"][0]


@pytest.mark.parametrize("baseline, expected", [
    ({"step": "tests", "exit_code": 0, "duration_s": 12.34}, "tests passed (12.3s)"),
    ({"step": "tests", "exit_code": 1}, "tests failed"),
    ({"step": "build", "exit_code": 0, "duration_s": 3.1}, "built (3.1s)"),
    ({"step": "build", "exit_code": 2}, "build failed"),
    ({"step": "syntax", "exit_code": 0}, "syntax OK"),
    ({"step": "syntax", "exit_code": 1}, "syntax errors"),
    ({"step": "skip", "exit_code": None}, "nothing to build"),
    ({"step": "tests", "exit_code": 1, "failure_kind": "deps_missing"}, "missing dependencies"),
    ({"step": "tests", "exit_code": 1, "failure_kind": "needs_network"}, "needs network"),
    ({"step": "build", "exit_code": None, "timed_out": True}, "timed out"),
])
def test_run_baseline_texts(baseline, expected):
    b = {**TRACE["baseline"], "failure_kind": None, "timed_out": False, **baseline}
    msg = render.render_run_update("12345", C3, "done", _trace(baseline=b, agent=None, judge=None))
    assert msg["text"] == f"▶ cubesolver: {expected}"


@pytest.mark.parametrize("outcome, expected", [
    ("works_as_claimed", "works as claimed"), ("runs_but_mismatch", "output didn't match the claim"),
    ("failed_to_run", "failed to run"), ("could_not_attempt", "couldn't run here"),
])
def test_run_judge_texts(outcome, expected):
    judge = {**TRACE["judge"], "outcome": outcome}
    msg = render.render_run_update("12345", C3, "done", _trace(judge=judge))
    assert msg["text"].endswith(f"→ {expected}")


def test_run_name_falls_back_to_trace_repo_then_source_text():
    claim = copy.deepcopy(C3)
    claim["assertions"]["repo_name"] = None
    assert render.render_run_update("1", claim, "done", TRACE)["text"].startswith("▶ cubesolver:")
    assert render.render_run_update("1", claim, "started", None)["text"].startswith(
        "▶ Built cubesolver, a full Rubik's cube solver in C++"[:42])


# ── label change, generated, modal ───────────────────────────────────────────

def test_label_change():
    edits = [{"claims_version": 2, "claim_id": "c3", "field": "scale", "from": "substantial",
              "to": "small", "by": "Jake", "at": "t"}]
    msg = render.render_label_change(C3, edits, "partial", "supported", "Jake")
    check_message(msg)
    assert msg["text"] == "Jake edited cubesolver: scale substantial → small. 🟡 → 🟢."
    same = render.render_label_change(C3, edits, "partial", "partial", "Jake")
    assert same["text"].endswith("Label unchanged (🟡).")
    nulls = [{**edits[0], "from": None, "to": 5, "field": "min_stars"}]
    assert "min_stars not stated → 5" in render.render_label_change(C3, nulls, "partial", "partial", "J")["text"]


def test_generated():
    gen = {"claim_id": "c3", "kind": "more_context", "verdict_id": "verdict_1", "model": "Qwen",
           "prompt_version": "context-v1", "created_at": "t", "text": "- one\n- **two**"}
    msg = render.render_generated(C3, gen)
    check_message(msg)
    assert msg["blocks"][0]["text"]["text"] == "*More context: cubesolver*\n• one\n• *two*"
    assert msg["blocks"][-1]["elements"][0]["text"] == (
        "Generated locally by Qwen · context-v1 · based on verdict_1")
    gen2 = {**gen, "kind": "suggest_questions", "text": "q" * 7000}
    msg2 = render.render_generated(C3, gen2)
    check_message(msg2)
    assert msg2["blocks"][0]["text"]["text"].startswith("*Suggested questions: cubesolver*")
    assert len([b for b in msg2["blocks"] if b["type"] == "section"]) == 3


def test_edit_modal_valid():
    view = render.render_edit_modal("12345", 3, C3)
    assert view["type"] == "modal" and view["callback_id"] == "openclaw:rv:edit_claim:12345:c3:3"
    assert json.loads(view["private_metadata"]) == {"application_id": "12345", "claim_id": "c3",
                                                    "claims_version": 3}
    for k in ("title", "submit", "close"):
        assert view[k]["type"] == "plain_text" and len(view[k]["text"]) <= 24
    assert view["blocks"]
    _no_none(view)
    json.dumps(view)


# ── Greenhouse note ──────────────────────────────────────────────────────────

def test_note_henry():
    note = render.render_note(APP, CLAIMS, VERDICT, "https://slack.example/p123")
    assert note.splitlines() == [
        "Resume Verifier · rubric-v1 · Oct 25, 2026",
        "6 supported · 1 partially supported · 0 contradicted · 2 no public evidence",
        "Partial: cubesolver: Described as a complete project, but the repo is 3 KB with 2 commits on 1 day",
        "Full report: https://slack.example/p123",
        "Evidence summary only; not a hiring recommendation.",
    ]
    assert note.isascii() or all(ord(c) < 0x2600 for c in note)     # no emoji


def test_note_without_permalink():
    note = render.render_note(APP, CLAIMS, VERDICT, None)
    assert "Full report" not in note and note.splitlines()[-1].startswith("Evidence summary only")


def test_note_eight_line_limit_contradicted_first():
    claims, verdict = _big(12, 0)
    for i, vc in enumerate(verdict["claims"]):
        vc["label"] = "contradicted" if i < 2 else "partial"
        vc["top_reason"] = f"reason {i}"
    verdict["tally"] = {"supported": 0, "partial": 10, "contradicted": 2, "no_evidence": 0}
    lines = render.render_note(APP, claims, verdict, None).splitlines()
    detail = lines[2:-1]
    assert len(detail) == 9
    assert detail[0] == "Contradicted: proj0: reason 0" and detail[1] == "Contradicted: proj1: reason 1"
    assert detail[2] == "Partial: proj2: reason 2"
    assert detail[-1] == "+ 4 more in the full report"


def test_note_flagged_claim_listed_as_partial():
    claims, verdict = _extended()
    note = render.render_note(APP, claims, verdict, None)
    assert "Partial: flaky: " in note
