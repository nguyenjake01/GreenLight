"""J14 tests: More context / Suggest questions, llm stub only, offline (design doc §11 J14)."""
import copy
import json
import threading
from pathlib import Path

import jsonschema
import pytest

from verifier.contracts import stubs
from verifier.contracts.errors import LLMError
from verifier.core import generate as gen, pipeline, render, store

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
SCHEMA = json.loads((Path(gen.__file__).parents[1] / "contracts" / "schemas"
                     / "generated.schema.json").read_text())


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path / "runs"))
    for k in ("VERIFIER_REAL", "RUN_CHECK", "SLACK_ACTIONS", "CUT", "WRITEBACK_CAP_S"):
        monkeypatch.delenv(k, raising=False)
    pipeline._APPS.clear()
    return tmp_path / "runs"


@pytest.fixture
def docs():
    claims = json.loads((FIXTURES / "claims" / "henrychen4736.json").read_text())
    verdict = json.loads((FIXTURES / "verdicts" / "henrychen4736.json").read_text())
    claim = claims["claims"][0]
    vclaim = next(v for v in verdict["claims"] if v["claim_id"] == claim["claim_id"])
    return claim, vclaim, verdict


class FakeLLM:
    """Replaces the llm stub's `llm_chat`; replies come from a list, the last one repeats."""

    def __init__(self, replies, model="test-model"):
        self.replies, self.model, self.calls = list(replies), model, []

    def __call__(self, messages, schema=None, **kw):
        self.calls.append((copy.deepcopy(messages), schema, kw))
        r = self.replies[min(len(self.calls), len(self.replies)) - 1]
        if isinstance(r, Exception):
            raise r
        return {"text": r, "json": None, "model": self.model, "duration_s": 0.0,
                "usage": {"prompt_tokens": 0, "completion_tokens": 0}}


def patch_llm(monkeypatch, replies, **kw):
    fake = FakeLLM(replies, **kw)
    monkeypatch.setattr(stubs.llm, "llm_chat", fake)
    return fake


# ── prompt input ──────────────────────────────────────────────────────────────

def test_prompt_input_has_only_allowed_fields(monkeypatch, docs):
    claim, vclaim, verdict = docs
    claim = {**claim, "raw_api_body": {"secret": "RAW-BODY"}, "readme": "README-TEXT"}
    vclaim = {**vclaim, "internal": "SHOULD-NOT-APPEAR"}
    verdict = {**verdict, "inputs": {"evidence_sha256": "HASH-NOT-SENT"}, "application_id": "APP-ID"}
    fake = patch_llm(monkeypatch, ["- fine"])
    gen.generate_text("more_context", claim, vclaim, verdict)
    messages, schema, kw = fake.calls[0]
    user = json.loads(messages[1]["content"])
    assert set(user) == {"claim", "verdict_claim", "account", "context"}
    assert set(user["claim"]) == {"type", "source_text", "assertions"}
    assert set(user["verdict_claim"]) == {"label", "top_reason", "checks", "notes", "flags",
                                          "evidence_links"}
    assert user["account"] == verdict["account"] and user["context"] == verdict["context"]
    blob = messages[0]["content"] + messages[1]["content"]
    for leaked in ("RAW-BODY", "README-TEXT", "SHOULD-NOT-APPEAR", "HASH-NOT-SENT", "APP-ID"):
        assert leaked not in blob
    # one call, no schema, temperature 0, reasoning off, 500 tokens, 60 s
    assert len(fake.calls) == 1 and schema is None
    assert kw == {"temperature": 0.0, "max_tokens": 500, "reasoning": False, "timeout_s": 60.0}


@pytest.mark.parametrize("kind,version", [("more_context", "context-v1"),
                                          ("suggest_questions", "questions-v1")])
def test_generated_validates_and_reports_model(monkeypatch, docs, kind, version):
    claim, vclaim, verdict = docs
    patch_llm(monkeypatch, ["- one\n- two"], model="served-model")
    out = gen.generate_text(kind, claim, vclaim, verdict)
    jsonschema.Draft202012Validator(SCHEMA).validate(out)
    assert out["model"] == "served-model" and out["prompt_version"] == version
    assert out["kind"] == kind and out["claim_id"] == claim["claim_id"]
    assert out["verdict_id"] == verdict["verdict_id"]
    assert (gen.PROMPTS_DIR / f"{version}.md").is_file()


def test_stub_llm_works_without_patching(docs):
    claim, vclaim, verdict = docs
    out = gen.generate_text("suggest_questions", claim, vclaim, verdict)
    jsonschema.Draft202012Validator(SCHEMA).validate(out)
    assert out["text"] == "(stub reply)" and out["model"] == "stub"


# ── guard ─────────────────────────────────────────────────────────────────────

def test_guard_retries_once_then_passes(monkeypatch, docs):
    claim, vclaim, verdict = docs
    fake = patch_llm(monkeypatch, ["The candidate is a liar.", "- 35 of 35 commits"])
    out = gen.generate_text("suggest_questions", claim, vclaim, verdict)
    assert len(fake.calls) == 2 and out["text"] == "- 35 of 35 commits"
    reminder = fake.calls[1][0][-1]
    assert reminder["role"] == "user" and "liar" in reminder["content"]


def test_guard_falls_back_after_second_violation(monkeypatch, docs):
    claim, vclaim, verdict = docs
    fake = patch_llm(monkeypatch, ["Looks fake.", "Do not hire."])
    out = gen.generate_text("more_context", claim, vclaim, verdict)
    assert len(fake.calls) == 2
    assert out["text"] == "Couldn't generate a neutral summary; see the checks on the card."
    jsonschema.Draft202012Validator(SCHEMA).validate(out)


