"""J9: the /internal/* API (design doc 8.4, 8.5). Stubs only, offline."""
import copy
import json
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from verifier.core import pipeline, service, store

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
TOKEN = {"X-Verifier-Token": "tok"}
_counter = iter(range(5000, 10**6))


@pytest.fixture
def runs(tmp_path, monkeypatch):
    path = tmp_path / "runs"
    monkeypatch.setenv("RUNS_DIR", str(path))
    monkeypatch.setenv("INTERNAL_TOKEN", "tok")
    for k in ("VERIFIER_REAL", "RUN_CHECK", "SLACK_ACTIONS", "CUT", "WRITEBACK_CAP_S"):
        monkeypatch.delenv(k, raising=False)
    pipeline._APPS.clear()
    return path


@pytest.fixture
def client(runs):
    return TestClient(service.create_app())


def make_app(name="Henry Chen", resume="henry.pdf", applied_at="2026-10-25T14:01:50Z"):
    n = next(_counter)
    return {
        "application_id": str(n), "candidate_id": str(n + 7), "candidate_name": name,
        "emails": ["h@example.com"], "website_addresses": [], "social_media_addresses": [],
        "job_title": "Software Engineer Intern", "applied_at": applied_at,
        "profile_url": f"http://ats.local/candidates/{n}",
        "resume": {"filename": resume, "url": "http://x/" + resume, "type": "resume"},
        "event_id": f"evt{n}",
    }


@pytest.fixture
def app_id(runs):
    app = make_app()
    pipeline.run(app, wait=True)
    return app["application_id"]


def slack_log(runs):
    p = runs / "_slack_stub.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


class Recorder:
    """Replaces pipeline functions; remembers calls and signals each one."""

    def __init__(self, monkeypatch, *names, delay=0.0, result=None):
        self.calls = []
        self.event = threading.Event()
        for name in names:
            monkeypatch.setattr(pipeline, name, self._make(name, delay))

    def _make(self, name, delay):
        def fake(*args, **kwargs):
            self.calls.append((name, args))
            self.event.set()
            time.sleep(delay)
        return fake

    def wait(self):
        assert self.event.wait(5), "pipeline function was not called"
        self.event.clear()
        return self.calls[-1]


def act(client, payload, **kw):
    return client.post("/internal/actions", json=payload, headers=TOKEN, **kw)


def block_action(action_id, value=None, *, selected=None, trigger_id=None, user=None):
    action = {"action_id": action_id, "type": "button"}
    if selected is not None:
        action.update(type="overflow", selected_option={"value": selected})
    else:
        action["value"] = value
    payload = {"type": "block_actions", "actions": [action],
               "user": user or {"id": "U1", "name": "jordan"}}
    if trigger_id:
        payload["trigger_id"] = trigger_id
    return payload


def submission(app_id, claim_id="c3", version=1, **overrides):
    payload = json.loads((FIXTURES / "slack" / "edit_submit.json").read_text())
    payload["view"]["private_metadata"] = json.dumps(
        {"application_id": app_id, "claim_id": claim_id, "claims_version": version})
    for block, value in overrides.items():
        payload["view"]["state"]["values"][block]["v"]["value"] = value
    return payload


# ── auth ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("method,path", [
    ("post", "/internal/actions"), ("get", "/internal/verdict?candidate=x"),
    ("post", "/internal/recheck"), ("get", "/internal/explain?candidate=x&claim=y"),
    ("get", "/internal/health")])
def test_401_without_or_with_wrong_token(client, method, path):
    for headers in ({}, {"X-Verifier-Token": "nope"}):
        r = getattr(client, method)(path, headers=headers)
        assert r.status_code == 401 and r.json() == {"error": "unauthorized"}


def test_health(client):
    r = client.get("/internal/health", headers=TOKEN)
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert {"real_modules", "fetch_mode"} <= set(r.json())


# ── verdict ───────────────────────────────────────────────────────────────────

def test_verdict_shape(client, app_id):
    r = client.get("/internal/verdict", params={"candidate": "henry"}, headers=TOKEN)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"application_id", "candidate_name", "job_title", "card_permalink",
                         "runs", "claims", "verdict", "other_matches"}
    assert body["application_id"] == app_id and body["candidate_name"] == "Henry Chen"
    assert body["card_permalink"] and body["runs"] == {"c1": "done", "c2": "done", "c3": "done"}
    assert [c["claim_id"] for c in body["claims"]][:3] == ["c1", "c2", "c3"] and len(body["claims"]) > 3
    assert body["verdict"]["application_id"] == app_id and body["other_matches"] == []


