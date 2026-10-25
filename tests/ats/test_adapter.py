"""Greenhouse adapter (J3): signatures, idempotency, normalization, resume download, write-back."""
import base64
import hashlib
import hmac
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from verifier.ats import mock_app
from verifier.ats.adapter import GreenhouseAdapter, normalize_application
from verifier.contracts.errors import HarvestError, ResumeError, SignatureError
from verifier.core import store

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
SECRET = "adapter-secret"
BASE = "http://ats.test"


@pytest.fixture
def runs(tmp_path, monkeypatch):
    path = tmp_path / "runs"
    for k, v in {"RUNS_DIR": str(path), "ATS_PUBLIC_URL": BASE,
                 "GREENHOUSE_WEBHOOK_SECRET": SECRET, "HARVEST_CLIENT_ID": "mock",
                 "HARVEST_CLIENT_SECRET": "mock"}.items():
        monkeypatch.setenv(k, v)
    return path


def make_adapter(client=None, **kw) -> GreenhouseAdapter:
    return GreenhouseAdapter(
        webhook_secret=SECRET, harvest_auth_url=f"{BASE}/token", harvest_base_url=f"{BASE}/v3",
        client_id="mock", client_secret="mock", client=client, **kw)


def webhook(name="henry", mutate=None) -> tuple[bytes, dict]:
    body = json.loads((FIXTURES / "webhooks" / f"{name}.json").read_text())
    if mutate:
        mutate(body)
    raw = json.dumps(body).encode()
    sig = "sha256 " + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {"Signature": sig, "Greenhouse-Event-ID": "evt-1"}


# ── signatures ────────────────────────────────────────────────────────────────

def test_good_signature_is_accepted_and_saved(runs):
    adapter, seen = make_adapter(), []
    adapter.on_new_application(seen.append)
    raw, headers = webhook()
    assert adapter.handle_webhook(raw, headers) == "accepted"
    assert len(seen) == 1 and seen[0]["application_id"] == "1000001"
    rd = runs / "1000001"
    assert (rd / "webhook.json").read_bytes() == raw  # byte for byte
    assert store.read_json(rd / "webhook_headers.json") == {
        "event_id": "evt-1", "signature": headers["Signature"]}


def test_header_names_match_case_insensitively(runs):
    raw, headers = webhook()
    lowered = {k.lower(): v for k, v in headers.items()}
    assert make_adapter().handle_webhook(raw, lowered) == "accepted"


def test_bad_signature_raises(runs):
    raw, headers = webhook()
    bad = {**headers, "Signature": "sha256 " + "0" * 64}
    with pytest.raises(SignatureError):
        make_adapter().handle_webhook(raw, bad)
    assert not (runs / "1000001").exists()


def test_tampered_body_raises(runs):
    raw, headers = webhook()
    with pytest.raises(SignatureError):
        make_adapter().handle_webhook(raw.replace(b"Henry", b"Hemry"), headers)


def test_signature_is_over_the_raw_bytes_not_a_reserialization(runs):
    raw, headers = webhook()
    spaced = json.dumps(json.loads(raw), indent=1).encode()  # same JSON, different bytes
    with pytest.raises(SignatureError):
        make_adapter().handle_webhook(spaced, headers)


@pytest.mark.parametrize("header", [None, "", "sha256", "sha1 abc", "abc", "sha256 abc def"])
def test_missing_or_malformed_signature_raises(runs, header):
    raw, headers = webhook()
    headers = {k: v for k, v in headers.items() if k != "Signature"}
    if header is not None:
        headers["Signature"] = header
    with pytest.raises(SignatureError):
        make_adapter().handle_webhook(raw, headers)


def test_wrong_secret_raises(runs):
    raw, headers = webhook()
    other = GreenhouseAdapter(webhook_secret="nope", harvest_auth_url="", harvest_base_url="",
                              client_id="", client_secret="")
    with pytest.raises(SignatureError):
        other.handle_webhook(raw, headers)


# ── ping, duplicates, idempotency ─────────────────────────────────────────────

