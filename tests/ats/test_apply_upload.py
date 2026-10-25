"""Mock ATS candidate-facing upload page (/apply) and the hide-simulate switch."""
import hashlib
import hmac
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from verifier.ats import mock_app

SECRET = "test-secret"
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
GOOD = {"first_name": "Ada", "last_name": "Lovelace", "email": "ada@example.com",
        "github_url": "https://github.com/ada-l"}


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    for k, v in {
        "RUNS_DIR": str(tmp_path / "runs"), "ATS_PUBLIC_URL": "http://ats.test",
        "VERIFIER_WEBHOOK_URL": "http://verifier.test/webhooks/greenhouse",
        "GREENHOUSE_WEBHOOK_SECRET": SECRET, "HARVEST_CLIENT_ID": "mock",
        "HARVEST_CLIENT_SECRET": "mock-secret",
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("MOCK_ATS_HIDE_SIMULATE", raising=False)


@pytest.fixture
def sent():
    return []


@pytest.fixture
def client(cfg, sent):
    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"status": "accepted"})

    app = mock_app.create_app(webhook_client=httpx.Client(transport=httpx.MockTransport(handler)))
    return TestClient(app, base_url="http://ats.test")


def post(client, data=None, filename="cv.pdf", content=PDF, **kw):
    files = {"resume": (filename, content, "application/pdf")} if content is not None else None
    return client.post("/apply", data=GOOD if data is None else data, files=files,
                       follow_redirects=False, **kw)


def applications(client):
    return client.app.state.mock.snapshot()["applications"]


def test_form_renders(client):
    r = client.get("/apply")
    assert r.status_code == 200
    assert "Careers · Software Engineer Intern" in r.text
    for name in ("first_name", "last_name", "email", "github_url", "resume"):
        assert f'name="{name}"' in r.text
    assert "Submit application" in r.text
    assert 'aria-current="page">Apply' in r.text


def test_valid_upload(client, sent):
    r = post(client)
    assert r.status_code == 303
    loc = r.headers["location"]
    assert loc.startswith("/apply/thanks?application_id=")
    snap = client.app.state.mock.snapshot()
    (app_rec,) = snap["applications"].values()
    assert f"application_id={app_rec['id']}" in loc
    cand = snap["candidates"][str(app_rec["candidate_id"])]
    assert (cand["first_name"], cand["last_name"]) == ("Ada", "Lovelace")
    assert cand["email_addresses"] == [{"type": "personal", "value": "ada@example.com"}]
    assert cand["website_addresses"] == [{"type": "other", "value": "https://github.com/ada-l"}]
    assert app_rec["jobs"][0]["name"] == "Software Engineer Intern"
    (att,) = snap["attachments"]
    assert att["type"] == "resume" and att["filename"] == "ada_lovelace_resume.pdf"
    assert client.app.state.mock.file_path(att["file_id"], att["filename"]).read_bytes() == PDF
    assert client.get(f"/files/{att['file_id']}/{att['filename']}").content == PDF

    (req,) = sent
    assert req.headers["Signature"] == "sha256 " + hmac.new(
        SECRET.encode(), req.content, hashlib.sha256).hexdigest()
    body = json.loads(req.content)
    assert body["action"] == "new_candidate_application"
    a = body["payload"]["application"]
    assert a["status"] == "active" and a["custom_fields"] == {}
    assert a["id"] == app_rec["id"] and a["source"] == app_rec["source"]
    c = a["candidate"]
    for key in ("phone_numbers", "social_media_addresses", "educations", "employments", "tags"):
        assert c[key] == []
    assert c["custom_fields"] == {}
    assert c["attachments"][0]["type"] == "resume"
    assert c["attachments"][0]["url"] == f"http://ats.test/files/{att['file_id']}/{att['filename']}"

    t = client.get(loc)
    assert t.status_code == 200
    assert "Application received" in t.text and "Ada Lovelace" in t.text
    assert "Software Engineer Intern" in t.text and "accepted" in t.text


def test_github_optional(client, sent):
    data = {k: v for k, v in GOOD.items() if k != "github_url"}
    assert post(client, data).status_code == 303
    (app_rec,) = applications(client).values()
    cand = client.app.state.mock.snapshot()["candidates"][str(app_rec["candidate_id"])]
    assert cand["website_addresses"] == []


def test_filename_sanitised(client):
    data = dict(GOOD, first_name="Zoë O'Neil", last_name="../../Evil Name")
    r = post(client, data, filename="../../etc/passwd.pdf")
    assert r.status_code == 303
    (att,) = client.app.state.mock.snapshot()["attachments"]
    assert att["filename"] == "zo_oneil_evil_name_resume.pdf"


@pytest.mark.parametrize("case", ["not_pdf", "oversize", "no_file", "empty_file", "no_first",
                                  "no_last", "no_email", "bad_email", "bad_github"])
def test_validation_errors(client, sent, case):
    data, content = dict(GOOD), PDF
    if case == "not_pdf":
        content = b"hello, not a pdf"
    elif case == "oversize":
        content = b"%PDF" + b"0" * mock_app.MAX_RESUME_BYTES
    elif case == "no_file":
        content = None
    elif case == "empty_file":
        content = b""
    elif case == "no_first":
        data["first_name"] = ""
    elif case == "no_last":
        data["last_name"] = "  "
    elif case == "no_email":
        data["email"] = ""
    elif case == "bad_email":
        data["email"] = "not-an-email"
    elif case == "bad_github":
        data["github_url"] = "https://github.com/ada/repo"
    r = post(client, data, content=content)
    assert r.status_code == 422
    assert 'class="ap-err"' in r.text
    assert "ada@example.com" in r.text or case in ("no_email", "bad_email")
    assert not applications(client) and not sent
    assert not client.app.state.mock.snapshot()["attachments"]


def test_non_github_hosts_rejected(client):
    for url in ("http://github.com/ada", "https://evil.com/github.com/ada",
                "https://github.com.evil.com/ada", "https://github.com/"):
        assert post(client, dict(GOOD, github_url=url)).status_code == 422


def test_hide_simulate_switch(client, monkeypatch):
    assert "Simulate application" in client.get("/").text
    for val in ("1", "true", "YES"):
        monkeypatch.setenv("MOCK_ATS_HIDE_SIMULATE", val)
        page = client.get("/").text
        assert "simulate" not in page.lower()
        assert 'href="/apply"' in page and "Open the careers page" in page
    monkeypatch.setenv("MOCK_ATS_HIDE_SIMULATE", "0")
    assert "Simulate application" in client.get("/").text
    # the canned route stays available
    monkeypatch.setenv("MOCK_ATS_HIDE_SIMULATE", "1")
    assert client.post("/_mock/apply", json={"fixture": "henry"}).status_code == 200


def test_no_page_says_greenhouse(client):
    pages = [client.get("/").text, client.get("/apply").text,
             post(client, dict(GOOD, email="")).text]
    r = post(client)
    pages.append(client.get(r.headers["location"]).text)
    for text in pages:
        assert "greenhouse" not in text.lower()
