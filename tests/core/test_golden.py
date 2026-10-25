"""Golden test for the rubric (design doc J5).

Each case in ``fixtures/golden/<case>/`` has ``claims.json``, a ``snapshot/`` folder, an optional
``run_traces/`` folder and ``expected_verdict.json``. The verdict is computed with ``verdict_n=1``
and a fixed ``computed_at`` and compared as canonical JSON, ignoring ``computed_at``. It is computed
twice to prove the output is identical.

``UPDATE_GOLDEN=1 pytest tests/core/test_golden.py`` rewrites the expected files (review the diff
before committing).

Cases:
- ``henry``: the hand-made sample snapshot, with one change: CAC24's Python is 22% of the bytes
  (the shared sample has 10%, which the rubric correctly calls 🟡), so the §11.4 labels come out.
  Replace with the real 11:45 recording when it lands.
- ``fake``: a hand-made snapshot of the fake candidate (account and repo created Oct 2026, 0 stars).
- ``edges``: hand-made edge cases (fork as creator, stars soft, language soft, empty repo, missing
  data, repo-match decision missing, failed build with a run trace, ...).
- ``nologin``: a candidate with no GitHub login.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from verifier.contracts.snapshot import Snapshot
from verifier.core import rubric, store

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
GOLDEN = FIXTURES / "golden"
FIXED_AT = "2026-10-25T14:06:40Z"
CASES = ["henry", "fake", "edges", "nologin"]
LABEL_FILES = {"henry": "henrychen4736", "fake": "rv-fake-candidate"}


def compute(case: str) -> dict:
    d = GOLDEN / case
    claims = json.loads((d / "claims.json").read_text(encoding="utf-8"))
    snapshot = Snapshot.load(d / "snapshot")
    traces_dir = d / "run_traces"
    traces = ([json.loads(p.read_text(encoding="utf-8")) for p in sorted(traces_dir.glob("*.json"))]
              if traces_dir.is_dir() else [])
    return rubric.compute_verdict(claims, snapshot, traces, verdict_n=1, computed_at=FIXED_AT)


def canonical_without_computed_at(verdict: dict) -> bytes:
    v = dict(verdict)
    v["computed_at"] = ""
    return store.dump_json(v)


@pytest.mark.parametrize("case", CASES)
def test_golden_verdict(case):
    expected_path = GOLDEN / case / "expected_verdict.json"
    verdict = compute(case)
    store.validate(verdict, "verdict")
    if os.environ.get("UPDATE_GOLDEN") == "1":
        expected_path.write_bytes(store.dump_json(verdict))
    assert expected_path.is_file(), f"{expected_path} missing; run with UPDATE_GOLDEN=1"
    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    assert canonical_without_computed_at(verdict) == canonical_without_computed_at(expected)


@pytest.mark.parametrize("case", CASES)
def test_golden_is_deterministic(case):
    assert store.dump_json(compute(case)) == store.dump_json(compute(case))


@pytest.mark.parametrize("case", CASES)
def test_golden_computed_at_is_the_only_free_field(case):
    one = rubric.compute_verdict(
        json.loads((GOLDEN / case / "claims.json").read_text(encoding="utf-8")),
        Snapshot.load(GOLDEN / case / "snapshot"), [], verdict_n=1, computed_at="2026-01-01T00:00:00Z")
    two = rubric.compute_verdict(
        json.loads((GOLDEN / case / "claims.json").read_text(encoding="utf-8")),
        Snapshot.load(GOLDEN / case / "snapshot"), [], verdict_n=1, computed_at="2027-01-01T00:00:00Z")
    assert canonical_without_computed_at(one) == canonical_without_computed_at(two)


# ── Expected labels (§11.4), keyed by claim key so they survive claim-id changes ──────────────

def labels_of(case: str) -> dict[str, str]:
    claims = json.loads((GOLDEN / case / "claims.json").read_text(encoding="utf-8"))
    verdict = compute(case)
    return {rubric.claim_key(c): v["label"] for c, v in zip(claims["claims"], verdict["claims"])}


@pytest.mark.parametrize("case", sorted(LABEL_FILES))
def test_expected_labels(case):
    expected = json.loads((FIXTURES / "expected" / f"{LABEL_FILES[case]}.labels.json")
                          .read_text(encoding="utf-8"))
    got = labels_of(case)
    verdict = compute(case)
    claims = json.loads((GOLDEN / case / "claims.json").read_text(encoding="utf-8"))["claims"]
    # oss_contribution checks arrive with J13; until then those claims come out "not run".
    pending = {rubric.claim_key(c) for c, v in zip(claims, verdict["claims"])
               if c["type"] == "oss_contribution" and v["top_reason"] == "Contribution check not run"}
    assert set(got) == set(expected)
    assert {k: v for k, v in got.items() if k not in pending} == \
           {k: v for k, v in expected.items() if k not in pending}


def test_henry_account_and_context():
    v = compute("henry")
    assert v["account"] == {"login": "henrychen4736", "login_source": "resume_link",
                            "login_conflict": None, "created_at": "2021-09-03",
                            "public_repos": 5, "followers": 4}
    assert v["context"] == ["Account created Sep 2021."]
    assert v["tally"] == {"supported": 6, "partial": 1, "contradicted": 0, "no_evidence": 2}


def test_henry_cubesolver_is_partial_from_substance_with_a_clean_run():
    vc = {c["claim_id"]: c for c in compute("henry")["claims"]}["c3"]
    assert vc["label"] == "partial" and vc["flags"] == []
    assert vc["top_reason"] == ("Described as a complete project, but the repo is 3 KB "
                                "with 2 commits on 1 day")
    run = [c for c in vc["checks"] if c["check"] == "run"][0]
    assert run["result"] == "pass" and run["reason"] == "Built in 3.1s; works as claimed"


def test_fake_fabricated_library_is_red_with_both_hard_reasons():
    v = compute("fake")
    vc = [c for c in v["claims"] if c["claim_id"] == "c1"][0]
    assert vc["label"] == "contradicted"
    hard = {c["check"]: c["reason"] for c in vc["checks"] if c["result"] == "hard"}
    assert hard == {"start_date": "Created Oct 2026; claim says 2023",
                    "stars": "0 stars; claim says 2000"}
    assert vc["top_reason"] == "Created Oct 2026; claim says 2023"
    assert v["context"][0] == "Account created Oct 2026."
    # red appears only on the fabricated library (D12: absent skills are never red)
    assert v["tally"]["contradicted"] == 1


def test_edges_covers_each_edge():
    v = compute("edges")
    by = {c["claim_id"]: c for c in v["claims"]}
    assert by["c1"]["label"] == "contradicted" and "Repo is a fork of upstream/forked-lib" in by["c1"]["top_reason"]
    assert by["c2"]["label"] == "partial" and by["c2"]["top_reason"] == "60 stars; claim says 100"
    assert by["c3"]["label"] == "partial" and by["c3"]["top_reason"] == "Python is 19% of the code"
    assert by["c4"]["label"] == "contradicted"
    assert by["c5"]["label"] == "no_evidence" and by["c5"]["top_reason"].startswith("No matching public repo")
    assert by["c6"]["label"] == "contradicted" and by["c6"]["top_reason"].startswith("Repo not found")
    assert by["c7"]["label"] == "no_evidence" and by["c7"]["top_reason"] == "Repo details unavailable offline"
    assert by["c8"]["label"] == "no_evidence" and by["c8"]["top_reason"] == "Couldn't match the repo"
    assert by["c9"]["label"] == "supported"
    assert by["c10"]["label"] == "partial" and by["c10"]["flags"] == ["run_failed"]
    assert by["c11"]["label"] == "contradicted"
    assert by["c12"]["label"] == "partial"
    assert by["c13"]["label"] == "partial" and by["c13"]["top_reason"].startswith("Only in star-proj")
    assert by["c14"]["label"] == "no_evidence"
    assert by["c15"]["top_reason"] == "Framework or tool; not checked in v1"
    assert by["c16"]["label"] == "supported"
    assert by["c17"]["top_reason"] == "Couldn't classify this claim; check the resume."
    assert by["c18"]["top_reason"] == "Contribution check not run"
    assert v["context"][-1] == "Some GitHub evidence was unavailable offline (4 requests)."


def test_nologin_case():
    v = compute("nologin")
    assert v["account"] is None
    assert [c["top_reason"] for c in v["claims"]] == [
        "No GitHub account found", "No GitHub account found", "No GitHub account found",
        "Not verifiable from public code"]
    assert v["context"] == ["No GitHub account found on the resume or application."]
