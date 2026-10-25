"""Mock ATS (J3): apply, files, token, notes, attachments. Offline: TestClient + MockTransport."""
import base64
import hashlib
import hmac
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from verifier.ats import mock_app

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
SECRET = "test-secret"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    for k, v in {
        "RUNS_DIR": str(runs), "ATS_PUBLIC_URL": "http://ats.test",
        "VERIFIER_WEBHOOK_URL": "http://verifier.test/webhooks/greenhouse",
        "GREENHOUSE_WEBHOOK_SECRET": SECRET, "HARVEST_CLIENT_ID": "mock",
        "HARVEST_CLIENT_SECRET": "mock-secret",
    }.items():
        monkeypatch.setenv(k, v)
    return runs


@pytest.fixture
def sent():
    """Webhook POSTs the mock sends out (intercepted), newest last."""
    return []


@pytest.fixture
def client(cfg, sent):
    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"status": "accepted"})

    app = mock_app.create_app(webhook_client=httpx.Client(transport=httpx.MockTransport(handler)))
    return TestClient(app, base_url="http://ats.test")


def token(client, secret="mock-secret", cid="mock") -> str:
    r = client.post("/token", auth=(cid, secret), data={"grant_type": "client_credentials"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def bearer(client) -> dict:
    return {"Authorization": f"Bearer {token(client)}"}


def apply(client, fixture="henry"):
    r = client.post("/_mock/apply", json={"fixture": fixture})
    assert r.status_code == 200, r.text
    return r.json()


# ── apply ─────────────────────────────────────────────────────────────────────

def test_apply_sends_a_correctly_signed_ascii_body_with_fresh_ids(client, sent):
    result = apply(client)
    assert result["webhook_status"] == "accepted"
    assert isinstance(result["application_id"], int) and isinstance(result["candidate_id"], int)
    assert len(sent) == 1
    req = sent[0]
    assert str(req.url) == "http://verifier.test/webhooks/greenhouse"
    raw = req.content
    assert raw.isascii()  # ASCII-escaped like Greenhouse
    expected = "sha256 " + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    assert req.headers["Signature"] == expected
    assert req.headers["Greenhouse-Event-ID"] == result["event_id"]

    body = json.loads(raw)
    app = body["payload"]["application"]
    assert body["action"] == "new_candidate_application"
    assert app["id"] == result["application_id"]
    assert app["candidate"]["id"] == result["candidate_id"]
    assert app["url"] == (f"http://ats.test/candidates/{result['candidate_id']}"
                          f"?application_id={result['application_id']}")
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.000Z", app["applied_at"])
    resume = next(a for a in app["candidate"]["attachments"] if a["type"] == "resume")
    assert resume["url"].startswith("http://ats.test/files/")
    assert resume["url"].endswith("/henry.pdf")


def test_apply_ids_are_fresh_every_time(client, sent):
    a, b = apply(client), apply(client)
    assert a["application_id"] != b["application_id"]
    assert a["candidate_id"] != b["candidate_id"]
    assert a["event_id"] != b["event_id"]
    assert len({r.headers["Greenhouse-Event-ID"] for r in sent}) == 2


def test_apply_skips_ids_that_already_have_a_run_folder(cfg, client):
    (cfg / "1000001").mkdir(parents=True)
    assert apply(client)["application_id"] == 1000002


def test_apply_stores_the_fixture_resume_and_serves_it(client, sent):
    apply(client)
    url = json.loads(sent[0].content)["payload"]["application"]["candidate"]["attachments"][0]["url"]
    r = client.get(urlparse(url).path)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content == (FIXTURES / "resumes" / "henry.pdf").read_bytes()


def test_apply_with_the_fake_fixture(client, sent):
    apply(client, "fake")
    assert json.loads(sent[0].content)["payload"]["application"]["candidate"]["first_name"] == "Alex"


def test_apply_reports_an_unreachable_verifier(cfg):
    def boom(request):
        raise httpx.ConnectError("refused")

    app = mock_app.create_app(webhook_client=httpx.Client(transport=httpx.MockTransport(boom)))
    r = TestClient(app).post("/_mock/apply", json={"fixture": "henry"})
    assert r.status_code == 200
    assert r.json()["webhook_status"] == "unreachable"
    assert "refused" in r.json()["webhook_error"]


def test_apply_reports_a_bad_signature_response(cfg):
    app = mock_app.create_app(webhook_client=httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(401, json={"error": "bad_signature"}))))
    r = TestClient(app).post("/_mock/apply", json={"fixture": "henry"})
    assert r.json()["webhook_status"] == "http_401"


