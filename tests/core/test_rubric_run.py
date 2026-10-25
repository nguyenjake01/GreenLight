"""The rubric's `run` check (design doc J10): mapping a RunTrace to check result, flags and the cap.

The run check can only add a soft result and a flag. It never makes a claim 🔴 and never upgrades
a label (D13).
"""
from __future__ import annotations

import copy
from typing import Any

import pytest

from verifier.core import rubric, store
from tests.core.test_rubric import World, check, doc, names, only, project, verdict

COMPUTED = "2026-10-25T14:06:40Z"


def baseline(step: str = "build", *, exit_code: int | None = 0, duration: float = 3.1,
             timed_out: bool = False, failure_kind: str | None = None,
             excerpt: str = "") -> dict[str, Any]:
    return {"step": step, "command": None if step == "skip" else "make", "exit_code": exit_code,
            "duration_s": duration, "timed_out": timed_out, "failure_kind": failure_kind,
            "output_excerpt": excerpt, "output_path": None}


def agent(stopped: str = "done") -> dict[str, Any]:
    return {"model": "m", "prompt_version": "run-v1", "limits": {"max_commands": 15, "timeout_s": 300},
            "stopped": stopped, "timed_out": stopped == "timeout", "commands": []}


def judge(outcome: str, rationale: str = "because") -> dict[str, Any]:
    return {"model": "m", "prompt_version": "judge-v1", "outcome": outcome, "rationale": rationale}


def trace(base: dict[str, Any] | None = None, *, ag: dict[str, Any] | None = None,
          jd: dict[str, Any] | None = None, claim_id: str = "c1") -> dict[str, Any]:
    return {"schema_version": "1", "claim_id": claim_id, "repo": "alice/proj",
            "commit_sha": "7f" * 20, "sandbox_backend": "stub", "baseline": base or baseline(),
            "agent": ag, "judge": jd}


def run_of(tmp_path, tr: dict[str, Any], **proj_kw: Any):
    """(verdict claim, run check) for a clean project with the given trace."""
    w = World(tmp_path)
    w.repo("proj")
    vc = only(verdict(tmp_path, [project(**proj_kw)], w, traces=[tr]))
    return vc, check(vc, "run")


# ── One test per table row ───────────────────────────────────────────────────

def test_row1_baseline_timeout(tmp_path):
    tr = trace(baseline("build", exit_code=None, duration=120.04, timed_out=True,
                        failure_kind="error"), jd=judge("works_as_claimed"))
    vc, chk = run_of(tmp_path, tr)
    assert (chk["result"], chk["reason"]) == ("soft", "Timed out after 120s during build")
    assert vc["flags"] == ["run_timeout"] and vc["label"] == "partial"


@pytest.mark.parametrize("step, word", [("build", "Build failed"), ("tests", "Tests failed"),
                                        ("syntax", "Syntax check failed")])
def test_row2_baseline_failed_error(tmp_path, step, word):
    tr = trace(baseline(step, exit_code=1, failure_kind="error",
                        excerpt="compiling...\nmain.cpp:3: error: expected ';'\nmake: *** [all] Error 1\n"))
    vc, chk = run_of(tmp_path, tr)
    assert chk["result"] == "soft"
    assert chk["reason"] == f"{word}: main.cpp:3: error: expected ';'"
    assert vc["flags"] == ["run_failed"] and vc["label"] == "partial"


def test_row2_first_error_line_keywords(tmp_path):
    for line in ("Traceback (most recent call last):", "fatal: bad thing", "UnhandledException x",
                 "3 FAILED", "ERROR collecting"):
        tr = trace(baseline("tests", exit_code=1, failure_kind="error",
                            excerpt=f"ok line\n{line}\nlast line\n"))
        _, chk = run_of(tmp_path, tr)
        assert chk["reason"] == f"Tests failed: {line}"


def test_row2_no_error_word_uses_last_nonempty_line(tmp_path):
    tr = trace(baseline("build", exit_code=2, failure_kind="error", excerpt="first\nsecond\n\n"))
    _, chk = run_of(tmp_path, tr)
    assert chk["reason"] == "Build failed: second"


