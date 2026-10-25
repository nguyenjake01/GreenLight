"""J7 orchestrator tests: stubs only, offline (design doc §5, §8.3)."""
import copy
import json
import threading
import time
from pathlib import Path

import pytest

from verifier.contracts import registry, stubs
from verifier.contracts.errors import HarvestError, LLMError, SandboxError, SlackError
from verifier.core import pipeline, store

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path / "runs"))
    for k in ("VERIFIER_REAL", "RUN_CHECK", "SLACK_ACTIONS", "CUT", "WRITEBACK_CAP_S"):
        monkeypatch.delenv(k, raising=False)
    pipeline._APPS.clear()
    return tmp_path / "runs"


_counter = iter(range(1000, 10**6))


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


def slack_log(env):
    p = env / "_slack_stub.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def texts(env, call=None):
    return [l["text"] or "" for l in slack_log(env) if call is None or l["call"] == call]


def writebacks(app_id):
    d = store.run_dir(app_id) / "writeback_stub"
    return sorted(d.iterdir()) if d.is_dir() else []


def errors(app_id):
    return store.read_meta(app_id)["stage_errors"]


# ── happy path ────────────────────────────────────────────────────────────────

def test_run_stub_henry(env):
    app = make_app()
    pipeline.run(app, wait=True)
    rd = store.run_dir(app["application_id"])
    meta = store.read_meta(app["application_id"])
    assert (rd / "resume.pdf").is_file() and (rd / "resume.txt").is_file()
    assert (rd / "resume_links.json").is_file() and (rd / "claims.v1.json").is_file()
    assert meta["applied_at"] == app["applied_at"]
    assert meta["latest"] == {"claims_version": 1, "snapshot_id": meta["latest"]["snapshot_id"],
                              "verdict_n": 4}
    assert meta["slack"]["card_ts"] == "stub.1" and meta["slack"]["card_permalink"]
    assert meta["slack"]["pdf_permalink"].endswith("_resume_verified_4.pdf")
    assert meta["runs"] == {"c1": "done", "c2": "done", "c3": "done"}
    assert meta["stage_errors"] == []
    assert len(meta["writebacks"]) == 1 and meta["writebacks"][0]["verdict_n"] == 4
    assert len(writebacks(app["application_id"])) == 1
    assert (rd / "verdicts" / "verdict_4.json").is_file()
    assert not pipeline.is_busy(app["application_id"])


def test_run_stub_fake(env):
    app = make_app("Alex Rivera", "fake.pdf")
    pipeline.run(app, wait=True)
    meta = store.read_meta(app["application_id"])
    assert meta["latest"]["verdict_n"] >= 1 and len(meta["writebacks"]) == 1


def test_run_twice_is_ignored(env):
    app = make_app()
    pipeline.run(app, wait=True)
    before = texts(env)
    pipeline.run(app, wait=True)
    assert texts(env) == before


def test_run_without_wait_returns_and_finishes(env):
    app = make_app()
    pipeline.run(app)  # lock is taken before the thread starts
    deadline = time.time() + 10
    while pipeline.is_busy(app["application_id"]) and time.time() < deadline:
        time.sleep(0.02)
    assert len(store.read_meta(app["application_id"])["writebacks"]) == 1


def test_run_check_off_marks_skipped(env, monkeypatch):
    monkeypatch.setenv("RUN_CHECK", "off")
    app = make_app()
    pipeline.run(app, wait=True)
    meta = store.read_meta(app["application_id"])
    assert meta["runs"] == {"c1": "skipped", "c2": "skipped", "c3": "skipped"}
    assert "post_thread" not in [l["call"] for l in slack_log(env)]
    assert meta["latest"]["verdict_n"] == 1 and len(meta["writebacks"]) == 1


# ── stage errors ──────────────────────────────────────────────────────────────

