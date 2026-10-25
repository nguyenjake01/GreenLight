"""J12: Edit claim (design doc 5.3, 8.3, 8.5). Stub Slack/ATS/fetch, real render and rubric; offline."""
import copy
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from verifier.contracts import stubs
from verifier.core import pipeline, render, service, store

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
TOKEN = {"X-Verifier-Token": "tok"}
QUEUED = "Queued: this edit applies after the current checks finish."
_counter = iter(range(20000, 10**6))


def load(rel):
    return json.loads((FIXTURES / rel).read_text(encoding="utf-8"))


HENRY = load("claims/henrychen4736.json")
AIDAN = load("claims/aidanhong8.json")


def claim_of(doc, claim_id):
    return next(c for c in doc["claims"] if c["claim_id"] == claim_id)


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("INTERNAL_TOKEN", "tok")
    for k in ("RUN_CHECK", "SLACK_ACTIONS", "CUT", "WRITEBACK_CAP_S"):
        monkeypatch.delenv(k, raising=False)
    # Real render (the label-change line) and real rubric (scale changes the label); the rest are stubs.
    monkeypatch.setenv("VERIFIER_REAL", "rubric,render")
    pipeline._APPS.clear()
    return tmp_path / "runs"


def make_app(name="Henry Chen", resume="henry.pdf"):
    n = next(_counter)
    return {
        "application_id": str(n), "candidate_id": str(n + 7), "candidate_name": name,
        "emails": ["h@example.com"], "website_addresses": [], "social_media_addresses": [],
        "job_title": "Software Engineer Intern", "applied_at": "2026-10-25T14:01:50Z",
        "profile_url": f"http://ats.local/candidates/{n}",
        "resume": {"filename": resume, "url": "http://x/" + resume, "type": "resume"},
        "event_id": f"evt{n}",
    }


@pytest.fixture
def app_id(env):
    app = make_app()
    pipeline.run(app, wait=True)
    return app["application_id"]


def slack_log(env):
    p = env / "_slack_stub.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def texts(env, call=None):
    return [l["text"] or "" for l in slack_log(env) if call is None or l["call"] == call]


# ── the modal ─────────────────────────────────────────────────────────────────

def inputs_by_id(modal):
    return {b["block_id"]: b for b in modal["blocks"] if b["type"] == "input"}


def option_values(block):
    return [o["value"] for o in block["element"]["options"]]


def test_modal_envelope():
    modal = render.render_edit_modal("12345", 3, claim_of(HENRY, "c3"))
    assert modal["type"] == "modal" and modal["callback_id"] == "openclaw:rv:edit_claim:12345:c3:3"
    assert modal["title"]["text"] == "Edit claim"
    assert modal["submit"]["text"] == "Save" and modal["close"]["text"] == "Cancel"
    assert json.loads(modal["private_metadata"]) == {
        "application_id": "12345", "claim_id": "c3", "claims_version": 3}
    first = modal["blocks"][0]
    assert first["type"] == "context"
    assert first["elements"][0]["text"] == "Built cubesolver, a full Rubik's cube solver in C++ (2024)"
    json.dumps(modal)  # serializable


def test_modal_quotes_at_most_300_characters_of_source_text():
    claim = copy.deepcopy(claim_of(HENRY, "c3"))
    claim["source_text"] = "x" * 900
    quote = render.render_edit_modal("1", 1, claim)["blocks"][0]["elements"][0]["text"]
    assert 0 < len(quote) <= 300