def test_verdict_by_id_and_other_matches(client, runs, app_id):
    older = make_app(applied_at="2026-10-24T09:00:00Z")
    pipeline.run(older, wait=True)
    body = client.get("/internal/verdict", params={"candidate": "Henry"}, headers=TOKEN).json()
    assert body["application_id"] == app_id            # newest first
    assert body["other_matches"] == [{"application_id": older["application_id"],
                                      "candidate_name": "Henry Chen"}]
    by_id = client.get("/internal/verdict", params={"candidate": older["application_id"]},
                       headers=TOKEN).json()
    assert by_id["application_id"] == older["application_id"] and by_id["other_matches"] == []


def test_verdict_404(client, app_id):
    r = client.get("/internal/verdict", params={"candidate": "nobody"}, headers=TOKEN)
    assert r.status_code == 404 and r.json() == {"error": "not_found"}
    assert client.get("/internal/verdict", headers=TOKEN).status_code == 404


# ── recheck ───────────────────────────────────────────────────────────────────

def test_recheck_started_and_queued(client, monkeypatch, app_id):
    rec = Recorder(monkeypatch, "recheck")
    r = client.post("/internal/recheck", json={"candidate": "henry", "by": "jake"}, headers=TOKEN)
    assert r.status_code == 202 and r.json() == {"application_id": app_id, "status": "started"}
    assert rec.wait() == ("recheck", (app_id, "jake"))
    with pipeline._lock(app_id):                      # a flow holds the lock
        r = client.post("/internal/recheck", json={"candidate": app_id, "by": "jake"},
                        headers=TOKEN)
    assert r.status_code == 202 and r.json() == {"application_id": app_id, "status": "queued"}
    rec.wait()


def test_recheck_404_and_bad_body(client, monkeypatch, app_id):
    rec = Recorder(monkeypatch, "recheck")
    r = client.post("/internal/recheck", json={"candidate": "nobody", "by": "x"}, headers=TOKEN)
    assert r.status_code == 404 and r.json() == {"error": "not_found"}
    assert client.post("/internal/recheck", content=b"nope", headers=TOKEN).status_code == 400
    assert client.post("/internal/recheck", json={}, headers=TOKEN).status_code == 400
    assert not rec.calls


# ── explain ───────────────────────────────────────────────────────────────────

def test_explain_with_run_block(client, app_id):
    r = client.get("/internal/explain", params={"candidate": "henry", "claim": "c3"}, headers=TOKEN)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"claim_id", "type", "source_text", "label", "top_reason", "checks",
                         "notes", "flags", "evidence_links", "run"}
    assert body["claim_id"] == "c3" and body["type"] == "project"
    trace = json.loads((FIXTURES / "run_traces" / "c3.json").read_text())
    assert body["run"] == {"outcome": trace["judge"]["outcome"],
                           "rationale": trace["judge"]["rationale"],
                           "baseline_step": trace["baseline"]["step"],
                           "baseline_exit_code": trace["baseline"]["exit_code"]}


def test_explain_by_text_and_run_null(client, app_id):
    r = client.get("/internal/explain", params={"candidate": app_id, "claim": "c4"}, headers=TOKEN)
    assert r.status_code == 200 and r.json()["claim_id"] == "c4" and r.json()["run"] is None
    claims = client.get("/internal/verdict", params={"candidate": app_id},
                        headers=TOKEN).json()["claims"]
    text = claims[2]["source_text"][:20].lower()
    r = client.get("/internal/explain", params={"candidate": app_id, "claim": text}, headers=TOKEN)
    assert r.status_code == 200


def test_explain_404(client, app_id):
    for params in ({"candidate": "nobody", "claim": "c3"}, {"candidate": "henry", "claim": "zzzz"},
                   {"candidate": "henry"}):
        r = client.get("/internal/explain", params=params, headers=TOKEN)
        assert r.status_code == 404 and r.json() == {"error": "not_found"}


# ── actions: block_actions ────────────────────────────────────────────────────

def test_recheck_button(client, monkeypatch):
    rec = Recorder(monkeypatch, "recheck")
    payload = json.loads((FIXTURES / "slack" / "recheck.json").read_text())
    r = act(client, payload)
    assert r.status_code == 200 and r.json() == {"ack_payload": None}
    assert rec.wait() == ("recheck", ("12345", "jordan"))


