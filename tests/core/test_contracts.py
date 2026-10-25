"""Contracts tests: config, cache_key, registry switching, and every stub returning data that
validates against its schema (design doc §8)."""
from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from verifier import demo
from verifier.contracts import interfaces, registry, stubs
from verifier.contracts.config import DEFAULTS, env
from verifier.contracts.errors import LLMSchemaError
from verifier.contracts.snapshot import Snapshot, cache_key
from verifier.core import store

FIXTURES = stubs.FIXTURES
SLACK_KEYS = {"seq", "call", "channel", "ts", "thread_ts", "text", "n_blocks", "path"}


@pytest.fixture(autouse=True)
def runs_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.delenv("VERIFIER_REAL", raising=False)
    return tmp_path / "runs"


def _pdf(name="henry.pdf") -> Path:
    return FIXTURES / "resumes" / name


def _henry_app(**over):
    app = {"application_id": "1", "candidate_id": "2", "candidate_name": "Henry Chen",
           "emails": ["h@example.com"], "website_addresses": [], "social_media_addresses": [],
           "job_title": "Software Engineer Intern", "applied_at": "2026-10-25T14:01:50Z",
           "profile_url": "http://localhost:8100/candidates/2",
           "resume": {"filename": "henry.pdf", "url": "http://x/henry.pdf", "type": "resume"},
           "event_id": "evt-1"}
    app.update(over)
    return app


def _henry_claims():
    app = _henry_app()
    cand = stubs.resume.link_github(stubs.resume.read_resume(_pdf()), app)
    rt = stubs.resume.read_resume(_pdf())
    return stubs.extract.extract_claims(rt, cand, application_id="1")


# ── config ────────────────────────────────────────────────────────
def test_env_default_override_and_empty(monkeypatch):
    monkeypatch.delenv("LLM_MODEL", raising=False)
    assert env("LLM_MODEL") == DEFAULTS["LLM_MODEL"] == "Qwen3.6-35B-A3B-NVFP4"
    monkeypatch.setenv("LLM_MODEL", "other")
    assert env("LLM_MODEL") == "other"
    monkeypatch.setenv("LLM_MODEL", "")
    assert env("LLM_MODEL") == "Qwen3.6-35B-A3B-NVFP4"
    with pytest.raises(KeyError):
        env("NOT_A_VARIABLE")


# ── snapshot / cache_key ──────────────────────────────────────────
def test_cache_key_normalizes():
    assert cache_key("get", "HTTPS://API.GitHub.com/repos/a/b?per_page=100&page=2") == \
        "GET https://api.github.com/repos/a/b?page=2&per_page=100"
    assert cache_key("GET", "https://api.github.com/search?q=a+b") == \
        cache_key("GET", "https://api.github.com/search?q=a%20b")
    assert cache_key("GET", "https://api.github.com/repos/a/b") == \
        "GET https://api.github.com/repos/a/b"


def test_snapshot_load_get_and_misses(tmp_path):
    body = b'{"x": 1}'
    sha = hashlib.sha256(body).hexdigest()
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw" / f"{sha}.json").write_bytes(body)
    key = cache_key("GET", "https://api.github.com/users/a")
    ev = {"schema_version": "1", "snapshot_id": "snap_20261025T140215Z", "application_id": "1",
          "github_login": "a", "fetch_mode": "replay", "created_at": "2026-10-25T14:02:15Z",
          "responses": [{"key": key, "status": 200, "fetched_at": "2026-10-25T14:02:15Z",
                         "etag": None, "body_sha256": sha, "body_path": f"raw/{sha}.json"}],
          "misses": ["GET https://api.github.com/users/b"], "decisions": []}
    (tmp_path / "evidence.json").write_text(json.dumps(ev))
    snap = Snapshot.load(tmp_path)
    assert snap.snapshot_id == "snap_20261025T140215Z"
    assert snap.get("GET", "https://API.github.com/users/a") == (200, {"x": 1})
    assert snap.get("GET", "https://api.github.com/users/b") is None
    assert snap.get_raw(f"raw/{sha}.json") == body
    assert snap.traces_dir() == tmp_path / "run_traces"


# ── registry ──────────────────────────────────────────────────────
def test_registry_defaults_to_stubs():
    for name in registry.NAMES:
        assert registry.get(name) is getattr(stubs, name)
    assert registry.real_modules() == []
    with pytest.raises(ValueError):
        registry.get("store")