@pytest.mark.parametrize("name", ["nope", "../henry", "", "a/b"])
def test_apply_unknown_fixture_is_404(client, name):
    assert client.post("/_mock/apply", json={"fixture": name}).status_code == 404


def test_files_404_for_unknown_or_traversal(client):
    assert client.get("/files/nope/x.pdf").status_code == 404
    assert client.get("/files/f1/..%2F..%2Fstate.json").status_code == 404


def test_state_is_persisted_and_reloaded(cfg, client):
    apply(client)
    assert (cfg / "_mock_ats" / "state.json").is_file()
    again = TestClient(mock_app.create_app(), base_url="http://ats.test")
    again.post("/_mock/apply", json={"fixture": "henry"})  # webhook fails (no server): fine
    state = json.loads((cfg / "_mock_ats" / "state.json").read_text())
    assert len(state["applications"]) == 2  # the first run's state survived the restart


# ── token ─────────────────────────────────────────────────────────────────────

def test_token_success(client):
    r = client.post("/token", auth=("mock", "mock-secret"), data={"grant_type": "client_credentials"})
    assert r.status_code == 200
    body = r.json()
    assert body["token_type"] == "Bearer" and body["expires_in"] == 3600 and body["access_token"]
    assert token(client) != body["access_token"]


@pytest.mark.parametrize("auth", [("mock", "wrong"), ("other", "mock-secret"), None])
def test_token_bad_client_is_401(client, auth):
    r = client.post("/token", auth=auth, data={"grant_type": "client_credentials"})
    assert r.status_code == 401
    assert r.json()["error"] == "invalid_client"


def test_token_wrong_grant_type_is_400(client):
    r = client.post("/token", auth=("mock", "mock-secret"), data={"grant_type": "password"})
    assert r.status_code == 400
    assert client.post("/token", auth=("mock", "mock-secret")).status_code == 400


def test_token_accepts_sub(client):
    r = client.post("/token", auth=("mock", "mock-secret"),
                    data={"grant_type": "client_credentials", "sub": "123"})
    assert r.status_code == 200


# ── notes ─────────────────────────────────────────────────────────────────────

def make_candidate(client) -> dict:
    r = apply(client)
    return {"candidate_id": r["candidate_id"], "application_id": r["application_id"]}


@pytest.mark.parametrize("visibility,spelled", [
    ("public", "publicly_visible"), ("private", "privately_visible"),
    ("admin_only", "admin_only_visible")])
def test_create_note_visibility_spellings(client, visibility, spelled):
    ids = make_candidate(client)
    r = client.post("/v3/notes", headers=bearer(client), json={
        **ids, "body": "line one\nline two", "note_type": "NOTE", "visibility": visibility})
    assert r.status_code == 201, r.text
    out = r.json()
    assert isinstance(out["id"], int) and out["type"] == "note" and out["visibility"] == spelled
    state = client.app.state.mock.snapshot()
    assert state["notes"][-1]["body"] == "line one\nline two"
    assert state["notes"][-1]["visibility"] == visibility


def test_note_without_application_id_is_ok(client):
    ids = make_candidate(client)
    r = client.post("/v3/notes", headers=bearer(client), json={
        "candidate_id": ids["candidate_id"], "body": "x", "note_type": "NOTE",
        "visibility": "private"})
    assert r.status_code == 201


@pytest.mark.parametrize("missing", ["candidate_id", "body", "note_type", "visibility"])
def test_note_required_fields_are_422(client, missing):
    ids = make_candidate(client)
    payload = {**ids, "body": "x", "note_type": "NOTE", "visibility": "private"}
    del payload[missing]
    r = client.post("/v3/notes", headers=bearer(client), json=payload)
    assert r.status_code == 422
    assert missing in r.json()["message"]


@pytest.mark.parametrize("patch", [
    {"visibility": "secret"}, {"note_type": "EMAIL"}, {"body": "  "},
    {"candidate_id": "2000001"}, {"application_id": "1000001"}, {"candidate_id": True},
])
def test_note_invalid_fields_are_422(client, patch):
    ids = make_candidate(client)
    payload = {**ids, "body": "x", "note_type": "NOTE", "visibility": "private", **patch}
    assert client.post("/v3/notes", headers=bearer(client), json=payload).status_code == 422


def test_note_non_json_body_is_422(client):
    r = client.post("/v3/notes", headers=bearer(client), content=b"nope")
    assert r.status_code == 422


def test_note_requires_a_token(client):
    ids = make_candidate(client)
    payload = {**ids, "body": "x", "note_type": "NOTE", "visibility": "private"}
    assert client.post("/v3/notes", json=payload).status_code == 401
    assert client.post("/v3/notes", json=payload,
                       headers={"Authorization": "Bearer not-a-token"}).status_code == 401
    assert client.post("/v3/notes", json=payload,
                       headers={"Authorization": "Basic abc"}).status_code == 401