def test_by_falls_back_to_username_then_id(client, monkeypatch):
    rec = Recorder(monkeypatch, "recheck")
    act(client, block_action("openclaw:rv:recheck", "1", user={"id": "U9", "username": "ju"}))
    assert rec.wait()[1] == ("1", "ju")
    act(client, block_action("openclaw:rv:recheck", "1", user={"id": "U9"}))
    assert rec.wait()[1] == ("1", "U9")


@pytest.mark.parametrize("kind", ["more_context", "suggest_questions"])
def test_claim_menu_generate(client, monkeypatch, kind):
    rec = Recorder(monkeypatch, "generate")
    r = act(client, block_action("openclaw:rv:claim_menu", selected=f"{kind}|12345|c3"))
    assert r.json() == {"ack_payload": None}
    assert rec.wait() == ("generate", ("12345", "c3", kind, "jordan"))


def test_overflow_fixture(client, monkeypatch):
    rec = Recorder(monkeypatch, "generate")
    payload = json.loads((FIXTURES / "slack" / "overflow_more_context.json").read_text())
    assert act(client, payload).json() == {"ack_payload": None}
    assert rec.wait() == ("generate", ("12345", "c3", "more_context", "jordan"))


def test_edit_claim_with_trigger_opens_modal(client, runs, app_id):
    r = act(client, block_action("openclaw:rv:claim_menu", selected=f"edit_claim|{app_id}|c3",
                                 trigger_id="trig.1"))
    assert r.status_code == 200 and r.json() == {"ack_payload": None}
    calls = [l for l in slack_log(runs) if l["call"] == "open_modal"]
    assert len(calls) == 1 and calls[0]["text"] == "openclaw:rv:edit_claim"  # the callback_id


def test_edit_claim_modal_arguments(client, monkeypatch, app_id):
    seen = {}
    monkeypatch.setattr(service.registry.get("slack"), "open_modal",
                        lambda t, v: seen.update(trigger=t, view=v), raising=False)
    act(client, block_action("openclaw:rv:claim_menu", selected=f"edit_claim|{app_id}|c3",
                             trigger_id="trig.2"))
    assert seen["trigger"] == "trig.2"
    assert json.loads(seen["view"]["private_metadata"]) == {
        "application_id": app_id, "claim_id": "c3", "claims_version": 1}


def test_edit_claim_without_trigger_replies_not_available(client, runs, app_id):
    r = act(client, block_action("openclaw:rv:claim_menu", selected=f"edit_claim|{app_id}|c3"))
    assert r.json() == {"ack_payload": None}
    deadline = time.time() + 5
    while time.time() < deadline:
        log = slack_log(runs)
        if any("Edit claim isn't available" in (l["text"] or "") for l in log):
            break
        time.sleep(0.05)
    assert any(l["call"] == "post_thread" and "Edit claim isn't available" in l["text"]
               for l in slack_log(runs))
    assert not [l for l in slack_log(runs) if l["call"] == "open_modal"]


def test_edit_claim_unknown_claim_still_acks(client, runs, app_id):
    r = act(client, block_action("openclaw:rv:claim_menu", selected=f"edit_claim|{app_id}|zz",
                                 trigger_id="t"))
    assert r.json() == {"ack_payload": None}
    assert not [l for l in slack_log(runs) if l["call"] == "open_modal"]


def test_transcript_button(client, monkeypatch):
    rec = Recorder(monkeypatch, "show_transcript")
    r = act(client, block_action("openclaw:rv:transcript", "12345|c3"))
    assert r.json() == {"ack_payload": None}
    assert rec.wait() == ("show_transcript", ("12345", "c3"))


@pytest.mark.parametrize("action_id", ["openclaw:rv:open_ats", "openclaw:rv:open_pdf",
                                       "openclaw:rv:mystery", ""])
def test_url_buttons_and_unknown_ack_only(client, monkeypatch, action_id):
    rec = Recorder(monkeypatch, "recheck", "generate", "show_transcript", "edit_claim")
    r = act(client, block_action(action_id, "x"))
    assert r.status_code == 200 and r.json() == {"ack_payload": None}
    time.sleep(0.1)
    assert not rec.calls


def test_odd_payloads_are_acked(client):
    for payload in ({}, {"type": "block_actions"}, {"type": "shortcut"},
                    block_action("openclaw:rv:claim_menu", selected="garbage"),
                    block_action("openclaw:rv:transcript", "no-bar")):
        assert act(client, payload).json() == {"ack_payload": None}