def test_registry_switching(monkeypatch):
    imported = []
    monkeypatch.setattr(registry.importlib, "import_module",
                        lambda path: imported.append(path) or SimpleNamespace(path=path))
    monkeypatch.setenv("VERIFIER_REAL", "fetch, rubric")
    assert registry.get("fetch").path == "verifier.core.fetch"
    assert registry.get("rubric").path == "verifier.core.rubric"
    assert registry.get("llm") is stubs.llm            # not listed: still the stub
    assert registry.real_modules() == ["fetch", "rubric"]
    monkeypatch.setenv("VERIFIER_REAL", "")
    assert registry.get("fetch") is stubs.fetch        # read on every call
    monkeypatch.setenv("VERIFIER_REAL", "all")
    assert registry.real_modules() == list(registry.NAMES)
    paths = {n: registry.get(n).path for n in registry.NAMES}
    assert paths["llm"] == "verifier.stack.llm" and paths["judge"] == "verifier.stack.judge"
    assert paths["annotate"] == "verifier.core.annotate" and paths["ats"] == "verifier.ats.adapter"
    assert paths["sandbox"] == "verifier.stack.sandbox_openshell"
    monkeypatch.setenv("SANDBOX_BACKEND", "docker")
    assert registry.get("sandbox").path == "verifier.core.harness"


def test_missing_real_module_does_not_break_stub_run(monkeypatch):
    monkeypatch.setenv("VERIFIER_REAL", "rubric")
    assert registry.get("fetch") is stubs.fetch        # other names never import anything


def test_stub_signatures_match_interfaces():
    for name in registry.NAMES:
        ns = getattr(stubs, name)
        for attr in vars(ns):
            if attr.startswith("_"):
                continue
            iface = getattr(interfaces, attr)
            impl = getattr(ns, attr)
            if inspect.isclass(iface):
                for meth in ("on_new_application", "handle_webhook", "get_application",
                             "fetch_resume", "write_back"):
                    assert list(inspect.signature(getattr(impl, meth)).parameters) == \
                        list(inspect.signature(getattr(iface, meth)).parameters), meth
                assert list(inspect.signature(impl.__init__).parameters) == \
                    list(inspect.signature(iface.__init__).parameters)
            else:
                got = inspect.signature(impl).parameters
                want = inspect.signature(iface).parameters
                assert [(p.name, p.kind) for p in got.values()] == \
                    [(p.name, p.kind) for p in want.values()], attr


# ── stubs: llm / extract / resume ─────────────────────────────────
def test_llm_stub():
    assert stubs.llm.llm_chat([])["text"] == "(stub reply)"
    r = stubs.llm.llm_chat([], {"title": "repo_match"})
    assert r["json"] == r["json"] and r["json"] == {"repo": "none"}
    assert json.loads(r["text"]) == r["json"]
    with pytest.raises(LLMSchemaError):
        stubs.llm.llm_chat([], {"title": "no_such_schema"})


def test_resume_stub_by_sha_and_fallback(tmp_path):
    rt = stubs.resume.read_resume(_pdf())
    assert rt["sha256"] == hashlib.sha256(_pdf().read_bytes()).hexdigest()
    unknown = tmp_path / "other.pdf"
    unknown.write_bytes(b"%PDF-unknown")
    assert stubs.resume.read_resume(unknown) == json.loads(
        (FIXTURES / "resumes" / "henry.resume.json").read_text())
    if (FIXTURES / "resumes" / "aidan.resume.json").exists():
        aidan = stubs.resume.read_resume(_pdf("aidan.pdf"))
        assert aidan["sha256"] == hashlib.sha256(_pdf("aidan.pdf").read_bytes()).hexdigest()


def test_link_github_stub_uses_resume_filename_not_app_id():
    cand = stubs.resume.link_github({}, _henry_app(application_id="999999"))
    assert cand["github_login"] == "henrychen4736"
    assert cand["github_login_source"] == "resume_link"
    none = stubs.resume.link_github({}, _henry_app(resume=None))
    assert none["github_login"] is None and none["github_login_source"] == "none"


def test_extract_stub_validates():
    claims = _henry_claims()
    store.validate(claims, "claims")
    assert claims["application_id"] == "1"
    assert claims["candidate"]["github_login"] == "henrychen4736"
    # unknown login falls back to Henry's claims
    cand = dict(claims["candidate"], github_login="nobody-here")
    again = stubs.extract.extract_claims({}, cand, application_id="7")
    assert again["application_id"] == "7" and again["candidate"] == cand
    store.validate(again, "claims")