def test_row2_empty_output(tmp_path):
    tr = trace(baseline("build", exit_code=2, failure_kind="error", excerpt=""))
    _, chk = run_of(tmp_path, tr)
    assert chk["reason"] == "Build failed"


def test_row2_error_line_cut_to_120(tmp_path):
    tr = trace(baseline("build", exit_code=1, failure_kind="error", excerpt="error: " + "x" * 300))
    _, chk = run_of(tmp_path, tr)
    assert chk["reason"] == "Build failed: " + ("error: " + "x" * 300)[:120]


def test_row3_runs_but_mismatch(tmp_path):
    tr = trace(jd=judge("runs_but_mismatch", "It printed hello instead of a solution."))
    vc, chk = run_of(tmp_path, tr)
    assert chk["result"] == "soft"
    assert chk["reason"] == "Ran, but the output didn't match the claim: It printed hello instead of a solution."
    assert vc["flags"] == ["run_failed"] and vc["label"] == "partial"


def test_row3_rationale_cut_to_120(tmp_path):
    tr = trace(jd=judge("runs_but_mismatch", "y" * 400))
    _, chk = run_of(tmp_path, tr)
    assert chk["reason"].endswith("y" * 120) and not chk["reason"].endswith("y" * 121)


def test_row4_failed_to_run(tmp_path):
    tr = trace(ag=agent("done"), jd=judge("failed_to_run", "Binary crashed on start."))
    vc, chk = run_of(tmp_path, tr)
    assert (chk["result"], chk["reason"]) == ("soft", "Failed to run: Binary crashed on start.")
    assert vc["flags"] == ["run_failed"]


def test_row4_failed_to_run_after_agent_timeout(tmp_path):
    tr = trace(ag=agent("timeout"), jd=judge("failed_to_run", "Hung."))
    vc, _ = run_of(tmp_path, tr)
    assert vc["flags"] == ["run_timeout"]


@pytest.mark.parametrize("step, summary", [
    ("build", "Built in 3.1s; works as claimed"),
    ("tests", "Tests passed in 3.1s; works as claimed"),
    ("syntax", "Syntax check passed; works as claimed"),
    ("skip", "Works as claimed"),
])
def test_row5_works_as_claimed(tmp_path, step, summary):
    base = baseline(step, exit_code=None if step == "skip" else 0)
    vc, chk = run_of(tmp_path, trace(base, ag=agent(), jd=judge("works_as_claimed")))
    assert (chk["result"], chk["reason"]) == ("pass", summary)
    assert vc["flags"] == [] and vc["label"] == "supported"


def test_row6_baseline_passed_no_judge(tmp_path):
    for step, text in (("build", "Built in 3.1s"), ("tests", "Tests passed in 3.1s"),
                       ("syntax", "Syntax check passed")):
        vc, chk = run_of(tmp_path, trace(baseline(step)))
        assert (chk["result"], chk["reason"]) == ("pass", text)
        assert vc["flags"] == [] and vc["label"] == "supported"


def test_row7_baseline_passed_judge_could_not_attempt(tmp_path):
    tr = trace(baseline("tests"), ag=agent(), jd=judge("could_not_attempt", "needs a GPU"))
    vc, chk = run_of(tmp_path, tr)
    assert (chk["result"], chk["reason"]) == (
        "pass", "Tests passed in 3.1s; couldn't test the behavior here")
    assert vc["label"] == "supported"


@pytest.mark.parametrize("fk, why", [("deps_missing", "missing dependencies"),
                                     ("needs_network", "needs network")])
def test_row8_could_not_run_here_baseline(tmp_path, fk, why):
    tr = trace(baseline("tests", exit_code=1, failure_kind=fk,
                        excerpt="ModuleNotFoundError: No module named 'torch'"))
    vc, chk = run_of(tmp_path, tr)
    assert (chk["result"], chk["reason"]) == ("skip", f"Could not run here: {why}")
    assert vc["flags"] == [] and vc["label"] == "supported"


def test_row8_could_not_attempt_without_baseline(tmp_path):
    tr = trace(baseline("skip", exit_code=None), ag=agent(), jd=judge("could_not_attempt", "No entry point."))
    vc, chk = run_of(tmp_path, tr)
    assert (chk["result"], chk["reason"]) == ("skip", "Could not run here: No entry point.")
    assert vc["flags"] == []


