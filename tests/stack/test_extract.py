import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from verifier.contracts import registry
from verifier.contracts.errors import LLMError, LLMSchemaError
from verifier.core import store
from verifier.stack import extract

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def _load(p):
    return json.loads((FIXTURES / p).read_text())


def flat(**kw):
    base = {"type": "project", "section": "Projects", "source_text": "x", "repo_url": None,
            "repo_name": None, "role": None, "language": None, "min_stars": None,
            "start_year": None, "scale": None, "public_signal": None, "behavior": None,
            "skill_name": None, "skill_kind": None, "contrib_repo": None, "contrib_role": None,
            "min_prs": None, "description": None, "category": None}
    base.update(kw)
    return base


def resp(claims, model="served"):
    return {"text": json.dumps({"claims": claims}), "json": {"claims": claims}, "model": model,
            "duration_s": 0.1, "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


@pytest.fixture
def fake_llm(monkeypatch):
    """Replace the registry's llm with a scripted fake; `.script` is a list of replies/exceptions."""
    state = SimpleNamespace(script=[], calls=[])

    def llm_chat(messages, schema=None, **kw):
        state.calls.append({"messages": copy.deepcopy(messages), "schema": schema, **kw})
        item = state.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    ns = SimpleNamespace(llm_chat=llm_chat)
    real_get = registry.get
    monkeypatch.setattr(registry, "get", lambda name: ns if name == "llm" else real_get(name))
    return state


def mk_resume(text, links=()):
    return {"text": text, "links": list(links), "sha256": "abc", "page_count": 1}


CAND = {"name": "N", "emails": [], "github_login": None, "github_login_source": "none",
        "github_login_conflict": None}


# ── real extract with the stub llm reproduces the fixture ──────────────────────

@pytest.mark.parametrize("login,resume_file", [("henrychen4736", "henry"),
                                               ("rv-fake-candidate", "fake")])
def test_stub_llm_reproduces_henry_fixture(login, resume_file, monkeypatch):
    monkeypatch.delenv("VERIFIER_REAL", raising=False)  # llm resolves to the stub
    resume = _load(f"resumes/{resume_file}.resume.json")
    cand = _load("claims/henrychen4736.json")["candidate"]
    doc = extract.extract_claims(resume, cand, application_id="12345")
    store.validate(doc, "claims")
    if resume_file == "henry":
        want = _load("claims/henrychen4736.json")
        for d in (doc, want):
            d.pop("created_at")
            d["extraction"].pop("model")  # stub reports "stub"; the fixture names the real model
        assert doc == want
    else:
        assert doc["extraction"]["status"] == "ok"


# ── prompt ─────────────────────────────────────────────────────────────────────

def test_prompt_contains_resume_and_links(fake_llm):
    fake_llm.script = [resp([])]
    resume = mk_resume("Some resume\nBuilt foo", [
        {"uri": "https://github.com/a/foo", "anchor_text": "foo repo", "page": 0},
        {"uri": "https://example.com", "anchor_text": "", "page": 0}])
    extract.extract_claims(resume, CAND, application_id="1")
    (call,) = fake_llm.calls
    assert call["schema"]["title"] == "extraction_output"
    assert call["temperature"] == 0 and call["reasoning"] is False
    assert call["max_tokens"] == 4000 and call["timeout_s"] == 180
    system, user = call["messages"]
    assert "source_text" in system["content"] and "substantial" in system["content"]
    assert "Some resume\nBuilt foo" in user["content"]
    assert "foo repo → https://github.com/a/foo" in user["content"]
    assert "→ https://example.com" in user["content"]


# ── post-processing ────────────────────────────────────────────────────────────

def test_empty_source_text_dropped_and_order_and_ids():
    text = "A  first\nthing\n\nSecond thing\nThird thing"
    out = extract.postprocess([
        flat(type="unchecked", category="other", source_text="Third thing"),
        flat(type="unchecked", category="other", source_text="   "),
        flat(type="unchecked", category="other", source_text="not in resume"),
        flat(type="unchecked", category="other", source_text="Second thing"),
        flat(type="unchecked", category="other", source_text="A first thing"),  # whitespace-normalized
        flat(type="unchecked", category="other", source_text="also missing"),
    ], text)
    assert [(c["claim_id"], c["source_text"]) for c in out] == [
        ("c1", "A first thing"), ("c2", "Second thing"), ("c3", "Third thing"),
        ("c4", "not in resume"), ("c5", "also missing")]


def test_same_source_text_keeps_model_order():
    text = "Languages: Python, Dart"
    out = extract.postprocess([
        flat(type="skill", source_text=text, skill_name="Python", skill_kind="language"),
        flat(type="skill", source_text=text, skill_name="Dart", skill_kind="language"),
    ], text)
    assert [c["assertions"]["name"] for c in out] == ["Python", "Dart"]


def test_source_text_capped_at_300():
    long = "word " * 100
    out = extract.postprocess([flat(type="unchecked", category="other", source_text=long)], long)
    assert len(out[0]["source_text"]) == 300


@pytest.mark.parametrize("url,expected", [
    ("https://github.com/o/r", "https://github.com/o/r"),
    ("https://github.com/o/r.git", "https://github.com/o/r"),
    ("https://github.com/o/r/", "https://github.com/o/r"),
    ("https://github.com/o/r/tree/main/src", "https://github.com/o/r"),
    ("http://www.github.com/o/my.repo-x/blob/x", "https://github.com/o/my.repo-x"),
    ("github.com/o/r", "https://github.com/o/r"),
    ("https://github.com/o", None),
    ("https://gitlab.com/o/r", None),
    ("", None),
    (None, None),
])
def test_normalize_github_url(url, expected):
    assert extract.normalize_github_url(url) == expected


def test_project_url_fills_name_and_public_signal():
    (c,) = extract.postprocess([flat(source_text="p", repo_url="https://github.com/o/Thing.git/",
                                     public_signal="none")], "p")
    a = c["assertions"]
    assert a["repo_url"] == "https://github.com/o/Thing"
    assert a["repo_name"] == "Thing"
    assert a["public_signal"] == "link"
    assert not c["needs_review"]


def test_public_signal_rules():
    out = extract.postprocess([
        flat(source_text="a", repo_name="a", public_signal="open_source_wording"),
        flat(source_text="b", repo_name="b", public_signal=None),
        flat(source_text="c", repo_name="c", repo_url="https://github.com/o/c",
             public_signal="open_source_wording"),
    ], "a b c")
    assert [c["assertions"]["public_signal"] for c in out] == ["open_source_wording", "none", "link"]


def test_non_github_repo_url_is_dropped():
    (c,) = extract.postprocess([flat(source_text="p", repo_url="https://example.com/x",
                                     public_signal="link")], "p")
    assert c["assertions"]["repo_url"] is None
    assert c["assertions"]["public_signal"] == "none"  # "link" needs a usable repo URL


def test_typed_assertions():
    out = extract.postprocess([
        flat(type="skill", source_text="s", skill_name="Rust", skill_kind="language"),
        flat(type="oss_contribution", source_text="o", contrib_repo="https://github.com/a/b/pull/3",
             contrib_role="contributor", min_prs=1),
        flat(type="unchecked", source_text="u", category="education"),
    ], "s o u")
    assert out[0]["assertions"] == {"name": "Rust", "kind": "language"}
    assert out[1]["assertions"] == {"repo": "a/b", "role": "contributor", "min_prs": 1,
                                    "description": None}
    assert out[2]["assertions"] == {"category": "education"}
    assert not any(c["needs_review"] for c in out)


@pytest.mark.parametrize("bad", [
    flat(type="skill", source_text="s"),                                  # no skill_name/kind
    flat(type="oss_contribution", source_text="s", contrib_repo="nope"),  # not owner/repo
    flat(type="oss_contribution", source_text="s", contrib_repo="a/b"),   # no role
    flat(type="unchecked", source_text="s"),                              # no category
    flat(type="project", source_text="s", min_stars="2k"),                # wrong type
])
def test_invalid_claim_becomes_needs_review(bad):
    (c,) = extract.postprocess([bad], "s")
    assert c["type"] == "unchecked" and c["needs_review"] is True
    assert c["assertions"] == {"category": "other"}
    assert c["source_text"] == "s" and c["section"] == "Projects"


# ── extract_claims: status, retry, extraction block ────────────────────────────

def test_ok_extraction_block_and_candidate_passthrough(fake_llm):
    fake_llm.script = [resp([flat(type="unchecked", category="other", source_text="x")], "m1")]
    cand = {**CAND, "name": "Zed", "github_login": "zed", "github_login_source": "resume_link"}
    doc = extract.extract_claims(mk_resume("x"), cand, application_id="77")
    store.validate(doc, "claims")
    assert doc["application_id"] == "77" and doc["claims_version"] == 1 and doc["edits"] == []
    assert doc["candidate"] == cand and doc["candidate"] is not cand
    assert doc["extraction"] == {"model": "m1", "prompt_version": "extract-v1", "temperature": 0,
                                 "resume_sha256": "abc", "status": "ok"}


def test_retry_once_then_ok(fake_llm):
    fake_llm.script = [LLMSchemaError("bad", "garbage"),
                       resp([flat(type="unchecked", category="other", source_text="x")])]
    doc = extract.extract_claims(mk_resume("x"), CAND, application_id="1")
    assert doc["extraction"]["status"] == "retried"
    assert len(doc["claims"]) == 1
    first, second = fake_llm.calls
    assert second["messages"][:-1] == first["messages"]
    assert second["messages"][-1] == {"role": "user", "content":
                                      "Your reply did not match the schema. Reply with JSON only."}


def test_two_schema_failures_gives_failed_empty(fake_llm, monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "cfg-model")
    fake_llm.script = [LLMSchemaError("bad", "a"), LLMSchemaError("bad", "b")]
    doc = extract.extract_claims(mk_resume("x"), CAND, application_id="1")
    store.validate(doc, "claims")
    assert doc["extraction"]["status"] == "failed" and doc["claims"] == []
    assert doc["extraction"]["model"] == "cfg-model"
    assert len(fake_llm.calls) == 2


def test_llm_error_propagates_without_retry(fake_llm):
    fake_llm.script = [LLMError("down")]
    with pytest.raises(LLMError):
        extract.extract_claims(mk_resume("x"), CAND, application_id="1")
    assert len(fake_llm.calls) == 1