def test_note_expired_token_is_401(client):
    ids = make_candidate(client)
    headers = bearer(client)
    state = client.app.state.mock
    for t in state.data["tokens"]:
        state.data["tokens"][t] = 1.0  # long expired
    payload = {**ids, "body": "x", "note_type": "NOTE", "visibility": "private"}
    assert client.post("/v3/notes", headers=headers, json=payload).status_code == 401


def test_note_unknown_candidate_or_application_is_404(client):
    ids = make_candidate(client)
    base = {"body": "x", "note_type": "NOTE", "visibility": "private"}
    h = bearer(client)
    assert client.post("/v3/notes", headers=h, json={**base, "candidate_id": 999}).status_code == 404
    assert client.post("/v3/notes", headers=h, json={
        **base, "candidate_id": ids["candidate_id"], "application_id": 999}).status_code == 404


def test_no_edit_or_delete_routes(client):
    h = bearer(client)
    for method in ("put", "patch", "delete"):
        for path in ("/v3/notes", "/v3/notes/1", "/v3/attachments", "/v3/attachments/1"):
            assert getattr(client, method)(path, headers=h).status_code in (404, 405)


# ── attachments ───────────────────────────────────────────────────────────────

PDF = b"%PDF-1.4 fake annotated pdf \x00\xff bytes"


def post_attachment(client, **fields):
    return client.post("/v3/attachments", headers=bearer(client), json=fields)


def test_attachment_content_round_trip(client):
    ids = make_candidate(client)
    r = post_attachment(client, application_id=ids["application_id"],
                        filename="henry_resume_verified_1.pdf", type="other",
                        content=base64.b64encode(PDF).decode())
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["application_id"] == ids["application_id"]
    assert out["candidate_id"] == ids["candidate_id"]
    assert out["type"] == "other" and out["filename"] == "henry_resume_verified_1.pdf"
    assert out["url"].startswith("http://ats.test/files/")
    got = client.get(urlparse(out["url"]).path)
    assert got.status_code == 200 and got.content == PDF


def test_attachment_url_is_stored_not_fetched(client):
    ids = make_candidate(client)
    r = post_attachment(client, application_id=ids["application_id"], filename="x.pdf",
                        type="other", url="http://unreachable.invalid/x.pdf")
    assert r.status_code == 201
    assert r.json()["url"] == "http://unreachable.invalid/x.pdf"


def test_attachment_needs_exactly_one_of_content_or_url(client):
    ids = make_candidate(client)
    common = {"application_id": ids["application_id"], "filename": "x.pdf", "type": "other"}
    assert post_attachment(client, **common).status_code == 422
    assert post_attachment(client, **common, content="QQ==", url="http://a/b").status_code == 422


@pytest.mark.parametrize("patch", [
    {"content": "not base64!!"}, {"type": "weird"}, {"filename": "../etc/passwd"},
    {"filename": "a/b.pdf"}, {"filename": ""}, {"application_id": "1000001"},
    {"visibility": "secret"}, {"content": 12},
])
def test_attachment_invalid_fields_are_422(client, patch):
    ids = make_candidate(client)
    payload = {"application_id": ids["application_id"], "filename": "x.pdf", "type": "other",
               "content": "QQ==", **patch}
    assert post_attachment(client, **payload).status_code == 422


@pytest.mark.parametrize("missing", ["application_id", "filename", "type"])
def test_attachment_required_fields_are_422(client, missing):
    ids = make_candidate(client)
    payload = {"application_id": ids["application_id"], "filename": "x.pdf", "type": "other",
               "content": "QQ=="}
    del payload[missing]
    assert post_attachment(client, **payload).status_code == 422


def test_attachment_unknown_application_is_404(client):
    assert post_attachment(client, application_id=424242, filename="x.pdf", type="other",
                           content="QQ==").status_code == 404


def test_attachment_requires_a_token(client):
    ids = make_candidate(client)
    r = client.post("/v3/attachments", json={
        "application_id": ids["application_id"], "filename": "x.pdf", "type": "other",
        "content": "QQ=="})
    assert r.status_code == 401


def test_uploading_a_resume_demotes_the_current_resume(client):
    ids = make_candidate(client)
    r = post_attachment(client, application_id=ids["application_id"], filename="new.pdf",
                        type="resume", content=base64.b64encode(PDF).decode())
    assert r.status_code == 201
    atts = client.app.state.mock.snapshot()["attachments"]
    by_name = {a["filename"]: a["type"] for a in atts}
    assert by_name == {"henry.pdf": "other", "new.pdf": "resume"}