def test_ping_and_other_actions_are_ignored(runs):
    adapter, seen = make_adapter(), []
    adapter.on_new_application(seen.append)
    for action in ("ping", "hire_candidate"):
        raw = json.dumps({"action": action, "payload": {}}).encode()
        sig = "sha256 " + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
        assert adapter.handle_webhook(raw, {"Signature": sig}) == "ignored"
    assert seen == []
    assert not (runs / "_events").exists()


def test_ping_still_needs_a_valid_signature(runs):
    with pytest.raises(SignatureError):
        make_adapter().handle_webhook(b'{"action": "ping"}', {"Signature": "sha256 00"})


def test_duplicate_event_id(runs):
    adapter, seen = make_adapter(), []
    adapter.on_new_application(seen.append)
    raw, headers = webhook()
    assert adapter.handle_webhook(raw, headers) == "accepted"
    assert adapter.handle_webhook(raw, headers) == "duplicate"
    assert len(seen) == 1
    assert (runs / "_events" / "evt-1").is_file()


def test_duplicate_is_remembered_across_adapter_instances(runs):
    raw, headers = webhook()
    assert make_adapter().handle_webhook(raw, headers) == "accepted"
    assert make_adapter().handle_webhook(raw, headers) == "duplicate"


def test_missing_event_id_falls_back_to_the_body_hash(runs):
    raw, headers = webhook()
    headers.pop("Greenhouse-Event-ID")
    adapter = make_adapter()
    assert adapter.handle_webhook(raw, headers) == "accepted"
    assert adapter.handle_webhook(raw, headers) == "duplicate"
    digest = hashlib.sha256(raw).hexdigest()
    assert (runs / "_events" / digest).is_file()
    assert store.read_json(runs / "1000001" / "webhook_headers.json")["event_id"] == digest


def test_odd_event_ids_cannot_escape_the_events_folder(runs):
    raw, headers = webhook()
    headers["Greenhouse-Event-ID"] = "../../evil"
    assert make_adapter().handle_webhook(raw, headers) == "accepted"
    assert not (runs.parent / "evil").exists()
    assert len(list((runs / "_events").iterdir())) == 1


def test_signed_garbage_is_a_value_error_and_leaves_no_marker(runs):
    raw = b"not json"
    sig = "sha256 " + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    with pytest.raises(ValueError):
        make_adapter().handle_webhook(raw, {"Signature": sig, "Greenhouse-Event-ID": "e"})
    raw = json.dumps({"action": "new_candidate_application", "payload": {}}).encode()
    sig = "sha256 " + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    with pytest.raises(ValueError):
        make_adapter().handle_webhook(raw, {"Signature": sig, "Greenhouse-Event-ID": "e"})
    assert not (runs / "_events").exists()


# ── normalization ─────────────────────────────────────────────────────────────

def test_normalization_of_the_henry_fixture(runs):
    seen = []
    adapter = make_adapter()
    adapter.on_new_application(seen.append)
    raw, headers = webhook()
    adapter.handle_webhook(raw, headers)
    assert seen[0] == {
        "application_id": "1000001", "candidate_id": "2000001", "candidate_name": "Henry Chen",
        "emails": ["henry@example.com"], "website_addresses": ["https://henrychen4736.github.io"],
        "social_media_addresses": [], "job_title": "Software Engineer Intern",
        "applied_at": "2026-10-25T14:01:50Z",
        "profile_url": "http://localhost:8100/candidates/2000001?application_id=1000001",
        "resume": {"filename": "henry.pdf", "type": "resume",
                   "url": "http://localhost:8100/files/placeholder/henry.pdf"},
        "event_id": "evt-1"}
    assert all(isinstance(seen[0][k], str) for k in ("application_id", "candidate_id"))


def test_normalization_without_resume_or_jobs(runs):
    def strip(body):
        a = body["payload"]["application"]
        a["candidate"]["attachments"] = [{"filename": "c.pdf", "url": "u", "type": "cover_letter"}]
        a["jobs"] = []
        a["candidate"]["social_media_addresses"] = [{"value": "https://x.com/h", "type": "twitter"}]

    raw, _ = webhook(mutate=strip)
    app = normalize_application(json.loads(raw), "e")
    assert app["resume"] is None and app["job_title"] is None
    assert app["social_media_addresses"] == ["https://x.com/h"]