def test_annotate_failure_no_pdf_no_writeback(env, monkeypatch):
    def boom(*a, **k):
        from verifier.contracts.errors import AnnotateError
        raise AnnotateError("cannot draw\nsecond line")
    monkeypatch.setattr(stubs.annotate, "annotate_pdf", boom)
    app = make_app()
    pipeline.run(app, wait=True)
    meta = store.read_meta(app["application_id"])
    errs = meta["stage_errors"]
    assert errs and all(e["stage"] == "annotate" for e in errs)
    assert errs[0]["error"] == "cannot draw" and "at" in errs[0]
    assert meta["writebacks"] == [] and writebacks(app["application_id"]) == []
    assert meta["slack"]["pdf_permalink"] is None
    assert "upload_file" not in [l["call"] for l in slack_log(env)]
    assert meta["latest"]["verdict_n"] == 4  # the run checks still completed


def test_extraction_failure_could_not_read_card(env, monkeypatch):
    def boom(*a, **k):
        raise LLMError("model unreachable")
    monkeypatch.setattr(stubs.extract, "extract_claims", boom)
    app = make_app()
    pipeline.run(app, wait=True)
    meta = store.read_meta(app["application_id"])
    assert [e["stage"] for e in meta["stage_errors"]] == ["extract"]
    assert meta["latest"]["verdict_n"] is None and meta["writebacks"] == []
    last = [l for l in slack_log(env) if l["call"] == "update_card"][-1]
    assert "Could not read claims" in last["text"]
    assert not (store.run_dir(app["application_id"]) / "verdicts").exists()


def test_extraction_status_failed(env, monkeypatch):
    real = stubs.extract.extract_claims

    def failed(*a, **k):
        doc = real(*a, **k)
        doc["extraction"]["status"] = "failed"
        doc["claims"] = []
        return doc
    monkeypatch.setattr(stubs.extract, "extract_claims", failed)
    app = make_app()
    pipeline.run(app, wait=True)
    meta = store.read_meta(app["application_id"])
    assert meta["stage_errors"][0]["stage"] == "extract" and meta["writebacks"] == []
    assert meta["latest"]["claims_version"] == 1
    assert "Could not read claims" in [l for l in slack_log(env) if l["call"] == "update_card"][-1]["text"]


def test_resume_error_posts_could_not_read_card(env, monkeypatch):
    from verifier.contracts.errors import ResumeError
    monkeypatch.setattr(stubs.resume, "read_resume", lambda p: (_ for _ in ()).throw(ResumeError("bad pdf")))
    app = make_app()
    pipeline.run(app, wait=True)
    meta = store.read_meta(app["application_id"])
    assert meta["stage_errors"][0] == {**meta["stage_errors"][0], "stage": "resume", "error": "bad pdf"}
    assert "Could not read claims" in texts(env, "post_card")[0]
    assert meta["slack"]["card_ts"] is not None and meta["writebacks"] == []


def test_slack_error_does_not_stop_run(env, monkeypatch):
    def boom(*a, **k):
        raise SlackError("rate limited")
    monkeypatch.setattr(stubs.slack, "post_card", boom)
    app = make_app()
    pipeline.run(app, wait=True)
    meta = store.read_meta(app["application_id"])
    assert any(e["stage"] == "slack" for e in meta["stage_errors"])
    assert len(meta["writebacks"]) == 1 and meta["slack"]["card_ts"] is None


def test_fetch_error_continues_on_empty_snapshot(env, monkeypatch):
    from verifier.contracts.errors import FetchError
    monkeypatch.setattr(stubs.fetch, "fetch_evidence",
                        lambda *a, **k: (_ for _ in ()).throw(FetchError("disk full")))
    app = make_app()
    pipeline.run(app, wait=True)
    meta = store.read_meta(app["application_id"])
    assert [e["stage"] for e in meta["stage_errors"]] == ["fetch"]
    ev = store.read_json(store.run_dir(app["application_id"]) / "snapshots"
                         / meta["latest"]["snapshot_id"] / "evidence.json")
    assert ev["responses"] == [] and ev["github_login"] and ev["misses"]
    assert len(meta["writebacks"]) == 1