def test_project_modal_fields_and_initial_values():
    modal = render.render_edit_modal("12345", 1, claim_of(HENRY, "c3"))
    blocks = inputs_by_id(modal)
    assert list(blocks) == ["repo_url", "repo_name", "language", "role", "min_stars",
                            "start_year", "scale", "public_signal", "behavior"]
    for b in blocks.values():
        assert b["element"]["action_id"] == "v"
    assert blocks["repo_name"]["element"]["initial_value"] == "cubesolver"
    assert blocks["language"]["element"]["initial_value"] == "C++"
    assert "initial_value" not in blocks["repo_url"]["element"]          # null: empty
    assert blocks["behavior"]["element"]["multiline"] is True
    assert blocks["behavior"]["element"]["initial_value"] == "Solves a Rubik's cube"
    # Selects
    role, scale, signal = blocks["role"], blocks["scale"], blocks["public_signal"]
    assert option_values(role)[:3] == ["creator", "co-creator", "maintainer"]
    assert role["element"]["initial_option"]["value"] == "creator"
    assert option_values(scale)[:2] == ["small", "substantial"]
    assert scale["element"]["initial_option"]["value"] == "substantial"
    assert option_values(signal) == ["link", "open_source_wording", "none"]
    assert signal["element"]["initial_option"]["value"] == "none"
    # "Not stated" is an option on the optional selects only
    assert option_values(role)[-1] == option_values(scale)[-1] == render.NOT_STATED
    assert render.NOT_STATED not in option_values(signal)
    # Numbers are whole-number inputs
    for f in ("min_stars", "start_year"):
        assert blocks[f]["element"]["type"] == "number_input"
        assert blocks[f]["element"]["is_decimal_allowed"] is False
    assert blocks["start_year"]["element"]["initial_value"] == "2024"
    assert "initial_value" not in blocks["min_stars"]["element"]
    # Required: only public_signal
    assert {f for f, b in blocks.items() if not b["optional"]} == {"public_signal"}


def test_project_modal_null_select_starts_on_not_stated():
    claim = claim_of(AIDAN, "c1")
    claim = {**claim, "assertions": {**claim["assertions"], "scale": None}}
    scale = inputs_by_id(render.render_edit_modal("1", 1, claim))["scale"]
    assert scale["element"]["initial_option"]["value"] == render.NOT_STATED


def test_skill_modal():
    blocks = inputs_by_id(render.render_edit_modal("1", 1, claim_of(HENRY, "c4")))
    assert list(blocks) == ["name", "kind"]
    assert blocks["name"]["element"]["initial_value"] == "Python" and not blocks["name"]["optional"]
    kind = blocks["kind"]
    assert not kind["optional"] and option_values(kind) == ["language", "framework_tool"]
    assert kind["element"]["initial_option"]["value"] == "language"


def test_contribution_modal():
    blocks = inputs_by_id(render.render_edit_modal("1", 1, claim_of(AIDAN, "c5")))
    assert list(blocks) == ["repo", "role", "min_prs", "description"]
    assert blocks["repo"]["element"]["initial_value"] == "boomty/CameraDetectionforTraffic"
    assert not blocks["repo"]["optional"] and not blocks["role"]["optional"]
    assert option_values(blocks["role"]) == ["contributor", "maintainer"]
    assert blocks["role"]["element"]["initial_option"]["value"] == "contributor"
    assert blocks["min_prs"]["element"]["type"] == "number_input"
    assert blocks["min_prs"]["element"]["initial_value"] == "2"
    assert blocks["description"]["element"]["multiline"] is True and blocks["description"]["optional"]


def test_modal_initial_options_are_among_options():
    for doc, ids in ((HENRY, ("c3", "c4")), (AIDAN, ("c1", "c3", "c4", "c5"))):
        for cid in ids:
            for b in inputs_by_id(render.render_edit_modal("1", 1, claim_of(doc, cid))).values():
                el = b["element"]
                if el["type"] == "static_select" and "initial_option" in el:
                    assert el["initial_option"]["value"] in [o["value"] for o in el["options"]]


# ── label-change message ──────────────────────────────────────────────────────

def edit(field, frm, to, claim_id="c3"):
    return {"claims_version": 2, "claim_id": claim_id, "field": field, "from": frm, "to": to,
            "by": "Jake", "at": "2026-10-25T15:00:00Z"}


def test_label_change_text():
    claim = claim_of(HENRY, "c3")
    msg = render.render_label_change(claim, [edit("scale", "substantial", "small")],
                                     "partial", "supported", "Jake")
    assert msg["text"] == "Jake edited cubesolver: scale substantial → small. 🟡 → 🟢."
    assert msg["blocks"][0]["type"] == "section"