def test_normalization_picks_the_first_resume(runs):
    def two(body):
        body["payload"]["application"]["candidate"]["attachments"] = [
            {"filename": "a.pdf", "url": "u1", "type": "other"},
            {"filename": "b.pdf", "url": "u2", "type": "resume"},
            {"filename": "c.pdf", "url": "u3", "type": "resume"}]

    raw, _ = webhook(mutate=two)
    assert normalize_application(json.loads(raw), "e")["resume"]["filename"] == "b.pdf"


def test_get_application_is_rebuilt_from_the_saved_files(runs):
    seen = []
    adapter = make_adapter()
    adapter.on_new_application(seen.append)
    raw, headers = webhook()
    adapter.handle_webhook(raw, headers)
    assert make_adapter().get_application("1000001") == seen[0]


# ── resume download ───────────────────────────────────────────────────────────

@pytest.fixture
def ats(runs):
    """A real mock ATS in process, plus a client for the adapter (and a webhook sink)."""
    app = mock_app.create_app(webhook_client=httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"status": "accepted"}))))
    return TestClient(app, base_url=BASE)


def apply_henry(ats) -> dict:
    return ats.post("/_mock/apply", json={"fixture": "henry"}).json()


def application_for(ats, adapter, result) -> dict:
    """Run the mock's own webhook body through the adapter, as the service would."""
    state = ats.app.state.mock.snapshot()
    cid, aid = str(result["candidate_id"]), str(result["application_id"])
    cand = state["candidates"][cid]
    att = state["attachments"][0]
    body = {"action": "new_candidate_application", "payload": {"application": {
        "id": result["application_id"], "applied_at": "x", "url": "u",
        "candidate": {**cand, "attachments": [
            {"filename": att["filename"], "type": "resume",
             "url": f"{BASE}/files/{att['file_id']}/{att['filename']}"}]},
        "jobs": state["applications"][aid]["jobs"]}}}
    return normalize_application(body, result["event_id"])


def test_fetch_resume_downloads_through_store(ats, runs):
    result = apply_henry(ats)
    adapter = make_adapter(ats)
    app = application_for(ats, adapter, result)
    dest = runs / str(result["application_id"]) / "resume.pdf"
    assert adapter.fetch_resume(app, dest) == dest
    assert dest.read_bytes() == (FIXTURES / "resumes" / "henry.pdf").read_bytes()
    assert adapter.fetch_resume(app, dest) == dest  # same bytes again is fine


def test_fetch_resume_without_a_resume(runs):
    with pytest.raises(ResumeError):
        make_adapter().fetch_resume({"resume": None}, runs / "r.pdf")  # type: ignore[arg-type]


def test_fetch_resume_http_failure(runs):
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    app = {"resume": {"filename": "x.pdf", "url": f"{BASE}/files/x/x.pdf", "type": "resume"}}
    with pytest.raises(ResumeError, match="404"):
        make_adapter(client).fetch_resume(app, runs / "r.pdf")  # type: ignore[arg-type]
    assert not (runs / "r.pdf").exists()


def test_fetch_resume_network_failure(runs):
    def boom(request):
        raise httpx.ConnectError("down")

    client = httpx.Client(transport=httpx.MockTransport(boom))
    app = {"resume": {"filename": "x.pdf", "url": f"{BASE}/files/x/x.pdf", "type": "resume"}}
    with pytest.raises(ResumeError, match="down"):
        make_adapter(client).fetch_resume(app, runs / "r.pdf")  # type: ignore[arg-type]


# ── write-back against the real mock ──────────────────────────────────────────