def test_form_encoded_payload(client, monkeypatch):
    rec = Recorder(monkeypatch, "recheck")
    payload = json.loads((FIXTURES / "slack" / "recheck.json").read_text())
    r = client.post("/internal/actions", content=urlencode({"payload": json.dumps(payload)}),
                    headers={**TOKEN, "Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 200 and r.json() == {"ack_payload": None}
    assert rec.wait() == ("recheck", ("12345", "jordan"))


def test_form_encoded_view_submission(client, monkeypatch, app_id):
    rec = Recorder(monkeypatch, "edit_claim")
    r = client.post("/internal/actions",
                    content=urlencode({"payload": json.dumps(submission(app_id))}),
                    headers={**TOKEN, "Content-Type": "application/x-www-form-urlencoded"})
    assert r.json() == {"ack_payload": None}
    rec.wait()


def test_bad_bodies_are_400(client):
    assert client.post("/internal/actions", content=b"{nope", headers=TOKEN).status_code == 400
    assert client.post("/internal/actions", content=b"a=b", headers={
        **TOKEN, "Content-Type": "application/x-www-form-urlencoded"}).status_code == 400
    assert client.post("/internal/actions", content=b"[1]", headers=TOKEN).status_code == 400


# ── actions: view_submission ──────────────────────────────────────────────────

def test_valid_submission_starts_edit_and_closes_modal(client, monkeypatch, app_id):
    rec = Recorder(monkeypatch, "edit_claim")
    r = act(client, submission(app_id))
    assert r.status_code == 200 and r.json() == {"ack_payload": None}
    name, (a, c, changes, by) = rec.wait()
    assert (a, c, by) == (app_id, "c3", "jordan")
    assert changes["repo_name"] == "cubesolver" and changes["role"] == "creator"
    assert changes["scale"] == "small" and changes["start_year"] == "2024"
    assert changes["repo_url"] is None and changes["min_stars"] is None


def test_invalid_submission_returns_errors(client, monkeypatch, app_id):
    rec = Recorder(monkeypatch, "edit_claim")
    r = act(client, submission(app_id, start_year="abc"))
    body = r.json()
    assert r.status_code == 200
    assert body["ack_payload"]["response_action"] == "errors"
    assert "start_year" in body["ack_payload"]["errors"]
    time.sleep(0.1)
    assert not rec.calls


def test_stale_claims_version(client, monkeypatch, app_id):
    rec = Recorder(monkeypatch, "edit_claim")
    r = act(client, submission(app_id, version=0))
    ack = r.json()["ack_payload"]
    assert ack["response_action"] == "errors"
    assert list(ack["errors"].values()) == [
        "The claims changed since you opened this; reopen Edit claim."]
    assert set(ack["errors"]) <= set(submission(app_id)["view"]["state"]["values"])
    time.sleep(0.1)
    assert not rec.calls


def test_submission_for_unknown_application(client, monkeypatch):
    rec = Recorder(monkeypatch, "edit_claim")
    ack = act(client, submission("99999999")).json()["ack_payload"]
    assert ack["response_action"] == "errors"
    assert not rec.calls


def test_other_callback_ids_are_ignored(client):
    payload = submission("1")
    payload["view"]["callback_id"] = "something:else"
    assert act(client, payload).json() == {"ack_payload": None}


def test_valid_submission_end_to_end_makes_claims_v2(client, runs, app_id):
    assert act(client, submission(app_id, behavior="Solves a scrambled cube")).json() == {
        "ack_payload": None}
    rd = store.run_dir(app_id)
    deadline = time.time() + 20
    while time.time() < deadline and not (rd / "claims.v2.json").exists():
        time.sleep(0.1)
    assert (rd / "claims.v2.json").is_file()


# ── timing ────────────────────────────────────────────────────────────────────

def test_actions_answer_fast_with_a_slow_pipeline(client, monkeypatch, app_id):
    slow = Recorder(monkeypatch, "recheck", "generate", "show_transcript", "edit_claim", delay=3.0)
    payloads = [
        block_action("openclaw:rv:recheck", app_id),
        block_action("openclaw:rv:claim_menu", selected=f"more_context|{app_id}|c3"),
        block_action("openclaw:rv:claim_menu", selected=f"edit_claim|{app_id}|c3"),
        block_action("openclaw:rv:transcript", f"{app_id}|c3"),
        submission(app_id),
    ]
    for payload in payloads:
        start = time.monotonic()
        assert act(client, payload).status_code == 200
        assert time.monotonic() - start < 1.0
    start = time.monotonic()
    r = client.post("/internal/recheck", json={"candidate": "henry", "by": "j"}, headers=TOKEN)
    assert r.status_code == 202 and time.monotonic() - start < 1.0
    slow.wait()