def test_label_change_unchanged_null_and_several_fields():
    claim = claim_of(HENRY, "c3")
    msg = render.render_label_change(
        claim, [edit("scale", "substantial", None), edit("start_year", 2024, 2023)],
        "partial", "partial", "Jake")
    assert msg["text"] == ("Jake edited cubesolver: scale substantial → not stated; "
                           "start_year 2024 → 2023. Label unchanged (🟡).")


# ── validate_edit ─────────────────────────────────────────────────────────────

def test_validate_edit_valid_returns_empty(app_id):
    assert pipeline.validate_edit(app_id, "c3", {"scale": "small", "min_stars": "3",
                                                 "start_year": "2024"}) == {}
    assert pipeline.validate_edit(app_id, "c3", {"scale": render.NOT_STATED, "role": "not stated",
                                                 "min_stars": "", "behavior": "  "}) == {}


def test_validate_edit_bad_repo_url(app_id):
    for bad in ("not a url", "https://gitlab.com/a/b", "https://github.com/onlyowner"):
        assert list(pipeline.validate_edit(app_id, "c3", {"repo_url": bad})) == ["repo_url"], bad
    assert pipeline.validate_edit(app_id, "c3", {"repo_url": "https://github.com/a/b"}) == {}


def test_validate_edit_year_range(app_id):
    for bad in ("1800", "1989", str(datetime.now(timezone.utc).year + 2), "abc", "20.5", "-1"):
        assert list(pipeline.validate_edit(app_id, "c3", {"start_year": bad})) == ["start_year"], bad
    for good in ("1990", str(datetime.now(timezone.utc).year + 1)):
        assert pipeline.validate_edit(app_id, "c3", {"start_year": good}) == {}


def test_validate_edit_whole_numbers_only(app_id):
    assert list(pipeline.validate_edit(app_id, "c3", {"min_stars": "-3"})) == ["min_stars"]
    assert list(pipeline.validate_edit(app_id, "c3", {"min_stars": "1.5"})) == ["min_stars"]
    assert pipeline.validate_edit(app_id, "c3", {"min_stars": "0"}) == {}


def test_validate_edit_select_values(app_id):
    assert list(pipeline.validate_edit(app_id, "c3", {"scale": "huge"})) == ["scale"]
    assert list(pipeline.validate_edit(app_id, "c3", {"public_signal": render.NOT_STATED})) == [
        "public_signal"]                       # required: no "not stated"


def test_validate_edit_missing_required_field(app_id):
    assert pipeline.validate_edit(app_id, "c4", {"name": "  "}) == {"name": "Required."}
    assert pipeline.validate_edit(app_id, "c4", {"kind": None}) == {"kind": "Required."}
    assert list(pipeline.validate_edit(app_id, "c3", {"public_signal": ""})) == ["public_signal"]


def test_validate_edit_public_signal_set_automatically_by_repo_url(app_id):
    # Even an invalid public_signal is not an error once a repo URL is given.
    assert pipeline.validate_edit(app_id, "c3", {"repo_url": "https://github.com/a/b",
                                                 "public_signal": ""}) == {}


def test_validate_edit_unknown_field_claim_and_type(app_id):
    assert list(pipeline.validate_edit(app_id, "c3", {"nope": 1})) == ["nope"]
    assert list(pipeline.validate_edit(app_id, "zz", {})) == ["_claim"]
    assert list(pipeline.validate_edit(app_id, "c8", {"x": 1})) == ["_claim"]   # unchecked


def test_validate_edit_contribution_repo():
    app = make_app("Aidan Hong", "aidan.pdf")
    pipeline.run(app, wait=True)
    aid = app["application_id"]
    for bad in ("justarepo", "a/b/c", "a b/c", ""):
        assert list(pipeline.validate_edit(aid, "c5", {"repo": bad})) == ["repo"], bad
    assert pipeline.validate_edit(aid, "c5", {"repo": "owner/repo", "min_prs": "2"}) == {}
    assert pipeline.validate_edit(aid, "c5", {"repo": "https://github.com/owner/repo"}) == {}
    assert list(pipeline.validate_edit(aid, "c5", {"role": "creator"})) == ["role"]