def test_uploading_other_leaves_the_resume_alone(client):
    ids = make_candidate(client)
    post_attachment(client, application_id=ids["application_id"], filename="a.pdf", type="other",
                    content="QQ==")
    atts = client.app.state.mock.snapshot()["attachments"]
    assert {a["filename"]: a["type"] for a in atts} == {"henry.pdf": "resume", "a.pdf": "other"}


# ── live: two real uvicorn processes ──────────────────────────────────────────

def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for(url: str, timeout: float = 30.0, **kw) -> None:
    import time
    end = time.time() + timeout
    while time.time() < end:
        try:
            if httpx.get(url, timeout=2.0, **kw).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise AssertionError(f"{url} did not come up")


def test_live_two_uvicorn_smoke(tmp_path):
    """Mock ATS and verifier service as real servers on spare ports, VERIFIER_REAL=ats."""
    import subprocess
    import sys
    import time

    ats_port, svc_port = _free_port(), _free_port()
    runs = tmp_path / "runs"
    env = {**os.environ,
           "RUNS_DIR": str(runs), "VERIFIER_REAL": "ats", "GREENHOUSE_WEBHOOK_SECRET": "live-secret",
           "INTERNAL_TOKEN": "live-token", "HARVEST_CLIENT_ID": "mock", "HARVEST_CLIENT_SECRET": "mock",
           "ATS_PUBLIC_URL": f"http://127.0.0.1:{ats_port}",
           "HARVEST_AUTH_URL": f"http://127.0.0.1:{ats_port}/token",
           "HARVEST_BASE_URL": f"http://127.0.0.1:{ats_port}/v3",
           "VERIFIER_WEBHOOK_URL": f"http://127.0.0.1:{svc_port}/webhooks/greenhouse"}
    repo = Path(__file__).resolve().parents[2]
    logs = [open(tmp_path / "ats.log", "w"), open(tmp_path / "svc.log", "w")]
    procs = [
        subprocess.Popen([sys.executable, "-m", "uvicorn", "verifier.ats.mock_app:app",
                          "--port", str(ats_port), "--log-level", "warning"],
                         cwd=repo, env=env, stdout=logs[0], stderr=subprocess.STDOUT),
        subprocess.Popen([sys.executable, "-m", "uvicorn", "verifier.core.service:app",
                          "--port", str(svc_port), "--log-level", "warning"],
                         cwd=repo, env=env, stdout=logs[1], stderr=subprocess.STDOUT),
    ]
    try:
        _wait_for(f"http://127.0.0.1:{ats_port}/")
        _wait_for(f"http://127.0.0.1:{svc_port}/internal/health",
                  headers={"X-Verifier-Token": "live-token"})
        result = httpx.post(f"http://127.0.0.1:{ats_port}/_mock/apply", json={"fixture": "henry"},
                            timeout=30).json()
        assert result["webhook_status"] == "accepted", result
        app_id = str(result["application_id"])

        # The pipeline (stubs, real ATS adapter) downloads the resume and writes back.
        state_path = runs / "_mock_ats" / "state.json"
        deadline = time.time() + 60
        state = {}
        while time.time() < deadline:
            state = json.loads(state_path.read_text())
            if state["notes"] and any(a["type"] == "other" for a in state["attachments"]):
                break
            time.sleep(0.3)
        assert (runs / app_id / "webhook.json").is_file()
        assert (runs / app_id / "resume.pdf").is_file()
        assert [n["visibility"] for n in state["notes"]] == ["private"], (
            (tmp_path / "svc.log").read_text()[-2000:])
        assert [a["type"] for a in state["attachments"]].count("other") == 1

        # The candidate page shows the verifier's note.
        page = httpx.get(f"http://127.0.0.1:{ats_port}/candidates/{result['candidate_id']}"
                         f"?application_id={app_id}").text
        assert "Resume Verifier (integration)" in page and "Henry Chen" in page

        # Replays through the CLI: duplicate, then 401 when tampered.
        base = [sys.executable, "-m", "verifier.ats.apply"]
        dup = subprocess.run([*base, "--replay", app_id], cwd=repo, env=env,
                             capture_output=True, text=True, timeout=30)
        assert dup.returncode == 0 and "HTTP 200" in dup.stdout and "duplicate" in dup.stdout
        bad = subprocess.run([*base, "--replay", app_id, "--tamper"], cwd=repo, env=env,
                             capture_output=True, text=True, timeout=30)
        assert "HTTP 401" in bad.stdout and "bad_signature" in bad.stdout
        listing = subprocess.run([*base, "--list"], cwd=repo, env=env, capture_output=True,
                                 text=True, timeout=30)
        assert listing.stdout.split() == ["fake", "henry"]
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
        for f in logs:
            f.close()
