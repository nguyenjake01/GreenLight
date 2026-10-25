"""Mock candidate page (J11): TestClient only."""
import base64
import html
import re
from urllib.parse import parse_qs, quote, unquote, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from verifier.ats import mock_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("ATS_PUBLIC_URL", "http://ats.test")
    monkeypatch.setenv("GREENHOUSE_WEBHOOK_SECRET", "s")
    app = mock_app.create_app(webhook_client=httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"status": "accepted"}))))
    return TestClient(app, base_url="http://ats.test")


def auth(client) -> dict:
    r = client.post("/token", auth=("mock", "mock"), data={"grant_type": "client_credentials"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def apply(client, fixture="henry") -> dict:
    return client.post("/_mock/apply", json={"fixture": fixture}).json()


def add_note(client, ids, body, visibility="private"):
    r = client.post("/v3/notes", headers=auth(client), json={
        "candidate_id": ids["candidate_id"], "application_id": ids["application_id"],
        "body": body, "note_type": "NOTE", "visibility": visibility})
    assert r.status_code == 201, r.text


def add_pdf(client, ids, filename, kind="other"):
    r = client.post("/v3/attachments", headers=auth(client), json={
        "application_id": ids["application_id"], "filename": filename, "type": kind,
        "content": base64.b64encode(b"%PDF-1.4 " + filename.encode()).decode()})
    assert r.status_code == 201, r.text


def page(client, ids, **params) -> str:
    q = {"application_id": ids["application_id"], **params}
    r = client.get(f"/candidates/{ids['candidate_id']}", params=q)
    assert r.status_code == 200, r.text
    return r.text


def viewer_file(text: str) -> str:
    """The decoded `file=` of the pdf.js iframe."""
    src = html.unescape(re.search(r'<iframe[^>]*\bsrc="([^"]+)"', text).group(1))
    assert src.startswith("/static/pdfjs/web/viewer.html?file=")
    return unquote(parse_qs(urlparse(src).query)["file"][0])


# ── header ────────────────────────────────────────────────────────────────────

def test_header_shows_name_and_application_and_never_claims_to_be_greenhouse(client):
    ids = apply(client)
    text = page(client, ids)
    assert "Mock ATS" in text
    assert "Henry Chen" in text
    assert "Software Engineer Intern" in text
    assert "Company careers site" in text
    assert str(ids["application_id"]) in text
    assert re.search(r"Applied: <b>[A-Z][a-z]{2} \d\d, \d{4} \d\d:\d\d UTC</b>", text)
    assert "greenhouse" not in text.lower()
    assert "Refresh" in text
    assert "http-equiv" not in text.lower()  # no auto-refresh


def test_application_id_is_optional(client):
    ids = apply(client)
    r = client.get(f"/candidates/{ids['candidate_id']}")
    assert r.status_code == 200 and "Henry Chen" in r.text


def test_unknown_candidate_or_mismatched_application_is_404(client):
    ids = apply(client)
    assert client.get("/candidates/999999").status_code == 404
    assert client.get(f"/candidates/{ids['candidate_id']}?application_id=999999").status_code == 404
    assert "greenhouse" not in client.get("/candidates/999999").text.lower()


# ── viewer ────────────────────────────────────────────────────────────────────

def test_viewer_points_at_the_original_resume_when_nothing_is_annotated(client):
    ids = apply(client)
    text = page(client, ids)
    path = viewer_file(text)
    assert path.startswith("/files/") and path.endswith("/henry.pdf")
    assert client.get(path).content.startswith(b"%PDF")


def test_viewer_points_at_the_newest_annotated_pdf(client):
    ids = apply(client)
    add_pdf(client, ids, "henry_chen_resume_verified_1.pdf")
    add_pdf(client, ids, "notes_from_someone.pdf")  # other, but not annotated
    add_pdf(client, ids, "henry_chen_resume_verified_2.pdf")
    add_pdf(client, ids, "latest_unrelated.pdf")
    assert viewer_file(page(client, ids)).endswith("/henry_chen_resume_verified_2.pdf")


def test_viewer_url_is_encoded_and_loads_the_file(client):
    ids = apply(client)
    add_pdf(client, ids, "Henry Chen & co_resume_verified_1.pdf")
    text = page(client, ids)
    path = viewer_file(text)
    assert "%20" in path or "+" not in path
    assert client.get(path).content == b"%PDF-1.4 Henry Chen & co_resume_verified_1.pdf"
    assert "file=%2Ffiles%2F" in html.unescape(text)  # the whole path is url-encoded


def test_a_resume_typed_upload_named_verified_is_not_treated_as_annotated(client):
    ids = apply(client)
    add_pdf(client, ids, "x_resume_verified_1.pdf", kind="resume")  # the D20 mistake
    assert viewer_file(page(client, ids)).endswith("/x_resume_verified_1.pdf")  # newest resume shown
    text = page(client, ids)
    assert "No resume on file" not in text


def test_pdfjs_is_vendored_and_served(client):
    for path, ctype in [("/static/pdfjs/web/viewer.html", "text/html"),
                        ("/static/pdfjs/web/viewer.mjs", "javascript"),
                        ("/static/pdfjs/build/pdf.mjs", "javascript"),
                        ("/static/pdfjs/build/pdf.worker.mjs", "javascript")]:
        r = client.get(path)
        assert r.status_code == 200, path
        assert ctype in r.headers["content-type"], path
    assert "greenhouse" not in client.get("/static/pdfjs/web/viewer.html").text.lower()


# ── attachments ───────────────────────────────────────────────────────────────

def test_attachments_original_first_then_annotated_newest_first(client):
    ids = apply(client)
    add_pdf(client, ids, "a_resume_verified_1.pdf")
    add_pdf(client, ids, "a_resume_verified_2.pdf")
    text = page(client, ids)
    section = text[text.index("<h2>Attachments</h2>"):text.index("<h2>Notes</h2>")]
    names = re.findall(r'<a href="[^"]*"[^>]*>([^<]+)</a>', section)
    assert names == ["henry.pdf", "a_resume_verified_2.pdf", "a_resume_verified_1.pdf"]
    assert ">resume</td>" in section and ">other</td>" in section
    assert re.search(r"[A-Z][a-z]{2} \d\d, \d{4} \d\d:\d\d UTC", section)  # upload time
    assert 'href="/files/' in section


# ── notes ─────────────────────────────────────────────────────────────────────

def test_notes_newest_first_with_badges_author_and_line_breaks(client):
    ids = apply(client)
    add_note(client, ids, "first note", "public")
    add_note(client, ids, "second note\nwith a second line", "private")
    add_note(client, ids, "third note", "admin_only")
    text = page(client, ids)
    assert text.index("third note") < text.index("second note") < text.index("first note")
    assert "second note<br>\nwith a second line" in text
    for badge in ("Public", "Private", "Admin only"):
        assert f'<span class="badge">{badge}</span>' in text
    assert text.count("Resume Verifier (integration)") == 3
    # badges line up with their notes: third is admin_only
    assert text.index("Admin only") < text.index("third note") < text.index("Private") \
        < text.index("second note") < text.index("Public") < text.index("first note")


def test_empty_notes_message(client):
    assert "No notes yet" in page(client, apply(client))


def test_a_script_tag_in_a_note_is_escaped(client):
    ids = apply(client)
    add_note(client, ids, "<script>alert(1)</script>\n<b>bold</b> & more")
    text = page(client, ids)
    assert "<script>alert(1)</script>" not in text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;<br>" in text
    assert "&lt;b&gt;bold&lt;/b&gt; &amp; more" in text


def test_notes_of_another_candidate_do_not_leak(client):
    a, b = apply(client), apply(client, "aidan")
    add_note(client, a, "henry only")
    text = page(client, b)
    assert "henry only" not in text and "Aidan Hong" in text


def test_page_never_shows_fields_the_real_ats_lacks(client):
    ids = apply(client)
    add_note(client, ids, "n")
    text = page(client, ids).lower()
    assert "score" not in text and "fraud" not in text and "tag" not in text.replace("stage", "")


# ── index and the form apply ──────────────────────────────────────────────────

def test_index_lists_applications_newest_first_and_a_button_per_fixture(client):
    first, second = apply(client), apply(client, "aidan")
    r = client.get("/")
    assert r.status_code == 200
    text = r.text
    assert text.index("Aidan Hong") < text.index("Henry Chen")  # newest first
    assert (f'href="/candidates/{second["candidate_id"]}?application_id={second["application_id"]}"'
            in text)
    assert (f'href="/candidates/{first["candidate_id"]}?application_id={first["application_id"]}"'
            in text)
    assert re.search(r'<form[^>]*method="post"[^>]*action="/_mock/apply"', text)
    assert 'name="fixture" value="henry"' in text and 'name="fixture" value="aidan"' in text
    assert text.count("Simulate application") >= 3  # heading + one button per fixture
    assert "greenhouse" not in text.lower()


def test_empty_index(client):
    assert "No applications yet" in client.get("/").text


def test_form_apply_redirects_back_to_the_index(client):
    r = client.post("/_mock/apply", data={"fixture": "henry"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert "Henry Chen" in client.get("/").text  # the application was created


def test_form_apply_with_an_unknown_fixture_is_404(client):
    assert client.post("/_mock/apply", data={"fixture": "nope"},
                       follow_redirects=False).status_code == 404