def test_banned_word_in_source_text_is_allowed(monkeypatch, docs):
    claim, vclaim, verdict = docs
    claim = {**claim, "source_text": "Built a fake news detector in Python"}
    fake = patch_llm(monkeypatch, ["- The project is a fake news detector, per the resume."])
    out = gen.generate_text("more_context", claim, vclaim, verdict)
    assert len(fake.calls) == 1 and "fake news detector" in out["text"]
    # ... but a different banned word in the same reply still trips it
    assert gen.banned_words("fake news, and a liar", claim["source_text"]) == ["liar"]


def test_guard_matches_words_not_substrings_and_ignores_urls():
    assert gen.banned_words("Fraudulent? Hiring! HIRE.", "") == ["fraudulent", "hire", "hiring"]
    assert gen.banned_words("fakeapp, shire, whiteboard, rejection-free", "") == ["rejection"]
    assert gen.banned_words("see https://github.com/x/fake-app", "") == []


def test_text_capped_and_links_kept(monkeypatch, docs):
    claim, vclaim, verdict = docs
    patch_llm(monkeypatch, ["\n".join(["- " + "x" * 90] * 60)])
    out = gen.generate_text("more_context", claim, vclaim, verdict)
    assert len(out["text"]) <= 2500
    for url in vclaim["evidence_links"]:
        assert url in out["text"]


def test_llm_error_propagates(monkeypatch, docs):
    claim, vclaim, verdict = docs
    patch_llm(monkeypatch, [LLMError("down")])
    with pytest.raises(LLMError):
        gen.generate_text("more_context", claim, vclaim, verdict)


# ── pipeline.generate ─────────────────────────────────────────────────────────

_counter = iter(range(5000, 10**6))


def make_app():
    n = next(_counter)
    return {
        "application_id": str(n), "candidate_id": str(n + 7), "candidate_name": "Henry Chen",
        "emails": ["h@example.com"], "website_addresses": [], "social_media_addresses": [],
        "job_title": "Software Engineer Intern", "applied_at": "2026-10-25T14:01:50Z",
        "profile_url": f"http://ats.local/candidates/{n}",
        "resume": {"filename": "henry.pdf", "url": "http://x/henry.pdf", "type": "resume"},
        "event_id": f"evt{n}",
    }


def slack_log(env):
    p = env / "_slack_stub.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


@pytest.fixture
def aid():
    app = make_app()
    pipeline.run(app, wait=True)
    return app["application_id"]


@pytest.mark.parametrize("kind,label,version", [
    ("more_context", "More context", "context-v1"),
    ("suggest_questions", "Suggested questions", "questions-v1")])
def test_pipeline_generate_saves_file_and_updates_thread(env, aid, kind, label, version):
    n = len(slack_log(env))
    pipeline.generate(aid, "c3", kind, "Jake")
    new = slack_log(env)[n:]
    assert [l["call"] for l in new] == ["post_thread", "update_card"]
    assert new[0]["text"].startswith(f"⏳ Jake asked for {label} on ")
    assert new[1]["ts"] == new[0]["ts"]
    assert new[1]["text"].startswith("(stub reply)")  # the render stub shows the text itself
    files = list((store.run_dir(aid) / "generated").glob(f"c3-{kind}-*.json"))
    assert len(files) == 1
    saved = json.loads(files[0].read_text())
    jsonschema.Draft202012Validator(SCHEMA).validate(saved)
    assert saved["prompt_version"] == version and saved["model"] == "stub"
    assert saved["text"].startswith("(stub reply)")
    assert store.read_meta(aid)["stage_errors"] == []


def test_pipeline_generate_llm_error(env, aid, monkeypatch):
    patch_llm(monkeypatch, [LLMError("down")])
    meta_before = store.read_meta(aid)
    n = len(slack_log(env))
    pipeline.generate(aid, "c3", "more_context", "Jake")
    new = slack_log(env)[n:]
    assert [l["call"] for l in new] == ["post_thread", "update_card"]
    assert new[1]["text"] == "Couldn't generate this right now."
    assert not list((store.run_dir(aid) / "generated").glob("*.json"))
    assert store.read_meta(aid)["stage_errors"] == meta_before["stage_errors"] == []


def test_pipeline_generate_cut(env, aid, monkeypatch):
    fake = patch_llm(monkeypatch, ["- x"])
    monkeypatch.setenv("CUT", "generate")
    n = len(slack_log(env))
    pipeline.generate(aid, "c3", "suggest_questions", "Jake")
    new = slack_log(env)[n:]
    assert [l["text"] for l in new] == ["Not available."]
    assert not fake.calls
    assert not (store.run_dir(aid) / "generated").exists() or not list(
        (store.run_dir(aid) / "generated").glob("*.json"))


def test_pipeline_generate_does_not_wait_for_the_lock(env, aid, monkeypatch):
    patch_llm(monkeypatch, ["- fine"])
    lock = pipeline._lock(aid)
    done = threading.Event()
    with lock:  # a run is "in progress"
        t = threading.Thread(target=lambda: (pipeline.generate(aid, "c3", "more_context", "Jake"),
                                             done.set()))
        t.start()
        assert done.wait(10), "generate waited on the application lock"
        t.join()
    assert list((store.run_dir(aid) / "generated").glob("c3-more_context-*.json"))


def test_render_generated_footer(docs):
    claim, vclaim, verdict = docs
    out = gen.generate_text("more_context", claim, vclaim, verdict)
    msg = render.render_generated(claim, out)
    assert "Generated locally by stub · context-v1 · based on " + verdict["verdict_id"] \
        in json.dumps(msg, ensure_ascii=False)
