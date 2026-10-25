"""Verifier service (J3): the webhook route and /internal/health."""
import hashlib
import hmac
import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from verifier.core import service

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
SECRET = "service-secret"


@pytest.fixture
def runs(tmp_path, monkeypatch):
    path = tmp_path / "runs"
    for k, v in {"RUNS_DIR": str(path), "VERIFIER_REAL": "ats", "INTERNAL_TOKEN": "tok",
                 "GREENHOUSE_WEBHOOK_SECRET": SECRET}.items():
        monkeypatch.setenv(k, v)
    return path


@pytest.fixture
def started(monkeypatch):
    """Replace pipeline.run with a recorder that blocks until released."""
    calls, release, done = [], threading.Event(), threading.Event()

    def fake_run(app, **_):
        calls.append(app)
        done.set()
        assert release.wait(10)

    monkeypatch.setattr(service.pipeline, "run", fake_run)
    yield type("S", (), {"calls": calls, "release": release, "done": done})
    release.set()


@pytest.fixture
def client(runs, started):
    return TestClient(service.create_app())


def signed(name="henry", event_id="evt-1", secret=SECRET):
    raw = json.dumps(json.loads((FIXTURES / "webhooks" / f"{name}.json").read_text())).encode()
    headers = {"Signature": "sha256 " + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest(),
               "Greenhouse-Event-ID": event_id}
    return raw, headers


def post(client, raw, headers):
    return client.post("/webhooks/greenhouse", content=raw, headers=headers)


def test_good_webhook_is_accepted_and_starts_the_pipeline(client, started, runs):
    raw, headers = signed()
    r = post(client, raw, headers)
    assert r.status_code == 200 and r.json() == {"status": "accepted"}
    assert started.done.wait(5)
    assert started.calls[0]["application_id"] == "1000001"
    assert (runs / "1000001" / "webhook.json").read_bytes() == raw


def test_response_does_not_wait_for_the_pipeline(client, started):
    raw, headers = signed()
    r = post(client, raw, headers)  # fake_run is still blocked on `release`
    assert r.status_code == 200
    assert started.done.wait(5) and not started.release.is_set()


def test_bad_signature_is_401(client, started):
    raw, headers = signed(secret="wrong")
    r = post(client, raw, headers)
    assert r.status_code == 401 and r.json() == {"error": "bad_signature"}
    assert not started.done.wait(0.2)


def test_missing_signature_is_401(client):
    raw, headers = signed()
    del headers["Signature"]
    r = post(client, raw, headers)
    assert r.status_code == 401 and r.json() == {"error": "bad_signature"}


def test_tampered_body_is_401(client):
    raw, headers = signed()
    assert post(client, raw[:-3] + b"X" + raw[-2:], headers).status_code == 401


def test_duplicate_event(client, started):
    raw, headers = signed()
    assert post(client, raw, headers).json() == {"status": "accepted"}
    assert post(client, raw, headers).json() == {"status": "duplicate"}
    assert started.done.wait(5)
    assert len(started.calls) == 1


def test_ping_is_ignored(client, started):
    raw = json.dumps({"action": "ping", "payload": {}}).encode()
    headers = {"Signature": "sha256 " + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()}
    r = post(client, raw, headers)
    assert r.status_code == 200 and r.json() == {"status": "ignored"}
    assert not started.done.wait(0.2)


def test_signed_garbage_is_400(client):
    raw = b"not json"
    headers = {"Signature": "sha256 " + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()}
    r = post(client, raw, headers)
    assert r.status_code == 400 and r.json()["error"] == "bad_request"


def test_a_crashing_pipeline_does_not_break_the_service(runs, monkeypatch):
    ran = threading.Event()

    def boom(app, **_):
        ran.set()
        raise RuntimeError("stage blew up")

    monkeypatch.setattr(service.pipeline, "run", boom)
    client = TestClient(service.create_app())
    raw, headers = signed()
    assert post(client, raw, headers).status_code == 200
    assert ran.wait(5)
    assert client.get("/internal/health", headers={"X-Verifier-Token": "tok"}).status_code == 200


def test_the_secret_falls_back_to_dev_secret_like_demo(runs, started, monkeypatch):
    monkeypatch.setenv("GREENHOUSE_WEBHOOK_SECRET", "")
    client = TestClient(service.create_app())
    raw, headers = signed(secret="dev-secret")
    assert post(client, raw, headers).json() == {"status": "accepted"}