# ── stubs: fetch / rubric / harness / render / annotate ───────────
def test_fetch_rubric_harness_chain(tmp_path):
    claims = _henry_claims()
    rd = store.run_dir("1")
    snap = stubs.fetch.fetch_evidence(claims, run_dir=rd, mode="replay")
    store.validate(snap.evidence, "evidence")
    assert snap.evidence["snapshot_id"] == snap.snapshot_id and snap.path.parent == rd / "snapshots"
    assert snap.evidence["application_id"] == "1"
    snap2 = stubs.fetch.fetch_evidence(claims, run_dir=rd, mode="replay")
    assert snap2.snapshot_id != snap.snapshot_id          # fresh dir each time

    v = stubs.rubric.compute_verdict(claims, snap, [], verdict_n=3, computed_at="2026-10-25T14:06:40Z")
    store.validate(v, "verdict")
    assert v["verdict_id"] == "verdict_3" and v["application_id"] == "1"
    assert v["inputs"]["snapshot_id"] == snap.snapshot_id
    assert v["inputs"]["claims_sha256"] == store.sha256_json(claims)
    assert [c["claim_id"] for c in v["claims"]] == [c["claim_id"] for c in claims["claims"]]
    assert sum(v["tally"].values()) == len(claims["claims"])

    project = next(c for c in claims["claims"] if c["type"] == "project")
    trace = stubs.harness.run_check(snap, project, "henrychen4736/x", mode="replay")
    store.validate(trace, "run_trace")
    assert (snap.traces_dir() / f"{trace['claim_id']}.json").is_file()
    v2 = stubs.rubric.compute_verdict(claims, snap, [trace], verdict_n=4, computed_at="x")
    assert v2["inputs"]["run_traces"] == [
        {"claim_id": trace["claim_id"], "sha256": store.sha256_json(trace)}]

    # a claim without a trace fixture gets a skip baseline
    other = {"claim_id": "c999", "type": "project", "section": None, "source_text": "s",
             "needs_review": False, "assertions": {}}
    t2 = stubs.harness.run_check(snap, other, "a/b", mode="replay")
    store.validate(t2, "run_trace")
    assert t2["baseline"]["step"] == "skip" and t2["agent"] is None


def test_rubric_stub_without_fixture_is_all_no_evidence():
    claims = _henry_claims()
    claims["candidate"]["github_login"] = None
    snap = stubs.fetch.fetch_evidence(claims, run_dir=store.run_dir("1"), mode="replay")
    v = stubs.rubric.compute_verdict(claims, snap, [], verdict_n=1, computed_at="t")
    store.validate(v, "verdict")
    assert {c["label"] for c in v["claims"]} == {"no_evidence"}
    assert v["claims"][0]["top_reason"] == "(stub) not checked"
    assert v["tally"]["no_evidence"] == len(claims["claims"])


def test_render_stubs_shape():
    app, claims = _henry_app(), _henry_claims()
    snap = stubs.fetch.fetch_evidence(claims, run_dir=store.run_dir("1"), mode="replay")
    v = stubs.rubric.compute_verdict(claims, snap, [], verdict_n=1, computed_at="t")
    meta = {"application_id": "1"}
    opts = {"profile_url": "u", "annotated_pdf_url": None, "action_buttons": True, "menu": []}
    msgs = [
        stubs.render.render_placeholder(app),
        stubs.render.render_card(app, claims, v, meta, opts),
        stubs.render.render_card(app, claims, None, meta, opts),
        stubs.render.render_run_update("1", claims["claims"][0], "started", None),
        stubs.render.render_label_change(claims["claims"][0], [], "partial", "supported", "me"),
        stubs.render.render_generated(claims["claims"][0], {"text": "hello"}),
    ]
    for m in msgs:
        assert m["text"] and 1 <= len(m["blocks"]) <= 50
        assert all(b["type"] in ("header", "section") for b in m["blocks"])
    assert "Could not read claims" in msgs[2]["text"]
    modal = stubs.render.render_edit_modal("1", 1, claims["claims"][0])
    assert modal["type"] == "modal" and modal["callback_id"] == "openclaw:rv:edit_claim"
    assert json.loads(modal["private_metadata"])["claims_version"] == 1
    note = stubs.render.render_note(app, claims, v, None)
    assert isinstance(note, str) and "supported" in note


def test_annotate_stub(tmp_path):
    claims = _henry_claims()
    out = tmp_path / "annotated" / "x_resume_verified_1.pdf"
    rep = stubs.annotate.annotate_pdf(_pdf(), claims, {}, out)
    assert out.read_bytes() == _pdf().read_bytes()
    assert rep["out_path"] == str(out) and rep["located"] == []
    assert rep["not_located"] == [c["claim_id"] for c in claims["claims"]]