# ── edit_claim ────────────────────────────────────────────────────────────────

def test_done_line_scale_substantial_to_small(env, app_id):
    old = store.read_meta(app_id)
    assert len(old["writebacks"]) == 1
    pipeline.edit_claim(app_id, "c3", {"scale": "small"}, "Jake")
    rd = store.run_dir(app_id)
    meta = store.read_meta(app_id)

    v2 = store.read_json(rd / "claims.v2.json")
    assert v2["claims_version"] == 2
    assert claim_of(v2, "c3")["assertions"]["scale"] == "small"
    edits = v2["edits"]
    assert len(edits) == 1
    e = edits[0]
    assert (e["claims_version"], e["claim_id"], e["field"], e["from"], e["to"], e["by"]) == (
        2, "c3", "scale", "substantial", "small", "Jake")
    assert e["at"]
    # claims.v1.json is never overwritten
    assert claim_of(store.read_json(rd / "claims.v1.json"), "c3")["assertions"]["scale"] == "substantial"
    # other claims untouched
    for c1, c2 in zip(store.read_json(rd / "claims.v1.json")["claims"], v2["claims"]):
        if c1["claim_id"] != "c3":
            assert c1 == c2

    # Same snapshot, new verdict on claims v2
    assert meta["latest"]["claims_version"] == 2
    assert meta["latest"]["snapshot_id"] == old["latest"]["snapshot_id"]
    verdict = store.read_json(rd / "verdicts" / f"verdict_{meta['latest']['verdict_n']}.json")
    assert verdict["inputs"]["claims_version"] == 2
    assert verdict["inputs"]["snapshot_id"] == old["latest"]["snapshot_id"]
    assert len(list((rd / "snapshots").iterdir())) == 1
    old_label = next(c["label"] for c in store.read_json(
        rd / "verdicts" / f"verdict_{old['latest']['verdict_n']}.json")["claims"] if c["claim_id"] == "c3")
    new_label = next(c["label"] for c in verdict["claims"] if c["claim_id"] == "c3")
    assert (old_label, new_label) == ("partial", "supported")

    # The thread line, and a second write-back
    assert texts(env, "post_thread")[-1] == "Jake edited cubesolver: scale substantial → small. 🟡 → 🟢."
    assert len(meta["writebacks"]) == 2
    assert meta["writebacks"][1]["verdict_n"] == meta["latest"]["verdict_n"]
    assert meta["writebacks"][1]["note_id"] != meta["writebacks"][0]["note_id"]
    assert meta["stage_errors"] == []
    assert not any(t == QUEUED for t in texts(env))


def test_edit_appends_to_earlier_edits(app_id):
    pipeline.edit_claim(app_id, "c3", {"scale": "small"}, "Jake")
    pipeline.edit_claim(app_id, "c3", {"start_year": "2023", "min_stars": "5"}, "Henry")
    rd = store.run_dir(app_id)
    v3 = store.read_json(rd / "claims.v3.json")
    assert v3["edits"][0] | {"at": None} == {
        "claims_version": 2, "claim_id": "c3", "field": "scale", "from": "substantial",
        "to": "small", "by": "Jake", "at": None}
    assert {(e["claims_version"], e["field"], e["from"], e["to"], e["by"]) for e in v3["edits"][1:]} == {
        (3, "start_year", 2024, 2023, "Henry"), (3, "min_stars", None, 5, "Henry")}
    assert len(v3["edits"]) == 3
    assert len(store.read_meta(app_id)["writebacks"]) == 3
    assert store.read_json(rd / "claims.v2.json")["edits"][0]["by"] == "Jake"  # v2 untouched