def test_row9_nothing_to_build_or_run(tmp_path):
    vc, chk = run_of(tmp_path, trace(baseline("skip", exit_code=None)))
    assert (chk["result"], chk["reason"]) == ("skip", "Nothing to build or run")
    assert vc["flags"] == [] and vc["label"] == "supported"


def test_row9_agent_without_judge_and_no_baseline(tmp_path):
    vc, chk = run_of(tmp_path, trace(baseline("skip", exit_code=None), ag=agent("error")))
    assert chk["result"] == "skip" and chk["reason"] == "Nothing to build or run"


# ── Precedence ───────────────────────────────────────────────────────────────

def test_failed_baseline_stays_flagged_when_judge_says_works(tmp_path):
    tr = trace(baseline("build", exit_code=1, failure_kind="error", excerpt="error: boom"),
               ag=agent(), jd=judge("works_as_claimed"))
    vc, chk = run_of(tmp_path, tr)
    assert chk["result"] == "soft" and chk["reason"] == "Build failed: error: boom"
    assert vc["flags"] == ["run_failed"] and vc["label"] == "partial"


def test_timeout_beats_everything(tmp_path):
    tr = trace(baseline("tests", exit_code=None, timed_out=True, duration=120.0, failure_kind="error"),
               ag=agent(), jd=judge("runs_but_mismatch"))
    vc, chk = run_of(tmp_path, tr)
    assert chk["reason"] == "Timed out after 120s during tests" and vc["flags"] == ["run_timeout"]


def test_baseline_error_beats_mismatch_and_failed_to_run(tmp_path):
    base = baseline("build", exit_code=1, failure_kind="error", excerpt="error: x")
    _, chk = run_of(tmp_path, trace(base, jd=judge("runs_but_mismatch")))
    assert chk["reason"].startswith("Build failed")
    _, chk = run_of(tmp_path, trace(base, ag=agent("timeout"), jd=judge("failed_to_run")))
    assert chk["reason"].startswith("Build failed")


def test_mismatch_beats_works_even_with_deps_missing_baseline(tmp_path):
    base = baseline("tests", exit_code=1, failure_kind="deps_missing")
    _, chk = run_of(tmp_path, trace(base, jd=judge("runs_but_mismatch", "nope")))
    assert chk["result"] == "soft"


def test_works_after_missing_deps_baseline_does_not_claim_a_passed_baseline(tmp_path):
    base = baseline("tests", exit_code=1, failure_kind="deps_missing")
    _, chk = run_of(tmp_path, trace(base, ag=agent(), jd=judge("works_as_claimed")))
    assert (chk["result"], chk["reason"]) == ("pass", "Works as claimed")


# ── The cap: never 🔴, never an upgrade ──────────────────────────────────────

SOFT_TRACES = [
    trace(baseline("build", exit_code=1, failure_kind="error", excerpt="error: x")),
    trace(jd=judge("runs_but_mismatch")),
    trace(jd=judge("failed_to_run")),
    trace(baseline("build", exit_code=None, timed_out=True, duration=120.0)),
]
OK_TRACES = [
    trace(jd=judge("works_as_claimed")), trace(),
    trace(baseline("skip", exit_code=None)),
    trace(baseline("tests", exit_code=1, failure_kind="needs_network")),
]


@pytest.mark.parametrize("tr", SOFT_TRACES)
def test_failed_run_caps_supported_at_partial_with_flag(tmp_path, tr):
    vc, chk = run_of(tmp_path, tr, role="creator", language="Python")
    assert chk["result"] == "soft"
    assert vc["label"] == "partial"
    assert vc["flags"] and set(vc["flags"]) <= {"run_failed", "run_timeout"}


@pytest.mark.parametrize("tr", SOFT_TRACES + OK_TRACES)
def test_run_check_never_creates_red(tmp_path, tr):
    vc, chk = run_of(tmp_path, tr)
    assert chk["result"] in ("pass", "soft", "skip")
    assert vc["label"] != "contradicted"


@pytest.mark.parametrize("tr", SOFT_TRACES + OK_TRACES)
def test_run_check_never_upgrades_a_hard(tmp_path, tr):
    w = World(tmp_path)
    w.repo("proj", stars=0)
    vc = only(verdict(tmp_path, [project(min_stars=100)], w, traces=[tr]))
    assert vc["label"] == "contradicted"


