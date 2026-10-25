"""J2 fetch tests: cache modes, retries, rate limits, snapshots, LLM fallback, recorder CLI.

All offline: GitHub is an `httpx.MockTransport` serving `fixtures/snapshots/sample/raw` bodies.
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from verifier.contracts import registry
from verifier.contracts.errors import FetchError, LLMError, LLMSchemaError
from verifier.contracts.snapshot import Snapshot, cache_key
from verifier.core import fetch, match, store

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
SAMPLE = FIXTURES / "snapshots" / "sample"
HENRY = json.loads((FIXTURES / "claims" / "henrychen4736.json").read_text())
NOT_FOUND = (404, b'{"message": "Not Found"}')


# ── Test doubles ──────────────────────────────────────────────────────────────

class FakeTime:
    """One clock for monotonic time, wall time and sleeps: sleeping advances all of them."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def utcnow(self) -> datetime:
        return datetime(2026, 10, 25, 14, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=int(self.now - 1000))


class MockGitHub:
    """`httpx.MockTransport` handler keyed by cache_key; unknown keys are 404."""

    def __init__(self, table=None, headers=None):
        self.table = dict(table if table is not None else sample_table())
        self.requests: list[httpx.Request] = []
        self.responders: dict[str, object] = {}     # key -> callable(request, n) -> Response | raises
        self.counts: dict[str, int] = {}
        self.headers = headers or (lambda n: {})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = cache_key("GET", str(request.url))
        n = self.counts[key] = self.counts.get(key, 0) + 1
        if key in self.responders:
            return self.responders[key](request, n)
        status, body = self.table.get(key, NOT_FOUND)
        return httpx.Response(status, content=body,
                              headers={"ETag": '"e"', **self.headers(len(self.requests))})

    @property
    def keys(self) -> list[str]:
        return [cache_key("GET", str(r.url)) for r in self.requests]


def sample_table() -> dict[str, tuple[int, bytes]]:
    ev = json.loads((SAMPLE / "evidence.json").read_text())
    return {r["key"]: (r["status"], (SAMPLE / r["body_path"]).read_bytes()) for r in ev["responses"]}