def test_edit_to_not_stated_stores_null(env, app_id):
    pipeline.edit_claim(app_id, "c3", {"scale": render.NOT_STATED}, "Jake")
    v2 = store.read_json(store.run_dir(app_id) / "claims.v2.json")
    assert claim_of(v2, "c3")["assertions"]["scale"] is None
    assert v2["edits"][0]["to"] is None
    assert "scale substantial → not stated" in texts(env, "post_thread")[-1]


def test_repo_name_change_makes_new_snapshot(env, app_id):
    old = store.read_meta(app_id)
    time.sleep(1.1)          # snapshot ids are timestamped to the second
    pipeline.edit_claim(app_id, "c3", {"repo_name": "cubesolver2"}, "Jake")
    rd = store.run_dir(app_id)
    meta = store.read_meta(app_id)
    assert meta["latest"]["snapshot_id"] != old["latest"]["snapshot_id"]
    assert len(list((rd / "snapshots").iterdir())) == 2
    v2 = store.read_json(rd / "claims.v2.json")
    assert [e["field"] for e in v2["edits"]] == ["repo_name"]
    verdict = store.read_json(rd / "verdicts" / f"verdict_{meta['latest']['verdict_n']}.json")
    assert verdict["inputs"]["snapshot_id"] == meta["latest"]["snapshot_id"]
    assert verdict["inputs"]["claims_version"] == 2
    assert texts(env, "post_thread")[-1].startswith("Jake edited cubesolver: repo_name cubesolver → cubesolver2.")
    assert len(meta["writebacks"]) == 2


def test_repo_url_edit_sets_link_signal_and_normalizes(app_id):
    time.sleep(1.1)
    pipeline.edit_claim(app_id, "c3", {"repo_url": "https://github.com/henrychen4736/cubesolver.git/",
                                       "public_signal": "none"}, "Jake")
    v2 = store.read_json(store.run_dir(app_id) / "claims.v2.json")
    a = claim_of(v2, "c3")["assertions"]
    assert a["repo_url"] == "https://github.com/henrychen4736/cubesolver"
    assert a["public_signal"] == "link"
    assert {e["field"] for e in v2["edits"]} == {"repo_url", "public_signal"}
    assert len(list((store.run_dir(app_id) / "snapshots").iterdir())) == 2


def test_contribution_repo_change_makes_new_snapshot():
    app = make_app("Aidan Hong", "aidan.pdf")
    pipeline.run(app, wait=True)
    aid = app["application_id"]
    old = store.read_meta(aid)["latest"]["snapshot_id"]
    time.sleep(1.1)
    pipeline.edit_claim(aid, "c5", {"repo": "someone/else"}, "Jake")
    meta = store.read_meta(aid)
    assert meta["latest"]["snapshot_id"] != old
    v2 = store.read_json(store.run_dir(aid) / "claims.v2.json")
    assert claim_of(v2, "c5")["assertions"]["repo"] == "someone/else"
    assert v2["edits"][0]["from"] == "boomty/CameraDetectionforTraffic"


def test_no_changes_replies_and_writes_nothing(env, app_id):
    before = store.read_meta(app_id)
    n_posts = len(slack_log(env))
    pipeline.edit_claim(app_id, "c3", {"scale": "substantial", "repo_name": "cubesolver",
                                       "min_stars": "", "start_year": "2024"}, "Jake")
    assert texts(env, "post_thread")[-1] == "No changes to cubesolver."
    after = store.read_meta(app_id)
    assert after["latest"] == before["latest"] and after["writebacks"] == before["writebacks"]
    assert not (store.run_dir(app_id) / "claims.v2.json").exists()
    assert len(slack_log(env)) == n_posts + 1


def test_invalid_values_reply_and_write_nothing(env, app_id):
    pipeline.edit_claim(app_id, "c3", {"start_year": "1800"}, "Jake")
    assert "start_year" in texts(env, "post_thread")[-1]
    assert not (store.run_dir(app_id) / "claims.v2.json").exists()


def test_unknown_claim_replies(env, app_id):
    pipeline.edit_claim(app_id, "zz", {"scale": "small"}, "Jake")
    assert "not found" in texts(env, "post_thread")[-1]
    assert not (store.run_dir(app_id) / "claims.v2.json").exists()


