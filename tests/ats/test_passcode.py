"""Mock ATS passcode gate: only tunnelled requests (Cf-Connecting-Ip) are gated."""
import hashlib
import hmac

import httpx
import pytest
from fastapi.testclient import TestClient

from verifier.ats import mock_app

PASS = "open-sesame"
CF = {"Cf-Connecting-Ip": "203.0.113.7"}


def cookie_for(passcode: str) -> dict:
    v = hmac.new(passcode.encode(), b"rv-gate-v1", hashlib.sha256).hexdigest()
    return {"Cookie": f"rv_gate={v}"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    for k, v in {
        "RUNS_DIR": str(tmp_path / "runs"), "ATS_PUBLIC_URL": "http://ats.test",
        "VERIFIER_WEBHOOK_URL": "http://verifier.test/webhooks/greenhouse",
        "GREENHOUSE_WEBHOOK_SECRET": "s", "HARVEST_CLIENT_ID": "mock",
        "HARVEST_CLIENT_SECRET": "mock-secret",
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("MOCK_ATS_PASSCODE", PASS)
    app = mock_app.create_app(webhook_client=httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))))
    return TestClient(app, base_url="http://ats.test", follow_redirects=False)


def test_no_passcode_means_no_gate(client, monkeypatch):
    monkeypatch.delenv("MOCK_ATS_PASSCODE")
    assert client.get("/", headers=CF).status_code == 200
    monkeypatch.setenv("MOCK_ATS_PASSCODE", "")
    assert client.get("/apply", headers=CF).status_code == 200


def test_local_requests_pass(client):
    assert client.get("/").status_code == 200
    assert client.get("/apply").status_code == 200
    assert client.get("/files/nope/x.pdf").status_code == 404
    r = client.post("/token", headers={"Authorization": "Basic bW9jazptb2NrLXNlY3JldA=="})
    assert r.status_code != 401 or r.json().get("error") != "passcode_required"
    r = client.post("/v3/notes", json={})
    assert r.json().get("error") != "passcode_required"


def test_public_without_cookie(client):
    r = client.get("/candidates/1?application_id=2", headers=CF)
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=%2Fcandidates%2F1%3Fapplication_id%3D2"
    for resp in (client.post("/apply", headers=CF, data={}),
                 client.get("/files/a/b.pdf", headers=CF),
                 client.get("/docs", headers=CF),
                 client.get("/openapi.json", headers=CF),
                 client.post("/token", headers=CF),
                 client.get("/_mock/status", headers=CF),
                 client.get("/static/rv/other.js", headers=CF)):
        assert resp.status_code == 401
        assert resp.json() == {"error": "passcode_required"}


def test_login_page_and_css_public(client):
    r = client.get("/login", headers=CF)
    assert r.status_code == 200
    for needle in ("GreenLight demo · judges", "Enter the passcode", "Passcode",
                   'autocomplete="current-password"', "Open the demo"):
        assert needle in r.text
    assert PASS not in r.text
    assert client.get("/static/rv/tokens.css", headers=CF).status_code == 200
    assert client.get("/static/rv/rv.css", headers=CF).status_code == 200


def test_wrong_passcode(client):
    r = client.post("/login", headers=CF, data={"passcode": "nope", "next": "/apply"})
    assert r.status_code == 401
    assert "That passcode isn&#39;t right." in r.text or "That passcode isn't right." in r.text
    assert "set-cookie" not in r.headers
    assert "nope" not in r.text


def test_right_passcode_sets_cookie_and_redirects(client):
    r = client.post("/login", headers=CF, data={"passcode": PASS, "next": "/apply"})
    assert r.status_code == 303
    assert r.headers["location"] == "/apply"
    sc = r.headers["set-cookie"].lower()
    assert "rv_gate=" in sc and "httponly" in sc and "secure" in sc
    assert "samesite=lax" in sc and "max-age=604800" in sc
    assert PASS not in r.headers["set-cookie"]


def test_cookie_unlocks_pages_and_apply(client):
    h = {**CF, **cookie_for(PASS)}
    assert client.get("/", headers=h).status_code == 200
    assert client.get("/apply", headers=h).status_code == 200
    r = client.post("/apply", headers=h, data={})
    assert r.status_code != 401 and r.status_code != 303


@pytest.mark.parametrize("nxt", ["//evil.com", "https://evil.com", "/\\evil.com", "evil"])
def test_open_redirect_blocked(client, nxt):
    r = client.post("/login", headers=CF, data={"passcode": PASS, "next": nxt})
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert 'value="/"' in client.get("/login", headers=CF, params={"next": nxt}).text


def test_throttle(client):
    for _ in range(10):
        assert client.post("/login", headers=CF, data={"passcode": "bad"}).status_code == 401
    r = client.post("/login", headers=CF, data={"passcode": "bad"})
    assert r.status_code == 429 and "Too many tries. Wait a few minutes." in r.text
    assert client.post("/login", headers=CF, data={"passcode": PASS}).status_code == 429
    other = {"Cf-Connecting-Ip": "198.51.100.9"}
    assert client.post("/login", headers=other, data={"passcode": "bad"}).status_code == 401


def test_changing_passcode_invalidates_cookie(client, monkeypatch):
    h = {**CF, **cookie_for(PASS)}
    assert client.get("/apply", headers=h).status_code == 200
    monkeypatch.setenv("MOCK_ATS_PASSCODE", "new-one")
    assert client.get("/apply", headers=h).status_code == 303


def test_no_page_says_greenhouse(client):
    h = {**CF, **cookie_for(PASS)}
    for path in ("/", "/apply"):
        assert "greenhouse" not in client.get(path, headers=h).text.lower()
    assert "greenhouse" not in client.get("/login", headers=CF).text.lower()