def test_write_back_lands_a_private_note_and_an_other_attachment(ats, runs, tmp_path):
    result = apply_henry(ats)
    adapter = make_adapter(ats)
    app = application_for(ats, adapter, result)
    pdf = tmp_path / "henry_chen_resume_verified_1.pdf"
    pdf.write_bytes(b"%PDF-1.4 annotated")
    out = adapter.write_back(app, "Verified.\nSecond line.", pdf)

    state = ats.app.state.mock.snapshot()
    (note,) = state["notes"]
    assert note["visibility"] == "private" and note["note_type"] == "NOTE"
    assert note["body"] == "Verified.\nSecond line."
    assert note["candidate_id"] == result["candidate_id"]
    assert note["application_id"] == result["application_id"]
    new_att = state["attachments"][-1]
    assert new_att["type"] == "other" and new_att["filename"] == pdf.name
    assert state["attachments"][0]["type"] == "resume"  # the real resume was not demoted

    assert out["note_id"] == str(note["id"]) and out["attachment_id"] == str(new_att["id"])
    assert out["attachment_url"].startswith(f"{BASE}/files/")
    assert ats.get(out["attachment_url"].removeprefix(BASE)).content == b"%PDF-1.4 annotated"


def test_write_back_note_visibility_comes_from_config(ats, runs, tmp_path):
    result = apply_henry(ats)
    adapter = make_adapter(ats, note_visibility="admin_only")
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"x")
    adapter.write_back(application_for(ats, adapter, result), "n", pdf)
    assert ats.app.state.mock.snapshot()["notes"][0]["visibility"] == "admin_only"


# ── write-back against a scripted Harvest ─────────────────────────────────────

APP = {"application_id": "1000001", "candidate_id": "2000001"}


class Harvest:
    """A scripted Harvest: records requests; `script` maps path -> list of responses."""

    def __init__(self, script=None):
        self.requests: list[httpx.Request] = []
        self.script = script or {}
        self.tokens = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        queue = self.script.get(path)
        if queue:
            return queue.pop(0)
        if path == "/token":
            self.tokens += 1
            return httpx.Response(200, json={"token_type": "Bearer", "expires_in": 3600,
                                             "access_token": f"tok{self.tokens}"})
        n = len([r for r in self.requests if r.url.path == path])
        return httpx.Response(201, json={"id": 100 + n, "url": f"{BASE}/files/f1/x.pdf"})

    def calls(self, path):
        return [r for r in self.requests if r.url.path == path]


@pytest.fixture
def pdf(tmp_path):
    p = tmp_path / "out_resume_verified_2.pdf"
    p.write_bytes(b"%PDF annotated")
    return p


def scripted(script=None):
    h = Harvest(script)
    return make_adapter(httpx.Client(transport=httpx.MockTransport(h))), h


def test_request_bodies_use_integer_ids_and_the_right_shapes(pdf):
    adapter, h = scripted()
    out = adapter.write_back(APP, "the note", pdf)  # type: ignore[arg-type]
    note = json.loads(h.calls("/v3/notes")[0].content)
    assert note == {"candidate_id": 2000001, "application_id": 1000001, "body": "the note",
                    "note_type": "NOTE", "visibility": "private"}
    att = json.loads(h.calls("/v3/attachments")[0].content)
    assert att["application_id"] == 1000001 and isinstance(att["application_id"], int)
    assert att["filename"] == pdf.name and att["type"] == "other" and "url" not in att
    assert base64.b64decode(att["content"]) == b"%PDF annotated"
    assert out == {"note_id": "101", "attachment_id": "101", "attachment_url": f"{BASE}/files/f1/x.pdf"}
    assert all(isinstance(out[k], str) for k in out)


def test_token_request_is_basic_auth_client_credentials(pdf):
    adapter, h = scripted()
    adapter.write_back(APP, "n", pdf)  # type: ignore[arg-type]
    tok = h.calls("/token")[0]
    assert tok.headers["authorization"] == "Basic " + base64.b64encode(b"mock:mock").decode()
    assert tok.content == b"grant_type=client_credentials"
    for r in h.calls("/v3/notes") + h.calls("/v3/attachments"):
        assert r.headers["authorization"] == "Bearer tok1"


def test_token_is_cached_across_write_backs(pdf):
    adapter, h = scripted()
    adapter.write_back(APP, "n", pdf)  # type: ignore[arg-type]
    adapter.write_back(APP, "n", pdf)  # type: ignore[arg-type]
    assert len(h.calls("/token")) == 1
    assert len(h.calls("/v3/notes")) == 2