@pytest.fixture
def ft(monkeypatch):
    t = FakeTime()
    monkeypatch.setattr(fetch, "_CLOCK", t.clock)
    monkeypatch.setattr(fetch, "_SLEEP", t.sleep)
    monkeypatch.setattr(fetch, "_utcnow", t.utcnow)
    return t


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("GITHUB_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("GITHUB_TOKEN", "")
    monkeypatch.setenv("CUT", "")
    monkeypatch.setenv("VERIFIER_REAL", "")
    return SimpleNamespace(cache=tmp_path / "cache", run=tmp_path / "run", tmp=tmp_path)


def serve(monkeypatch, gh: MockGitHub) -> MockGitHub:
    monkeypatch.setattr(fetch, "_TRANSPORT", httpx.MockTransport(gh))
    return gh


def claims_for(*claims, login="henrychen4736", app_id="42") -> dict:
    doc = copy.deepcopy(HENRY)
    doc["application_id"] = app_id
    doc["candidate"]["github_login"] = login
    doc["claims"] = list(claims)
    return doc


def project(claim_id="c1", repo_name=None, repo_url=None, text="a project") -> dict:
    return {"claim_id": claim_id, "type": "project", "section": "Projects", "source_text": text,
            "needs_review": False,
            "assertions": {"repo_url": repo_url, "repo_name": repo_name, "role": "creator",
                           "language": None, "min_stars": None, "start_year": None, "scale": None,
                           "public_signal": "none", "behavior": None}}


def oss(claim_id="c9", repo="Org/Repo", role="contributor") -> dict:
    return {"claim_id": claim_id, "type": "oss_contribution", "section": "OSS", "source_text": "oss",
            "needs_review": False,
            "assertions": {"repo": repo, "role": role, "min_prs": None, "description": None}}


def run(claims, env, mode="record") -> Snapshot:
    return fetch.fetch_evidence(claims, run_dir=env.run, mode=mode)


def cache_files(env) -> list[Path]:
    return sorted(env.cache.glob("*.json")) if env.cache.exists() else []


# ── Modes and cache ───────────────────────────────────────────────────────────

def test_record_writes_cache_and_snapshot(env, ft, monkeypatch):
    gh = serve(monkeypatch, MockGitHub())
    snap = run(HENRY, env)
    assert snap.evidence["fetch_mode"] == "record"
    assert set(gh.keys) == set(sample_table())          # keys are compatible with the sample snapshot
    assert len(cache_files(env)) == len(snap.evidence["responses"]) == 16
    assert snap.evidence["misses"] == []

    key = "GET https://api.github.com/users/henrychen4736"
    entry = json.loads((env.cache / f"{hashlib.sha256(key.encode()).hexdigest()}.json").read_text())
    assert set(entry) == {"key", "status", "etag", "fetched_at", "body_sha256", "body"}
    assert entry["key"] == key and entry["status"] == 200 and entry["etag"] == '"e"'
    assert hashlib.sha256(entry["body"].encode()).hexdigest() == entry["body_sha256"]
    assert snap.get("GET", fetch.user_url("HenryChen4736"))[0] == 200


def test_replay_reads_cache_only(env, ft, monkeypatch):
    serve(monkeypatch, MockGitHub())
    run(HENRY, env, "record")
    gh2 = serve(monkeypatch, MockGitHub())
    snap = run(HENRY, env, "replay")
    assert gh2.requests == []
    assert snap.evidence["fetch_mode"] == "replay" and len(snap.evidence["responses"]) == 16


def test_live_calls_github_and_leaves_cache_alone(env, ft, monkeypatch):
    gh = serve(monkeypatch, MockGitHub())
    snap = run(HENRY, env, "live")
    assert len(gh.requests) == 16 and len(snap.evidence["responses"]) == 16
    assert cache_files(env) == []


def test_record_then_replay_are_identical(env, ft, monkeypatch):
    serve(monkeypatch, MockGitHub())
    rec = run(HENRY, env, "record")
    ft.sleep(5)
    serve(monkeypatch, MockGitHub(table={}))             # replay must not need GitHub at all
    rep = run(HENRY, env, "replay")
    assert rec.snapshot_id != rep.snapshot_id
    for field in ("responses", "misses", "decisions", "github_login", "application_id"):
        assert rec.evidence[field] == rep.evidence[field]
    assert rec.evidence["misses"] == [] and rec.evidence["decisions"] == []
    assert (rec.evidence["fetch_mode"], rep.evidence["fetch_mode"]) == ("record", "replay")
    # same raw bytes behind the same keys
    for r in rec.evidence["responses"]:
        assert rec.get_raw(r["body_path"]) == rep.get_raw(r["body_path"])


def test_record_replaces_existing_cache_file(env, ft, monkeypatch):
    gh = serve(monkeypatch, MockGitHub())
    run(HENRY, env, "record")
    key = "GET https://api.github.com/users/henrychen4736"
    gh.table[key] = (200, b'{"login": "henrychen4736", "changed": true}')
    run(HENRY, env, "record")
    path = env.cache / f"{hashlib.sha256(key.encode()).hexdigest()}.json"
    assert json.loads(path.read_text())["body"].count("changed") == 1
    assert run(HENRY, env, "replay").get("GET", fetch.user_url("henrychen4736"))[1]["changed"] is True


def test_replay_against_empty_cache_gives_misses_not_a_crash(env, ft):
    snap = run(HENRY, env, "replay")
    assert snap.evidence["responses"] == []
    assert snap.evidence["misses"] == sorted(snap.evidence["misses"]) and snap.evidence["misses"]
    assert all(m.startswith("GET https://api.github.com/") for m in snap.evidence["misses"])
    assert snap.get("GET", fetch.user_url("henrychen4736")) is None


def test_corrupt_cache_file_raises_fetch_error(env, ft):
    env.cache.mkdir(parents=True)
    key = cache_key("GET", fetch.user_url("henrychen4736"))
    (env.cache / f"{hashlib.sha256(key.encode()).hexdigest()}.json").write_text("{not json")
    with pytest.raises(FetchError):
        run(HENRY, env, "replay")
    assert not (env.run / "snapshots").exists() or not list((env.run / "snapshots").iterdir())


def test_cache_body_hash_mismatch_raises_fetch_error(env, ft, monkeypatch):
    serve(monkeypatch, MockGitHub())
    run(HENRY, env, "record")
    path = next(iter(cache_files(env)))
    entry = json.loads(path.read_text())
    entry["body"] = '{"tampered": true}'
    path.write_text(json.dumps(entry))
    with pytest.raises(FetchError):
        run(HENRY, env, "replay")


def test_invalid_mode(env, ft):
    with pytest.raises(FetchError):
        run(HENRY, env, "bogus")


# ── Headers ───────────────────────────────────────────────────────────────────

def test_request_headers_with_and_without_token(env, ft, monkeypatch):
    gh = serve(monkeypatch, MockGitHub())
    monkeypatch.setenv("GITHUB_TOKEN", "tok123")
    run(HENRY, env, "live")
    for r in gh.requests:
        assert r.headers["authorization"] == "Bearer tok123"
        assert r.headers["accept"] == "application/vnd.github+json"
        assert r.headers["x-github-api-version"] == "2022-11-28"

    gh2 = serve(monkeypatch, MockGitHub())
    monkeypatch.setenv("GITHUB_TOKEN", "")
    run(HENRY, env, "live")
    assert gh2.requests and all("authorization" not in r.headers for r in gh2.requests)


def test_request_urls_are_lowercased(env, ft, monkeypatch):
    gh = serve(monkeypatch, MockGitHub())
    doc = claims_for(project(repo_name="Polytrack-ML"), login="HenryChen4736")
    run(doc, env, "live")
    assert "https://api.github.com/users/henrychen4736" in {str(r.url) for r in gh.requests}
    assert all(str(r.url) == str(r.url).lower() for r in gh.requests)


# ── Responses vs. misses ──────────────────────────────────────────────────────

def test_404_is_recorded_evidence(env, ft, monkeypatch):
    serve(monkeypatch, MockGitHub())
    doc = claims_for(project(repo_url="https://github.com/ghost/gone"))
    snap = run(doc, env, "record")
    got = snap.get("GET", fetch.repo_detail_url("ghost/gone"))
    assert got is not None and got[0] == 404
    assert cache_key("GET", fetch.repo_detail_url("ghost/gone")) not in snap.evidence["misses"]
    # the 404 decides "not found": no further requests for that repo
    assert snap.get("GET", fetch.commits_url("ghost/gone")) is None
    assert cache_key("GET", fetch.commits_url("ghost/gone")) not in snap.evidence["misses"]
    assert match.match_repo(doc["claims"][0], snap)["repo"] is None


@pytest.mark.parametrize("status", [409, 410])
def test_409_and_410_are_recorded(env, ft, monkeypatch, status):
    gh = serve(monkeypatch, MockGitHub())
    key = cache_key("GET", fetch.commits_url("henrychen4736/cubesolver"))
    gh.table[key] = (status, b'{"message": "Git Repository is empty."}')
    snap = run(claims_for(project(repo_name="cubesolver")), env, "record")
    assert snap.get("GET", fetch.commits_url("henrychen4736/cubesolver"))[0] == status


@pytest.mark.parametrize("status", [401, 403, 422, 429, 500, 502])
def test_other_statuses_are_misses(env, ft, monkeypatch, status):
    gh = serve(monkeypatch, MockGitHub())
    key = cache_key("GET", fetch.user_url("henrychen4736"))
    gh.table[key] = (status, b'{"message": "nope"}')
    snap = run(claims_for(), env, "record")
    assert key in snap.evidence["misses"]
    assert key not in [r["key"] for r in snap.evidence["responses"]]
    assert all(json.loads(p.read_text())["key"] != key for p in cache_files(env))


def test_timeout_is_a_miss_not_an_error(env, ft, monkeypatch):
    gh = serve(monkeypatch, MockGitHub())
    key = cache_key("GET", fetch.user_url("henrychen4736"))

    def boom(request, n):
        raise httpx.ConnectTimeout("slow", request=request)

    gh.responders[key] = boom
    snap = run(claims_for(), env, "record")
    assert key in snap.evidence["misses"] and gh.counts[key] == 2   # one retry


def test_5xx_retried_once_then_succeeds(env, ft, monkeypatch):
    gh = serve(monkeypatch, MockGitHub())
    key = cache_key("GET", fetch.user_url("henrychen4736"))
    table_ok = gh.table[key]
    gh.responders[key] = lambda req, n: (httpx.Response(503) if n == 1
                                         else httpx.Response(table_ok[0], content=table_ok[1]))
    snap = run(claims_for(), env, "record")
    assert snap.get("GET", fetch.user_url("henrychen4736"))[0] == 200 and gh.counts[key] == 2


def test_5xx_twice_is_a_miss(env, ft, monkeypatch):
    gh = serve(monkeypatch, MockGitHub())
    key = cache_key("GET", fetch.user_url("henrychen4736"))
    gh.responders[key] = lambda req, n: httpx.Response(502)
    snap = run(claims_for(), env, "record")
    assert key in snap.evidence["misses"] and gh.counts[key] == 2


def test_non_json_body_is_a_miss(env, ft, monkeypatch):
    gh = serve(monkeypatch, MockGitHub())
    key = cache_key("GET", fetch.user_url("henrychen4736"))
    gh.table[key] = (200, b"<html>captive portal</html>")
    snap = run(claims_for(), env, "record")
    assert key in snap.evidence["misses"]


def test_rate_limit_stop_turns_remaining_keys_into_misses(env, ft, monkeypatch):
    # Remaining count goes 2, 1, 0 on the first three calls; no calls after that.
    gh = serve(monkeypatch, MockGitHub(headers=lambda n: {"X-RateLimit-Remaining": str(max(0, 3 - n))}))
    snap = run(HENRY, env, "record")
    assert len(gh.requests) == 3
    assert len(snap.evidence["responses"]) == 3              # the call that hit 0 is still kept
    assert len(snap.evidence["misses"]) > 0
    assert not set(snap.evidence["misses"]) & {r["key"] for r in snap.evidence["responses"]}
    assert len(cache_files(env)) == 3


def test_429_stops_further_calls(env, ft, monkeypatch):
    gh = serve(monkeypatch, MockGitHub())
    gh.responders[cache_key("GET", fetch.user_url("henrychen4736"))] = lambda r, n: httpx.Response(429)
    snap = run(HENRY, env, "record")
    assert len(gh.requests) == 1 and len(snap.evidence["misses"]) > 1


def test_redirect_followed_and_cache_keyed_by_requested_url(env, ft, monkeypatch):
    gh = serve(monkeypatch, MockGitHub())
    old = cache_key("GET", fetch.repo_detail_url("o/old"))
    gh.responders[old] = lambda req, n: httpx.Response(
        301, headers={"Location": "https://api.github.com/repositories/777"})
    gh.table[cache_key("GET", "https://api.github.com/repositories/777")] = (
        200, b'{"full_name": "o/new", "size": 5}')
    snap = run(claims_for(project(repo_url="https://github.com/o/old")), env, "record")
    got = snap.get("GET", fetch.repo_detail_url("o/old"))
    assert got == (200, {"full_name": "o/new", "size": 5})
    assert any(str(r.url).endswith("/repositories/777") for r in gh.requests)
    assert not any("repositories/777" in k for k in [r["key"] for r in snap.evidence["responses"]])
    assert run(claims_for(project(repo_url="https://github.com/O/Old")), env, "replay").get(
        "GET", fetch.repo_detail_url("o/old"))[0] == 200


# ── Search spacing and oss requests ───────────────────────────────────────────

def test_search_calls_are_spaced_2_1_seconds(env, ft, monkeypatch):
    stamps: list[float] = []
    gh = serve(monkeypatch, MockGitHub())
    inner = gh.__call__

    def recording(request):
        if "/search/" in str(request.url):
            stamps.append(ft.now)
        return inner(request)

    monkeypatch.setattr(fetch, "_TRANSPORT", httpx.MockTransport(recording))
    run(claims_for(oss(role="maintainer"), oss("c10", "Org/Other")), env, "record")
    assert len(stamps) == 5                                    # 3 for the maintainer claim, 2 for the other
    assert all(b - a >= 2.1 - 1e-9 for a, b in zip(stamps, stamps[1:]))


def test_oss_requests_per_claim(env, ft, monkeypatch):
    gh = serve(monkeypatch, MockGitHub())
    snap = run(claims_for(oss(role="contributor")), env, "record")
    keys = set(gh.keys)
    expected = {
        cache_key("GET", match.merged_prs_url("org/repo", "henrychen4736")),
        cache_key("GET", match.any_prs_url("org/repo", "henrychen4736")),
        cache_key("GET", match.author_commits_url("org/repo", "henrychen4736")),
    }
    assert expected <= keys
    assert not any("reviewed-by" in k for k in keys)
    assert snap.get("GET", match.merged_prs_url("Org/Repo", "HenryChen4736"))[0] == 404


def test_oss_maintainer_adds_review_search(env, ft, monkeypatch):
    gh = serve(monkeypatch, MockGitHub())
    run(claims_for(oss(role="maintainer")), env, "record")
    assert sum("/search/" in k for k in gh.keys) == 3
    assert any("reviewed-by" in k for k in gh.keys)


def test_cut_oss_skips_the_requests(env, ft, monkeypatch):
    monkeypatch.setenv("CUT", "slack,oss")
    gh = serve(monkeypatch, MockGitHub())
    run(claims_for(oss()), env, "record")
    assert not any("org/repo" in k for k in gh.keys)


# ── Snapshot shape ────────────────────────────────────────────────────────────

def test_evidence_validates_is_sorted_and_stable(env, ft, monkeypatch):
    serve(monkeypatch, MockGitHub())
    snap = run(HENRY, env, "record")
    store.validate(snap.evidence, "evidence")
    keys = [r["key"] for r in snap.evidence["responses"]]
    assert keys == sorted(keys)
    for r in snap.evidence["responses"]:
        assert r["body_path"] == f"raw/{r['body_sha256']}.json"
        assert hashlib.sha256(snap.get_raw(r["body_path"])).hexdigest() == r["body_sha256"]
    on_disk = (snap.path / "evidence.json").read_bytes()
    assert on_disk == store.dump_json(snap.evidence)
    assert snap.snapshot_id == snap.path.name and snap.path.parent == env.run / "snapshots"
    assert snap.evidence["created_at"] == snap.snapshot_id[5:9] + "-" + snap.snapshot_id[9:11] + "-" \
        + snap.snapshot_id[11:13] + "T" + snap.snapshot_id[14:16] + ":" + snap.snapshot_id[16:18] + ":" \
        + snap.snapshot_id[18:20] + "Z"
    with pytest.raises(Exception):                            # write-once: evidence.json can't be replaced
        store.write_json(snap.path / "evidence.json", {**snap.evidence, "misses": ["x"]})


def test_misses_sorted(env, ft):
    snap = run(HENRY, env, "replay")
    assert snap.evidence["misses"] == sorted(set(snap.evidence["misses"]))


def test_no_login_snapshot(env, ft, monkeypatch):
    gh = serve(monkeypatch, MockGitHub())
    doc = claims_for(project(repo_name="x"), login=None)
    snap = run(doc, env, "record")
    store.validate(snap.evidence, "evidence")
    assert snap.evidence["github_login"] is None
    assert snap.evidence["responses"] == [] and snap.evidence["misses"] == [] and snap.evidence["decisions"] == []
    assert gh.requests == [] and cache_files(env) == []


def test_taken_second_waits_a_second(env, ft, monkeypatch):
    serve(monkeypatch, MockGitHub())
    first = run(claims_for(login=None), env)
    second = run(claims_for(login=None), env)
    assert first.snapshot_id != second.snapshot_id
    assert 1.0 in ft.sleeps
    assert second.snapshot_id == "snap_20261025T140001Z"


def test_local_failure_raises_fetch_error(env, ft):
    env.run.parent.mkdir(parents=True, exist_ok=True)
    env.run.write_text("a file, not a folder")
    with pytest.raises(FetchError):
        run(HENRY, env, "replay")


# ── LLM fallback ──────────────────────────────────────────────────────────────

def fake_llm(monkeypatch, reply=None, exc=None):
    calls: list[dict] = []

    def llm_chat(messages, schema=None, *, temperature=0.0, max_tokens=2048, **kw):
        calls.append({"messages": messages, "schema": schema, "temperature": temperature})
        if exc:
            raise exc
        return {"text": json.dumps(reply), "json": reply, "model": "fake-model", "duration_s": 0.0,
                "usage": {"prompt_tokens": 0, "completion_tokens": 0}}

    real_get = registry.get
    monkeypatch.setattr(fetch.registry, "get",
                        lambda name: SimpleNamespace(llm_chat=llm_chat) if name == "llm" else real_get(name))
    return calls


FUZZY = project("c3", repo_name="Rubik solver", text="Built a Rubik's cube solving program")


def test_llm_fallback_records_decision_and_fetches_the_repo(env, ft, monkeypatch):
    gh = serve(monkeypatch, MockGitHub())
    calls = fake_llm(monkeypatch, {"repo": "cubesolver"})
    snap = run(claims_for(FUZZY), env, "record")

    assert len(calls) == 1 and calls[0]["temperature"] == 0.0
    enum = calls[0]["schema"]["properties"]["repo"]["enum"]
    assert enum[-1] == "none" and "cubesolver" in enum and len(enum) == 6
    prompt = "\n".join(m["content"] for m in calls[0]["messages"])
    assert "Built a Rubik's cube solving program" in prompt and "cubesolver" in prompt

    [d] = snap.evidence["decisions"]
    assert d["kind"] == "repo_match" and d["claim_id"] == "c3" and d["model"] == "fake-model"
    assert d["prompt_version"] == "match-v1" and d["output"] == "cubesolver"
    assert d["inputs"]["claimed"] == "Built a Rubik's cube solving program"
    assert sorted(d["inputs"]["candidates"]) == sorted(r["name"] for r in snap.get(
        "GET", fetch.repos_list_url("henrychen4736"))[1])
    assert cache_key("GET", fetch.commits_url("henrychen4736/cubesolver")) in gh.keys
    assert match.match_repo(FUZZY, snap)["method"] == "llm"


def test_llm_answer_outside_the_enum_counts_as_none(env, ft, monkeypatch):
    gh = serve(monkeypatch, MockGitHub())
    fake_llm(monkeypatch, {"repo": "not-a-repo"})
    snap = run(claims_for(FUZZY), env, "record")
    assert [d["output"] for d in snap.evidence["decisions"]] == ["none"]
    assert cache_key("GET", fetch.commits_url("henrychen4736/cubesolver")) not in gh.keys
    assert match.match_repo(FUZZY, snap)["repo"] is None


@pytest.mark.parametrize("exc", [LLMError("down"), LLMSchemaError("bad", "oops"), RuntimeError("boom")])
def test_failed_llm_call_records_nothing(env, ft, monkeypatch, exc):
    serve(monkeypatch, MockGitHub())
    fake_llm(monkeypatch, exc=exc)
    snap = run(claims_for(FUZZY), env, "record")
    assert snap.evidence["decisions"] == []


def test_llm_not_called_for_url_or_exact_name_or_other_types(env, ft, monkeypatch):
    serve(monkeypatch, MockGitHub())
    calls = fake_llm(monkeypatch, {"repo": "none"})
    run(claims_for(project("c1", repo_name="CUBESOLVER"),
                   project("c2", repo_url="https://github.com/o/r"), oss()), env, "record")
    assert calls == []


def test_llm_called_for_project_with_no_name_and_no_url(env, ft, monkeypatch):
    serve(monkeypatch, MockGitHub())
    calls = fake_llm(monkeypatch, {"repo": "none"})
    run(claims_for(project("c1", text="a payments thing")), env, "record")
    assert len(calls) == 1


def test_llm_not_called_when_repo_list_is_unavailable(env, ft, monkeypatch):
    calls = fake_llm(monkeypatch, {"repo": "none"})
    snap = run(claims_for(FUZZY), env, "replay")               # empty cache: repo list is a miss
    assert calls == [] and snap.evidence["decisions"] == []


def test_stub_llm_gives_a_none_decision(env, ft, monkeypatch):
    serve(monkeypatch, MockGitHub())
    snap = run(claims_for(FUZZY), env, "record")               # registry resolves the llm stub
    assert [(d["output"], d["model"]) for d in snap.evidence["decisions"]] == [("none", "stub")]


def test_decisions_follow_claim_order(env, ft, monkeypatch):
    serve(monkeypatch, MockGitHub())
    fake_llm(monkeypatch, {"repo": "cac24"})
    a, b = project("c7", text="alpha"), project("c2", text="beta")
    snap = run(claims_for(a, b), env, "record")
    assert [d["claim_id"] for d in snap.evidence["decisions"]] == ["c7", "c2"]


# ── Top-30 selection ──────────────────────────────────────────────────────────

def test_languages_for_the_30_most_recent_non_fork_repos(env, ft, monkeypatch):
    repos = [{"name": f"r{i:02d}", "fork": i % 5 == 0, "pushed_at": f"2026-01-{(i % 28) + 1:02d}T00:00:00Z"}
             for i in range(45)]
    gh = serve(monkeypatch, MockGitHub(table={
        cache_key("GET", fetch.repos_list_url("u")): (200, json.dumps(repos).encode())}))
    run(claims_for(login="u"), env, "live")
    langs = [k for k in gh.keys if k.endswith("/languages")]
    assert len(langs) == 30
    owned = sorted((r for r in repos if not r["fork"]), key=lambda r: r["pushed_at"], reverse=True)[:30]
    assert {k.split("/")[-2] for k in langs} == {r["name"] for r in owned}


# ── Recorder CLI ──────────────────────────────────────────────────────────────

@pytest.fixture
def cli(env, ft, monkeypatch):
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    gh = serve(monkeypatch, MockGitHub())
    gits: list[list[str]] = []

    def fake_git(args, cwd=None):
        gits.append(args)
        if args[0] == "clone":
            Path(args[-1]).mkdir(parents=True)
            return ""
        if args[0] == "rev-parse":
            return "0123456789abcdef0123456789abcdef01234567"
        if args[0] == "archive":
            Path(args[args.index("-o") + 1]).write_bytes(b"fake-tarball")
            return ""
        raise AssertionError(args)

    monkeypatch.setattr(fetch, "_git", fake_git)
    repos_dir = env.tmp / "repos"
    return SimpleNamespace(gh=gh, gits=gits, repos=repos_dir, env=env)


def record_args(cli, *extra):
    return ["record", "--login", "henrychen4736", "--claims", str(FIXTURES / "claims" / "henrychen4736.json"),
            "--cache-dir", str(cli.env.cache), "--repos-dir", str(cli.repos), *extra]


def test_cli_record_fills_cache_and_prints_summary(cli, capsys):
    assert fetch.main(record_args(cli)) == 0
    out = capsys.readouterr().out
    assert "WITHOUT GITHUB_TOKEN" in out and "misses" in out and "https://api.github.com/users/henrychen4736" in out
    assert all("authorization" not in r.headers for r in cli.gh.requests)
    # every key requested once; superset: detail/contributors/commits for all 5 top repos
    assert len(cli.gh.keys) == len(set(cli.gh.keys)) == 16 + 2 * 3
    assert len(cache_files(cli.env)) == len(cli.gh.keys)
    assert cli.gits == [] and not cli.repos.exists()
    # the recording replays through fetch_evidence
    snap = run(HENRY, cli.env, "replay")
    assert snap.evidence["misses"] == [] and len(snap.evidence["responses"]) == 16


def test_cli_login_repeated_and_from_claims_are_deduped(cli, capsys):
    argv = record_args(cli) + ["--login", "HenryChen4736"]
    assert fetch.main(argv) == 0
    assert len(cli.gh.keys) == len(set(cli.gh.keys))


def test_cli_clone_writes_archives_and_metadata(cli, capsys):
    assert fetch.main(record_args(cli, "--clone")) == 0
    names = sorted(p.name for p in cli.repos.iterdir())
    assert names == ["henrychen4736__cac24.json", "henrychen4736__cac24.tar.gz",
                     "henrychen4736__cubesolver.json", "henrychen4736__cubesolver.tar.gz",
                     "henrychen4736__polytrack-ml.json", "henrychen4736__polytrack-ml.tar.gz"]
    meta = json.loads((cli.repos / "henrychen4736__polytrack-ml.json").read_text())
    assert meta["repo"] == "henrychen4736/Polytrack-ML" and meta["commit_sha"].startswith("0123456789")
    assert meta["cloned_at"].endswith("Z")
    clones = [a for a in cli.gits if a[0] == "clone"]
    assert len(clones) == 3 and all("--depth" in a and "1" in a for a in clones)


def test_cli_clone_skips_existing_unless_forced(cli, capsys):
    cli.repos.mkdir()
    existing = cli.repos / "henrychen4736__cubesolver.tar.gz"
    existing.write_bytes(b"original")
    assert fetch.main(record_args(cli, "--clone")) == 0
    assert existing.read_bytes() == b"original"
    assert not (cli.repos / "henrychen4736__cubesolver.json").exists()
    assert "already exists" in capsys.readouterr().out
    assert fetch.main(record_args(cli, "--clone", "--force")) == 0
    assert existing.read_bytes() == b"fake-tarball"


def test_cli_clone_skips_oversized_repos(cli, capsys):
    key = cache_key("GET", fetch.repo_detail_url("henrychen4736/cubesolver"))
    body = json.loads(cli.gh.table[key][1])
    body["size"] = 60 * 1024
    cli.gh.table[key] = (200, json.dumps(body).encode())
    assert fetch.main(record_args(cli, "--clone")) == 0
    assert not (cli.repos / "henrychen4736__cubesolver.tar.gz").exists()
    assert (cli.repos / "henrychen4736__cac24.tar.gz").exists()
    assert "50 MB" in capsys.readouterr().out


def test_cli_extra_repo_gets_requests_and_clone(cli, capsys):
    argv = ["record", "--login", "someone", "--repo", "Org/Big-Thing", "--clone",
            "--cache-dir", str(cli.env.cache), "--repos-dir", str(cli.repos)]
    assert fetch.main(argv) == 0
    assert cache_key("GET", fetch.commits_url("org/big-thing")) in cli.gh.keys
    assert (cli.repos / "org__big-thing.tar.gz").exists()


def test_cli_missing_claims_file_fails_cleanly(cli, capsys):
    argv = ["record", "--claims", str(cli.env.tmp / "nope.json"), "--cache-dir", str(cli.env.cache)]
    assert fetch.main(argv) == 1
    assert "FAILED" in capsys.readouterr().err


def test_cli_with_token_sends_authorization(cli, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "sekret")
    assert fetch.main(record_args(cli)) == 0
    assert all(r.headers["authorization"] == "Bearer sekret" for r in cli.gh.requests)