def test_edit_queues_behind_running_flow(env, monkeypatch):
    real = stubs.harness.run_check
    monkeypatch.setattr(stubs.harness, "run_check",
                        lambda s, c, r, *, mode: (time.sleep(0.4), real(s, c, r, mode=mode))[1])
    app = make_app()
    aid = app["application_id"]
    pipeline.run(app)
    deadline = time.time() + 10
    while time.time() < deadline and not (store.run_dir(aid) / "claims.v1.json").exists():
        time.sleep(0.02)
    time.sleep(0.2)
    assert pipeline.is_busy(aid)
    t = threading.Thread(target=pipeline.edit_claim, args=(aid, "c3", {"scale": "small"}, "Jake"))
    t.start()
    t.join(60)
    assert not t.is_alive()
    log = texts(env, "post_thread")
    assert QUEUED in log
    # the queued line comes first; the edit's own line comes after the run checks finish
    edit_line = "Jake edited cubesolver: scale substantial → small. 🟡 → 🟢."
    assert edit_line in log and log.index(QUEUED) < log.index(edit_line)
    assert (store.run_dir(aid) / "claims.v2.json").is_file()
    assert len(store.read_meta(aid)["writebacks"]) == 2


def test_new_claims_file_is_never_overwritten(app_id):
    rd = store.run_dir(app_id)
    v1 = (rd / "claims.v1.json").read_bytes()
    pipeline.edit_claim(app_id, "c3", {"scale": "small"}, "Jake")
    pipeline.edit_claim(app_id, "c3", {"scale": "substantial"}, "Jake")
    assert (rd / "claims.v1.json").read_bytes() == v1
    assert claim_of(store.read_json(rd / "claims.v2.json"), "c3")["assertions"]["scale"] == "small"
    assert claim_of(store.read_json(rd / "claims.v3.json"), "c3")["assertions"]["scale"] == "substantial"


# ── through the J9 route ──────────────────────────────────────────────────────

def test_edit_submit_fixture_through_internal_actions(env, app_id):
    client = TestClient(service.create_app())
    payload = json.loads((FIXTURES / "slack" / "edit_submit.json").read_text())
    payload["view"]["private_metadata"] = json.dumps(
        {"application_id": app_id, "claim_id": "c3", "claims_version": 1})
    r = client.post("/internal/actions", json=payload, headers=TOKEN)
    assert r.status_code == 200 and r.json() == {"ack_payload": None}
    rd = store.run_dir(app_id)
    deadline = time.time() + 30
    while time.time() < deadline and len(store.read_meta(app_id)["writebacks"]) < 2:
        time.sleep(0.1)
    meta = store.read_meta(app_id)
    assert len(meta["writebacks"]) == 2
    v2 = store.read_json(rd / "claims.v2.json")
    assert claim_of(v2, "c3")["assertions"]["scale"] == "small"
    assert [e["field"] for e in v2["edits"]] == ["scale"]       # the other fields match the claim
    assert v2["edits"][0]["by"] == "jordan"
    assert meta["latest"]["snapshot_id"] == store.read_json(
        rd / "verdicts" / f"verdict_{meta['latest']['verdict_n']}.json")["inputs"]["snapshot_id"]
    assert texts(env, "post_thread")[-1] == "jordan edited cubesolver: scale substantial → small. 🟡 → 🟢."


def test_edit_matching_a_different_repo_reruns_its_run_check(env, app_id):
    time.sleep(1.1)
    pipeline.edit_claim(app_id, "c3", {"repo_name": "CAC24"}, "Jake")
    meta = store.read_meta(app_id)
    snap = store.run_dir(app_id) / "snapshots" / meta["latest"]["snapshot_id"]
    assert (snap / "run_traces" / "c3.json").is_file()   # a fresh trace in the new snapshot
    # the run check started again for the edited claim (now named CAC24), after the first run's lines
    assert texts(env, "post_thread").count("▶ CAC24: running…") == 2
    assert meta["runs"]["c3"] == "done" and meta["stage_errors"] == []