def test_token_is_renewed_60_seconds_before_it_expires(pdf, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("verifier.ats.adapter.time.monotonic", lambda: clock[0])
    adapter, h = scripted()
    adapter.write_back(APP, "n", pdf)  # type: ignore[arg-type]
    clock[0] += 3600 - 61
    adapter.write_back(APP, "n", pdf)  # type: ignore[arg-type]
    assert len(h.calls("/token")) == 1
    clock[0] += 2
    adapter.write_back(APP, "n", pdf)  # type: ignore[arg-type]
    assert len(h.calls("/token")) == 2


def test_a_401_gets_a_new_token_and_retries_that_call_once(pdf):
    adapter, h = scripted({"/v3/notes": [httpx.Response(401, json={"error": "unauthorized"})]})
    out = adapter.write_back(APP, "n", pdf)  # type: ignore[arg-type]
    notes = h.calls("/v3/notes")
    assert [r.headers["authorization"] for r in notes] == ["Bearer tok1", "Bearer tok2"]
    assert len(h.calls("/token")) == 2
    assert len(h.calls("/v3/attachments")) == 1
    assert out["note_id"]


def test_a_second_401_is_a_harvest_error(pdf):
    adapter, h = scripted({"/v3/notes": [httpx.Response(401, text="no"), httpx.Response(401, text="no")]})
    with pytest.raises(HarvestError, match="401"):
        adapter.write_back(APP, "n", pdf)  # type: ignore[arg-type]
    assert len(h.calls("/v3/notes")) == 2


def test_non_201_is_a_harvest_error_with_a_truncated_body(pdf):
    adapter, h = scripted({"/v3/notes": [httpx.Response(500, text="x" * 1000)]})
    with pytest.raises(HarvestError) as e:
        adapter.write_back(APP, "n", pdf)  # type: ignore[arg-type]
    assert "500" in str(e.value) and "x" * 300 in str(e.value) and "x" * 301 not in str(e.value)
    assert len(h.calls("/v3/notes")) == 1  # never retried
    assert h.calls("/v3/attachments") == []


def test_200_is_not_201(pdf):
    adapter, _ = scripted({"/v3/notes": [httpx.Response(200, json={"id": 1})]})
    with pytest.raises(HarvestError, match="200"):
        adapter.write_back(APP, "n", pdf)  # type: ignore[arg-type]


def test_attachment_failure_does_not_repeat_the_note(pdf):
    adapter, h = scripted({"/v3/attachments": [httpx.Response(422, text="bad")]})
    with pytest.raises(HarvestError, match="422"):
        adapter.write_back(APP, "n", pdf)  # type: ignore[arg-type]
    assert len(h.calls("/v3/notes")) == 1 and len(h.calls("/v3/attachments")) == 1


def test_token_failure_is_a_harvest_error(pdf):
    adapter, _ = scripted({"/token": [httpx.Response(401, json={"error": "invalid_client"})]})
    with pytest.raises(HarvestError, match="token"):
        adapter.write_back(APP, "n", pdf)  # type: ignore[arg-type]


def test_network_failure_is_a_harvest_error(pdf):
    def boom(request):
        raise httpx.ConnectError("down")

    adapter = make_adapter(httpx.Client(transport=httpx.MockTransport(boom)))
    with pytest.raises(HarvestError):
        adapter.write_back(APP, "n", pdf)  # type: ignore[arg-type]


def test_outgoing_calls_are_logged(pdf, caplog):
    adapter, _ = scripted()
    with caplog.at_level("INFO", logger="verifier.ats.adapter"):
        adapter.write_back(APP, "n", pdf)  # type: ignore[arg-type]
    lines = [r.getMessage() for r in caplog.records]
    assert any(l.startswith("POST /token -> 200 (") and l.endswith(" ms)") for l in lines)
    assert any(l.startswith("POST /v3/notes -> 201") for l in lines)
    assert any(l.startswith("POST /v3/attachments -> 201") for l in lines)