def test_run_check_error_no_flag(env, monkeypatch):
    real = stubs.harness.run_check

    def flaky(snapshot, claim, repo, *, mode):
        if claim["claim_id"] == "c2":
            raise SandboxError("docker not running")
        return real(snapshot, claim, repo, mode=mode)
    monkeypatch.setattr(stubs.harness, "run_check", flaky)
    app = make_app()
    pipeline.run(app, wait=True)
    meta = store.read_meta(app["application_id"])
    assert meta["runs"] == {"c1": "done", "c2": "error", "c3": "done"}
    assert meta["stage_errors"][0]["stage"] == "run_check" and meta["stage_errors"][0]["claim_id"] == "c2"
    assert any("Run check error" in t for t in texts(env, "update_card"))
    assert len(meta["writebacks"]) == 1


def test_harvest_error_is_stage_error(env, monkeypatch):
    monkeypatch.setattr(stubs.ats.GreenhouseAdapter, "write_back",
                        lambda *a, **k: (_ for _ in ()).throw(HarvestError("401 from harvest")))
    app = make_app()
    pipeline.run(app, wait=True)
    meta = store.read_meta(app["application_id"])
    assert meta["writebacks"] == [] and meta["stage_errors"][0]["stage"] == "write_back"


def test_error_text_is_first_line_and_capped(env, monkeypatch):
    monkeypatch.setattr(stubs.fetch, "fetch_evidence",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x" * 500 + "\nmore")))
    app = make_app()
    pipeline.run(app, wait=True)
    assert len(errors(app["application_id"])[0]["error"]) == 200


# ── concurrency and write-back cap ────────────────────────────────────────────

def test_recheck_waits_for_running_flow(env, monkeypatch):
    real = stubs.harness.run_check

    def slow(snapshot, claim, repo, *, mode):
        time.sleep(0.3)
        return real(snapshot, claim, repo, mode=mode)
    monkeypatch.setattr(stubs.harness, "run_check", slow)
    app = make_app()
    pipeline.run(app)  # background
    aid = app["application_id"]
    assert pipeline.is_busy(aid)
    t = threading.Thread(target=pipeline.recheck, args=(aid, "Jake"))
    t.start()
    time.sleep(0.1)
    assert not any("Re-check started" in x for x in texts(env))  # still waiting
    t.join(30)
    log = texts(env)
    started = next(i for i, x in enumerate(log) if "Re-check started" in x)
    last_run = max(i for i, x in enumerate(log) if x.startswith("Run check done"))
    assert started > last_run
    meta = store.read_meta(aid)
    assert len(meta["writebacks"]) == 2


def test_writeback_cap_fires_once_before_run_ends(env, monkeypatch):
    monkeypatch.setenv("WRITEBACK_CAP_S", "1")
    real = stubs.harness.run_check
    stamps = {}

    def slow(snapshot, claim, repo, *, mode):
        time.sleep(0.8)
        stamps[claim["claim_id"]] = time.time()
        return real(snapshot, claim, repo, mode=mode)
    monkeypatch.setattr(stubs.harness, "run_check", slow)
    wb_times = []
    orig = stubs.ats.GreenhouseAdapter.write_back

    def spy(self, *a, **k):
        wb_times.append(time.time())
        return orig(self, *a, **k)
    monkeypatch.setattr(stubs.ats.GreenhouseAdapter, "write_back", spy)
    app = make_app()
    pipeline.run(app, wait=True)
    meta = store.read_meta(app["application_id"])
    assert len(wb_times) == 1 and len(meta["writebacks"]) == 1
    assert wb_times[0] < stamps["c3"]               # before the last run check ended
    assert meta["writebacks"][0]["verdict_n"] < meta["latest"]["verdict_n"]
    # the card still updated after the write-back, and the newest PDF reached the thread
    assert meta["slack"]["pdf_permalink"].endswith("_resume_verified_4.pdf")
    assert meta["runs"] == {"c1": "done", "c2": "done", "c3": "done"}


def test_global_sandbox_semaphore_is_two():
    assert pipeline._SANDBOX_SEM._value == 2


# ── recheck and carry-forward ─────────────────────────────────────────────────

def test_recheck_carries_traces_when_pushed_at_unchanged(env, monkeypatch):
    app = make_app()
    pipeline.run(app, wait=True)
    aid = app["application_id"]
    calls = []
    real = stubs.harness.run_check
    monkeypatch.setattr(stubs.harness, "run_check",
                        lambda s, c, r, *, mode: calls.append(c["claim_id"]) or real(s, c, r, mode=mode))
    old_sid = store.read_meta(aid)["latest"]["snapshot_id"]
    time.sleep(1.1)  # snapshot ids have 1 s resolution
    pipeline.recheck(aid, "Jake")
    meta = store.read_meta(aid)
    assert meta["latest"]["snapshot_id"] != old_sid
    assert calls == []                                       # nothing re-ran
    new_dir = store.run_dir(aid) / "snapshots" / meta["latest"]["snapshot_id"] / "run_traces"
    assert sorted(p.name for p in new_dir.iterdir()) == ["c1.json", "c2.json", "c3.json"]
    assert meta["runs"] == {"c1": "done", "c2": "done", "c3": "done"}
    assert "↻ Re-check started by Jake." in texts(env)
    assert texts(env)[-1] == "↻ Re-check done: no label changes."
    assert len(meta["writebacks"]) == 2
    # raw outputs copied with the trace
    assert (store.run_dir(aid) / "snapshots" / meta["latest"]["snapshot_id"] / "raw"
            / "52fe1a016b4b130902deb6b6c2c1983fbdae0366865f8a606137ddd7cec01383.txt").is_file()


def test_recheck_reruns_when_pushed_at_changed(env, monkeypatch):
    app = make_app()
    pipeline.run(app, wait=True)
    aid = app["application_id"]
    real_fetch = stubs.fetch.fetch_evidence

    def changed(claims, *, run_dir, mode):
        snap = real_fetch(claims, run_dir=run_dir, mode=mode)
        # rewrite cubesolver's repo body in the new snapshot with a later pushed_at
        key = "GET https://api.github.com/repos/henrychen4736/cubesolver"
        rec = next(r for r in snap.evidence["responses"] if r["key"] == key)
        body = json.loads(snap.get_raw(rec["body_path"]))
        body["pushed_at"] = "2030-01-01T00:00:00Z"
        raw = json.dumps(body).encode()
        sha = store.sha256_bytes(raw)
        store.write_bytes(snap.path / "raw" / f"{sha}.json", raw)
        rec["body_path"] = f"raw/{sha}.json"
        (snap.path / "evidence.json").write_bytes(store.dump_json(snap.evidence))
        return type(snap).load(snap.path)
    monkeypatch.setattr(stubs.fetch, "fetch_evidence", changed)
    calls = []
    real = stubs.harness.run_check
    monkeypatch.setattr(stubs.harness, "run_check",
                        lambda s, c, r, *, mode: calls.append(c["claim_id"]) or real(s, c, r, mode=mode))
    time.sleep(1.1)
    pipeline.recheck(aid, "Jake")
    assert calls == ["c3"]


def test_recheck_retries_failed_extraction_only(env, monkeypatch):
    real = stubs.extract.extract_claims
    monkeypatch.setattr(stubs.extract, "extract_claims",
                        lambda *a, **k: (_ for _ in ()).throw(LLMError("down")))
    app = make_app()
    pipeline.run(app, wait=True)
    aid = app["application_id"]
    assert store.read_meta(aid)["latest"]["verdict_n"] is None
    monkeypatch.setattr(stubs.extract, "extract_claims", real)
    pipeline.recheck(aid, "Jake")
    meta = store.read_meta(aid)
    assert meta["latest"]["claims_version"] == 1 and meta["latest"]["verdict_n"] is not None
    assert len(meta["writebacks"]) == 1
    # a healthy extraction is never repeated
    called = []
    monkeypatch.setattr(stubs.extract, "extract_claims", lambda *a, **k: called.append(1))
    pipeline.recheck(aid, "Jake")
    assert called == [] and store.read_meta(aid)["latest"]["claims_version"] == 1


def test_recheck_reports_label_changes(env, monkeypatch):
    app = make_app()
    pipeline.run(app, wait=True)
    aid = app["application_id"]
    real = stubs.rubric.compute_verdict

    def flipped(claims, snapshot, traces, *, verdict_n, computed_at):
        v = real(claims, snapshot, traces, verdict_n=verdict_n, computed_at=computed_at)
        if verdict_n > 4:
            for vc in v["claims"]:
                if vc["claim_id"] == "c3":
                    vc["label"] = "contradicted"
        return v
    monkeypatch.setattr(stubs.rubric, "compute_verdict", flipped)
    time.sleep(1.1)
    pipeline.recheck(aid, "Jake")
    assert texts(env)[-1] == "↻ Re-check done: cubesolver 🟡 → 🔴."


# ── edit_claim ────────────────────────────────────────────────────────────────

def test_edit_claim_new_version_same_snapshot(env):
    app = make_app()
    pipeline.run(app, wait=True)
    aid = app["application_id"]
    old = store.read_meta(aid)
    pipeline.edit_claim(aid, "c3", {"scale": "small"}, "Jake")
    meta = store.read_meta(aid)
    rd = store.run_dir(aid)
    v2 = store.read_json(rd / "claims.v2.json")
    assert meta["latest"]["claims_version"] == 2 and meta["latest"]["snapshot_id"] == old["latest"]["snapshot_id"]
    assert v2["claims_version"] == 2
    assert v2["claims"][2]["assertions"]["scale"] == "small"
    assert [(e["field"], e["from"], e["to"], e["by"]) for e in v2["edits"]] == [("scale", "substantial", "small", "Jake")]
    assert store.read_json(rd / "claims.v1.json")["claims"][2]["assertions"]["scale"] == "substantial"
    verdict = store.read_json(rd / "verdicts" / f"verdict_{meta['latest']['verdict_n']}.json")
    assert verdict["inputs"]["claims_version"] == 2
    assert verdict["inputs"]["snapshot_id"] == old["latest"]["snapshot_id"]
    assert len(meta["writebacks"]) == 2
    assert texts(env, "post_thread")[-1].startswith("c3:")  # stub label-change line


def test_edit_claim_no_changes(env):
    app = make_app()
    pipeline.run(app, wait=True)
    aid = app["application_id"]
    before = store.read_meta(aid)
    pipeline.edit_claim(aid, "c3", {"scale": "substantial", "repo_name": "cubesolver"}, "Jake")
    assert texts(env)[-1] == "No changes to cubesolver."
    assert store.read_meta(aid)["latest"] == before["latest"]
    assert not (store.run_dir(aid) / "claims.v2.json").exists()


def test_edit_claim_repo_change_fetches_new_snapshot(env):
    app = make_app()
    pipeline.run(app, wait=True)
    aid = app["application_id"]
    old_sid = store.read_meta(aid)["latest"]["snapshot_id"]
    time.sleep(1.1)
    pipeline.edit_claim(aid, "c3", {"repo_name": "cubesolver2", "repo_url": "https://github.com/henrychen4736/cubesolver2.git"}, "Jake")
    meta = store.read_meta(aid)
    v2 = store.read_json(store.run_dir(aid) / "claims.v2.json")
    assert meta["latest"]["snapshot_id"] != old_sid
    fields = {e["field"] for e in v2["edits"]}
    assert fields == {"repo_name", "repo_url", "public_signal"}
    assert v2["claims"][2]["assertions"]["repo_url"] == "https://github.com/henrychen4736/cubesolver2"
    assert v2["claims"][2]["assertions"]["public_signal"] == "link"


def test_edit_claim_invalid_values_reply(env):
    app = make_app()
    pipeline.run(app, wait=True)
    aid = app["application_id"]
    pipeline.edit_claim(aid, "c3", {"start_year": "1800"}, "Jake")
    assert "start_year" in texts(env)[-1] and not (store.run_dir(aid) / "claims.v2.json").exists()


def test_edit_queues_behind_running_flow(env, monkeypatch):
    real = stubs.harness.run_check
    monkeypatch.setattr(stubs.harness, "run_check",
                        lambda s, c, r, *, mode: (time.sleep(0.3), real(s, c, r, mode=mode))[1])
    app = make_app()
    pipeline.run(app)
    aid = app["application_id"]
    time.sleep(0.15)
    t = threading.Thread(target=pipeline.edit_claim, args=(aid, "c3", {"scale": "small"}, "Jake"))
    t.start()
    t.join(30)
    log = texts(env)
    assert "Queued: this edit applies after the current checks finish." in log
    assert (store.run_dir(aid) / "claims.v2.json").is_file()


def test_validate_edit(env):
    app = make_app()
    pipeline.run(app, wait=True)
    aid = app["application_id"]
    assert pipeline.validate_edit(aid, "c3", {"scale": "small", "min_stars": "3"}) == {}
    errs = pipeline.validate_edit(aid, "c3", {"repo_url": "not a url", "start_year": "1800",
                                              "min_stars": "-2", "scale": "huge"})
    assert set(errs) == {"repo_url", "start_year", "min_stars", "scale"}
    assert pipeline.validate_edit(aid, "c4", {"name": ""}) == {"name": "Required."}
    assert pipeline.validate_edit(aid, "c8", {"x": 1}).keys() == {"_claim"}
    assert pipeline.validate_edit(aid, "zz", {}).keys() == {"_claim"}


# ── show_transcript ───────────────────────────────────────────────────────────

def test_show_transcript_uploads_text_file(env):
    app = make_app()
    pipeline.run(app, wait=True)
    aid = app["application_id"]
    n_before = len(slack_log(env))
    pipeline.show_transcript(aid, "c3")
    new = slack_log(env)[n_before:]
    assert [l["call"] for l in new] == ["upload_file"]
    assert new[0]["text"] == "cubesolver: run transcript"
    path = Path(new[0]["path"])
    assert path.parent == store.run_dir(aid) / "transcripts" and path.name.startswith("c3-snap_")
    text = path.read_text()
    assert "Run transcript: cubesolver" in text and "g++ -std=c++17 -O2 -o /tmp/a.out cubesolver.cpp" in text
    assert "$ ls -la /work" in text and "works_as_claimed" in text
    pipeline.show_transcript(aid, "c3")  # idempotent
    assert len(slack_log(env)) == n_before + 2


def test_show_transcript_without_trace(env):
    app = make_app()
    pipeline.run(app, wait=True)
    pipeline.show_transcript(app["application_id"], "c4")
    assert texts(env)[-1] == "No run transcript for Python yet."


# ── generate ──────────────────────────────────────────────────────────────────

def test_generate_cut_and_missing_module(env, monkeypatch):
    app = make_app()
    pipeline.run(app, wait=True)
    aid = app["application_id"]
    monkeypatch.setenv("CUT", "generate")
    pipeline.generate(aid, "c3", "more_context", "Jake")
    assert texts(env)[-1] == "Not available."
    monkeypatch.setenv("CUT", "")
    pipeline.generate(aid, "c3", "bogus", "Jake")
    assert texts(env)[-1] == "Not available."


def test_generate_with_fake_module(env, monkeypatch):
    import sys
    import types
    app = make_app()
    pipeline.run(app, wait=True)
    aid = app["application_id"]
    mod = types.ModuleType("verifier.core.generate")

    def generate_text(kind, claim, vclaim, verdict):
        return {"claim_id": claim["claim_id"], "kind": kind, "verdict_id": verdict["verdict_id"],
                "model": "stub", "prompt_version": "context-v1", "created_at": store.now_iso(),
                "text": "(stub reply)"}
    mod.generate_text = generate_text
    monkeypatch.setitem(sys.modules, "verifier.core.generate", mod)
    monkeypatch.setattr("verifier.core.generate", mod, raising=False)
    n = len(slack_log(env))
    pipeline.generate(aid, "c3", "more_context", "Jake")
    new = slack_log(env)[n:]
    assert [l["call"] for l in new] == ["post_thread", "update_card"]
    assert "asked for More context on cubesolver" in new[0]["text"]
    assert new[1]["ts"] == new[0]["ts"] and new[1]["text"] == "(stub reply)"
    assert len(list((store.run_dir(aid) / "generated").glob("c3-more_context-*.json"))) == 1

    def llm_down(*a, **k):
        raise LLMError("down")
    mod.generate_text = llm_down
    pipeline.generate(aid, "c3", "suggest_questions", "Jake")
    assert texts(env)[-1] == "Couldn't generate this right now."


# ── card_options ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("actions,cut,buttons,menu", [
    ("plugin", "", True, ["more_context", "suggest_questions", "edit_claim"]),
    ("agent", "", True, ["more_context", "suggest_questions"]),
    ("off", "", False, []),
    ("plugin", "generate", True, ["edit_claim"]),
    ("plugin", "edit", True, ["more_context", "suggest_questions"]),
    ("plugin", "oss,generate,edit", True, []),
    ("agent", "edit", True, ["more_context", "suggest_questions"]),
])
def test_card_options(monkeypatch, actions, cut, buttons, menu):
    monkeypatch.setenv("SLACK_ACTIONS", actions)
    monkeypatch.setenv("CUT", cut)
    app = make_app()
    meta = {"slack": {"pdf_permalink": "https://x/pdf"}}
    opts = pipeline.card_options(app, meta)
    assert opts == {"profile_url": app["profile_url"], "annotated_pdf_url": "https://x/pdf",
                    "action_buttons": buttons, "menu": menu}
    assert pipeline.card_options(app, {"slack": {}})["annotated_pdf_url"] is None