# ── stubs: slack ──────────────────────────────────────────────────
def test_slack_stub_log_shape(runs_dir, tmp_path):
    msg = {"text": "hi", "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}]}
    card = stubs.slack.post_card("C1", msg)
    stubs.slack.update_card(card, msg)
    line = stubs.slack.post_thread(card, msg)
    up = stubs.slack.upload_file(card, tmp_path / "f.pdf", title="f.pdf", comment="c")
    assert stubs.slack.get_permalink(card).startswith("https://")
    stubs.slack.open_modal("trig", {"callback_id": "cb", "blocks": []})
    assert card["ts"] != line["ts"] and up["permalink"] == f"file://{tmp_path / 'f.pdf'}"
    lines = [json.loads(l) for l in (runs_dir / "_slack_stub.jsonl").read_text().splitlines()]
    assert [l["seq"] for l in lines] == list(range(1, 7))
    assert [l["call"] for l in lines] == ["post_card", "update_card", "post_thread",
                                          "upload_file", "get_permalink", "open_modal"]
    assert all(set(l) == SLACK_KEYS for l in lines)
    assert lines[1]["ts"] == card["ts"] and lines[2]["thread_ts"] == card["ts"]
    assert lines[3]["thread_ts"] == card["ts"] and lines[3]["path"] == str(tmp_path / "f.pdf")
    assert lines[0]["n_blocks"] == 1 and lines[0]["text"] == "hi"
    assert json.loads((runs_dir / "_slack_last_card.json").read_text())["blocks"] == msg["blocks"]


# ── stubs: sandbox / agent / judge ────────────────────────────────
def test_sandbox_agent_judge_stubs(tmp_path):
    seen = []
    with stubs.sandbox.open_sandbox(tmp_path) as sb:
        orig = sb.exec
        sb.exec = lambda cmd, t: seen.append(cmd) or orig(cmd, t)
        r = sb.exec("echo hi", 5)
        assert (r["exit_code"], r["stdout"], r["timed_out"]) == (0, b"(stub) echo hi\n", False)
        assert sb.backend == "stub"
        res = stubs.agent.agent_probe(sb, {}, None)
    assert res["stopped"] == "done" and seen == ["echo hi", "ls -la", "echo stub-run"]
    j = stubs.judge.judge({}, [])
    assert j["outcome"] == "works_as_claimed" and j["rationale"] == "(stub) not judged"


# ── stubs: ats ────────────────────────────────────────────────────
def test_ats_stub_webhook_resume_writeback(tmp_path):
    body = json.loads((FIXTURES / "webhooks" / "henry.json").read_text())
    body["payload"]["application"]["id"] = 4242
    raw = json.dumps(body).encode()
    adapter = stubs.ats.GreenhouseAdapter(webhook_secret="s", harvest_auth_url="a",
                                          harvest_base_url="b", client_id="c", client_secret="d")
    got = []
    adapter.on_new_application(got.append)
    assert adapter.handle_webhook(raw, {"greenhouse-event-id": "e1"}) == "accepted"   # no signature
    assert adapter.handle_webhook(raw, {"Greenhouse-Event-ID": "e1"}) == "duplicate"
    ping = json.dumps({"action": "ping"}).encode()
    assert adapter.handle_webhook(ping, {}) == "ignored"
    app = got[0]
    assert app["application_id"] == "4242" and isinstance(app["candidate_id"], str)
    assert app["event_id"] == "e1" and app["resume"]["type"] == "resume"
    assert app == adapter.get_application("4242")
    rd = store.run_dir("4242")
    assert (rd / "webhook.json").read_bytes() == raw and (rd / "webhook_headers.json").is_file()

    dest = adapter.fetch_resume(app, rd / "resume.pdf")
    assert dest.read_bytes() == _pdf(app["resume"]["filename"]).read_bytes()
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-x")
    r1 = adapter.write_back(app, "note one", pdf)
    r2 = adapter.write_back(app, "note two", pdf)
    assert (r1["note_id"], r1["attachment_id"]) == ("stub-note-1", "stub-att-1")
    assert r2["note_id"] == "stub-note-2"
    assert (rd / "writeback_stub" / "1" / "note.txt").read_text() == "note one"
    assert (rd / "writeback_stub" / "2" / "a.pdf").read_bytes() == b"%PDF-x"


# ── end to end ────────────────────────────────────────────────────
def test_demo_stub_henry_end_to_end(runs_dir, capsys):
    assert demo.main(["--stub"]) == 0
    out = capsys.readouterr().out
    assert "demo OK" in out
    app_dirs = [p for p in runs_dir.iterdir() if p.is_dir()]
    assert len(app_dirs) == 1
    rd = app_dirs[0]
    assert (rd / "resume.pdf").is_file() and (rd / "resume.txt").is_file()
    assert (rd / "resume_links.json").is_file() and (rd / "claims.v1.json").is_file()
    assert list((rd / "annotated").glob("henry_chen_resume_verified_*.pdf"))


def test_demo_refuses_real_ats(capsys):
    assert demo.main(["--stub", "--real", "ats"]) == 2
    assert "mock ATS" in capsys.readouterr().err


def test_demo_check_catches_missing_pieces(runs_dir):
    rd = store.run_dir("55")
    fails = demo.check_run(rd, [], store)
    assert any("claims.v1.json" in f for f in fails) and any("snapshot" in f for f in fails)