# ── /internal/health ──────────────────────────────────────────────────────────

def test_health(client, monkeypatch):
    r = client.get("/internal/health", headers={"X-Verifier-Token": "tok"})
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "real_modules": ["ats"], "fetch_mode": "replay"}


@pytest.mark.parametrize("headers", [{}, {"X-Verifier-Token": "wrong"}, {"X-Verifier-Token": ""}])
def test_health_needs_the_token(client, headers):
    r = client.get("/internal/health", headers=headers)
    assert r.status_code == 401 and r.json() == {"error": "unauthorized"}


def test_health_with_no_configured_token_fails_closed(runs, started, monkeypatch):
    monkeypatch.setenv("INTERNAL_TOKEN", "")
    client = TestClient(service.create_app())
    assert client.get("/internal/health").status_code == 401
    assert client.get("/internal/health", headers={"X-Verifier-Token": ""}).status_code == 401


def test_webhook_route_does_not_need_the_internal_token(client):
    raw, headers = signed()
    assert post(client, raw, headers).status_code == 200  # no X-Verifier-Token sent


# ── mock ATS -> service, in process ───────────────────────────────────────────

def test_signed_apply_from_the_mock_reaches_the_service_and_the_write_back_lands(
        runs, monkeypatch, tmp_path):
    """J3 done line, in process: apply -> signed webhook accepted -> resume downloaded from the
    mock -> a private note and an `other` attachment land in the mock."""
    import httpx

    from verifier.ats import mock_app
    from verifier.ats.adapter import GreenhouseAdapter

    monkeypatch.setenv("ATS_PUBLIC_URL", "http://ats.test")
    monkeypatch.setenv("VERIFIER_WEBHOOK_URL", "http://verifier.test/webhooks/greenhouse")
    service_client = TestClient(service.create_app(), base_url="http://verifier.test")
    ats = TestClient(mock_app.create_app(webhook_client=service_client), base_url="http://ats.test")
    adapter = GreenhouseAdapter(
        webhook_secret=SECRET, harvest_auth_url="http://ats.test/token",
        harvest_base_url="http://ats.test/v3", client_id="mock", client_secret="mock", client=ats)
    finished = threading.Event()
    outcome = {}

    def stand_in_pipeline(app, **_):  # the part of pipeline.run that talks to the ATS
        try:
            from verifier.core import store
            rd = store.run_dir(app["application_id"])
            pdf = adapter.fetch_resume(app, rd / "resume.pdf")
            annotated = tmp_path / "henry_chen_resume_verified_1.pdf"
            annotated.write_bytes(pdf.read_bytes())
            outcome["result"] = adapter.write_back(app, "Resume check\ndone.", annotated)
        except BaseException as e:  # surface it in the assertion below
            outcome["error"] = e
        finally:
            finished.set()

    monkeypatch.setattr(service.pipeline, "run", stand_in_pipeline)

    resp = ats.post("/_mock/apply", json={"fixture": "henry"})
    assert resp.status_code == 200
    result = resp.json()
    assert result["webhook_status"] == "accepted"
    assert finished.wait(10) and "error" not in outcome, outcome.get("error")

    rd = runs / str(result["application_id"])
    assert (rd / "webhook.json").is_file()
    assert (rd / "resume.pdf").read_bytes() == (FIXTURES / "resumes" / "henry.pdf").read_bytes()

    state = ats.app.state.mock.snapshot()
    (note,) = state["notes"]
    assert note["visibility"] == "private" and note["candidate_id"] == result["candidate_id"]
    others = [a for a in state["attachments"] if a["type"] == "other"]
    assert len(others) == 1 and others[0]["application_id"] == result["application_id"]
    assert [a["type"] for a in state["attachments"]].count("resume") == 1

    # Replays: the same event is a duplicate; a tampered body is a 401.
    from verifier.ats import apply as apply_cli
    assert apply_cli.replay(str(result["application_id"]), client=service_client) == (
        200, {"status": "duplicate"})
    status, body = apply_cli.replay(str(result["application_id"]), tampered=True,
                                    client=service_client)
    assert status == 401 and body == {"error": "bad_signature"}