# ── read helpers ──────────────────────────────────────────────────────────────

def test_find_applications_and_claim_and_state(env):
    a1 = make_app("Henry Chen", applied_at="2026-10-25T10:00:00Z")
    a2 = make_app("Henry Smith", applied_at="2026-10-25T12:00:00Z")
    a3 = make_app("Alex Rivera", "fake.pdf", applied_at="2026-10-25T11:00:00Z")
    for a in (a1, a2, a3):
        pipeline.run(a, wait=True)
    i1, i2, i3 = (a["application_id"] for a in (a1, a2, a3))
    assert pipeline.find_applications("henry") == [i2, i1]
    assert pipeline.find_applications("HENRY CHEN") == [i1]
    assert pipeline.find_applications(i3) == [i3]
    assert pipeline.find_applications("nobody") == [] and pipeline.find_applications("") == []
    assert pipeline.find_application("henry") == i2
    assert pipeline.find_application("zzz") is None
    # claims
    assert pipeline.find_claim(i1, "c3") == "c3"
    assert pipeline.find_claim(i1, "CUBESOLVER") == "c3"
    assert pipeline.find_claim(i1, "polytrack") == "c1"
    assert pipeline.find_claim(i1, "python") is not None
    assert pipeline.find_claim(i1, "no such thing") is None
    assert pipeline.find_claim("missing", "c1") is None
    # state
    state = pipeline.get_state(i1)
    assert state["meta"]["application_id"] == i1
    assert state["claims"]["claims_version"] == 1
    assert state["verdict"]["verdict_id"] == "verdict_4"
    with pytest.raises(KeyError):
        pipeline.get_state("missing")
    assert not (env / "missing").exists()


# ── meta helpers ──────────────────────────────────────────────────────────────

def test_meta_helpers_are_atomic_and_idempotent(env):
    app = make_app()
    meta = pipeline._initial_meta(app)
    got, created = store.init_meta("m1", meta)
    assert created and got == meta
    again, created2 = store.init_meta("m1", {**meta, "job_title": "other"})
    assert not created2 and again["job_title"] == meta["job_title"]
    errs = []

    def bump(i):
        store.update_meta("m1", lambda m: m["stage_errors"].append({"stage": "t", "error": str(i), "at": "x"}))
    ts = [threading.Thread(target=bump, args=(i,)) for i in range(20)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(store.read_meta("m1")["stage_errors"]) == 20
    with pytest.raises(Exception):  # invalid result is refused, file unchanged
        store.update_meta("m1", lambda m: m.update({"runs": {"c1": "bogus"}}))
    assert store.read_meta("m1")["runs"] == {}
    assert not list((env / "m1").glob(".meta.json.*.tmp"))
    assert store.update_meta("m1", lambda m: {**m, "job_title": None})["job_title"] is None
    with pytest.raises(FileNotFoundError):
        store.read_meta("nope")