@pytest.mark.parametrize("tr", SOFT_TRACES + OK_TRACES)
def test_run_check_never_upgrades_a_soft(tmp_path, tr):
    w = World(tmp_path)
    w.repo("proj", stars=60)
    vc = only(verdict(tmp_path, [project(min_stars=100)], w, traces=[tr]))
    assert vc["label"] == "partial"


def test_run_pass_does_not_cancel_a_soft_check(tmp_path):
    w = World(tmp_path)
    w.repo("proj", size=3, commits=2)
    tr = trace(jd=judge("works_as_claimed"))
    vc = only(verdict(tmp_path, [project(scale="substantial")], w, traces=[tr]))
    assert check(vc, "substance")["result"] == "soft"
    assert check(vc, "run")["result"] == "pass"
    assert vc["label"] == "partial"
    assert vc["top_reason"].startswith("Described as a complete project")


def test_flag_only_on_the_run_claim(tmp_path):
    w = World(tmp_path)
    w.repo("a")
    w.repo("b")
    claims = [project("c1", "a"), project("c2", "b")]
    v = verdict(tmp_path, claims, w, traces=[trace(jd=judge("failed_to_run"), claim_id="c2")])
    assert [c["flags"] for c in v["claims"]] == [[], ["run_failed"]]
    assert [c["label"] for c in v["claims"]] == ["supported", "partial"]


# ── Placement ────────────────────────────────────────────────────────────────

def test_run_check_is_last(tmp_path):
    w = World(tmp_path)
    w.repo("proj", size=3, commits=2)
    vc = only(verdict(tmp_path, [project(role="creator", language="Python", min_stars=0,
                                         start_year=2024, scale="substantial")], w, traces=[trace()]))
    assert names(vc)[-1] == "run" and names(vc)[:2] == ["repo", "fork"]


def test_no_trace_no_run_check(tmp_path):
    w = World(tmp_path)
    w.repo("proj")
    assert "run" not in names(only(verdict(tmp_path, [project()], w)))


def test_trace_for_another_claim_is_ignored(tmp_path):
    w = World(tmp_path)
    w.repo("proj")
    vc = only(verdict(tmp_path, [project("c1")], w, traces=[trace(claim_id="c2")]))
    assert "run" not in names(vc)


def test_no_run_check_when_repo_did_not_pass(tmp_path):
    w = World(tmp_path)
    w.repo("other")
    w.decide("c1")
    vc = only(verdict(tmp_path, [project("c1", "ghost", public_signal="link")], w, traces=[trace()]))
    assert names(vc) == ["repo"] and vc["label"] == "contradicted" and vc["flags"] == []


def test_trace_hash_recorded_in_inputs(tmp_path):
    w = World(tmp_path)
    w.repo("proj")
    tr = trace(jd=judge("works_as_claimed"))
    d = doc([project()], w.login)
    v = rubric.compute_verdict(d, w.build(), [tr], verdict_n=1, computed_at=COMPUTED)
    assert v["inputs"]["run_traces"] == [{"claim_id": "c1", "sha256": store.sha256_json(tr)}]


def test_run_check_does_not_mutate_trace(tmp_path):
    tr = trace(baseline("build", exit_code=1, failure_kind="error", excerpt="error: x"))
    before = copy.deepcopy(tr)
    run_of(tmp_path, tr)
    assert tr == before


def test_fixture_trace_c3_passes_cleanly():
    """The hand-made cubesolver trace (build ok, judge works_as_claimed) is a plain pass."""
    import json
    from pathlib import Path
    tr = json.loads((Path(__file__).resolve().parents[2] / "fixtures" / "run_traces" / "c3.json")
                    .read_text(encoding="utf-8"))
    chk, flags = rubric._run_check(tr)
    assert (chk["result"], chk["reason"]) == ("pass", "Built in 3.1s; works as claimed")
    assert flags == []


def test_helper_traces_validate_against_the_schema():
    for tr in (trace(), trace(baseline("tests", exit_code=1, failure_kind="error"), ag=agent("timeout"),
                              jd=judge("failed_to_run"))):
        store.validate(tr, "run_trace")
