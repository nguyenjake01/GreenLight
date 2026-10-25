"""J2 matching tests: match_repo, match_commits, commit_timeline, URL builders (all pure, offline)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from verifier.contracts.snapshot import Snapshot, cache_key
from verifier.core import match

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
SAMPLE = FIXTURES / "snapshots" / "sample"


class FakeSnap:
    """Same `get` and `evidence` as a Snapshot, built from {url: (status, body)}."""

    def __init__(self, login: str | None, table: dict[str, tuple[int, Any]] | None = None,
                 decisions: list[dict] | None = None):
        self.evidence = {"github_login": login, "decisions": decisions or []}
        self.table = {cache_key("GET", u): v for u, v in (table or {}).items()}

    def get(self, method: str, url: str):
        return self.table.get(cache_key(method, url))


def repo_list(*names: str) -> list[dict]:
    return [{"name": n, "fork": False, "description": None, "language": None} for n in names]


def project(claim_id="c1", repo_name=None, repo_url=None) -> dict:
    return {"claim_id": claim_id, "type": "project", "section": "Projects", "source_text": "x",
            "needs_review": False,
            "assertions": {"repo_url": repo_url, "repo_name": repo_name, "role": "creator",
                           "language": None, "min_stars": None, "start_year": None, "scale": None,
                           "public_signal": "none", "behavior": None}}


def snap_with_repos(*names: str, login="x", decisions=None) -> FakeSnap:
    return FakeSnap(login, {match.repos_list_url(login): (200, repo_list(*names))}, decisions)


# ── URL builders ──────────────────────────────────────────────────────────────

def test_urls_lowercase_logins_and_repos():
    assert match.user_url("HenryChen4736") == "https://api.github.com/users/henrychen4736"
    assert match.repo_detail_url("Org/Big-Repo") == "https://api.github.com/repos/org/big-repo"
    assert match.languages_url("A/B") == "https://api.github.com/repos/a/b/languages"
    assert match.repos_list_url("X") == \
        "https://api.github.com/users/x/repos?per_page=100&sort=pushed&type=owner"


def test_url_keys_are_equivalent_across_case_and_param_order():
    a = cache_key("GET", match.contributors_url("Foo/Bar"))
    b = cache_key("get", "HTTPS://API.GITHUB.COM/repos/foo/bar/contributors?per_page=100&anon=1")
    assert a == b == "GET https://api.github.com/repos/foo/bar/contributors?anon=1&per_page=100"


def test_search_urls():
    merged = match.merged_prs_url("Org/Repo", "Me")
    assert merged == "https://api.github.com/search/issues?q=type:pr+is:merged+author:me+repo:org/repo"
    assert match.any_prs_url("o/r", "me").endswith("q=type:pr+author:me+repo:o/r")
    assert match.reviewed_prs_url("o/r", "me").endswith("q=type:pr+reviewed-by:me+repo:o/r")
    assert match.author_commits_url("O/R", "Me").endswith("/repos/o/r/commits?author=me&per_page=100")
    # `+` and `%20` give the same key
    assert cache_key("GET", merged) == cache_key(
        "GET", "https://api.github.com/search/issues?q=type:pr%20is:merged%20author:me%20repo:org/repo")


@pytest.mark.parametrize("ref,expected", [
    ("https://github.com/Org/Repo", "Org/Repo"),
    ("github.com/org/repo.git", "org/repo"),
    ("https://www.github.com/org/repo/tree/main/src", "org/repo"),
    ("org/repo", "org/repo"),
    ("https://gitlab.com/org/repo", None),
    ("https://github.com/org", None),
    ("", None), (None, None),
])
def test_parse_repo_ref(ref, expected):
    assert match.parse_repo_ref(ref) == expected


# ── match_repo ────────────────────────────────────────────────────────────────

def test_url_match_any_owner_and_404_means_not_found():
    snap = snap_with_repos("mine")
    got = match.match_repo(project(repo_url="https://github.com/SomeOrg/Thing"), snap)
    assert got["repo"] == "SomeOrg/Thing" and got["method"] == "url"

    snap.table[cache_key("GET", match.repo_detail_url("someorg/thing"))] = (404, {"message": "Not Found"})
    gone = match.match_repo(project(repo_url="https://github.com/SomeOrg/Thing"), snap)
    assert gone["repo"] is None and gone["method"] is None and "404" in gone["searched"]


def test_url_match_wins_over_name():
    snap = snap_with_repos("paykit")
    got = match.match_repo(project(repo_name="paykit", repo_url="github.com/other/paykit"), snap)
    assert (got["repo"], got["method"]) == ("other/paykit", "url")


def test_non_github_url_is_not_found():
    got = match.match_repo(project(repo_url="https://gitlab.com/a/b"), snap_with_repos("b"))
    assert got["repo"] is None and "not a GitHub" in got["searched"]


@pytest.mark.parametrize("claimed", ["CubeSolver", "cubesolver", "CUBESOLVER", " cubesolver ", "x/cubesolver"])
def test_exact_name_ignores_case(claimed):
    claimed = claimed.replace("x/", "X/")
    snap = snap_with_repos("cubeSolver", "other")
    got = match.match_repo(project(repo_name=claimed), snap)
    assert (got["repo"], got["method"]) == ("x/cubeSolver", "name")


def test_exact_name_counts_forks():
    snap = FakeSnap("x", {match.repos_list_url("x"): (200, [{"name": "forked", "fork": True}])})
    assert match.match_repo(project(repo_name="Forked"), snap)["repo"] == "x/forked"


def test_not_found_says_what_was_searched():
    snap = snap_with_repos("a", "b", "c", "d", "e", login="x")
    got = match.match_repo(project(repo_name="payments-lib"), snap)
    assert got == {"repo": None, "method": None,
                   "searched": "No repo named payments-lib among @x's 5 public repos"}


def test_recorded_decision_matches_with_llm_method():
    decision = {"kind": "repo_match", "claim_id": "c3", "model": "m", "prompt_version": "match-v1",
                "inputs": {"claimed": "payments library", "candidates": ["paykit", "dotfiles"]},
                "output": "PayKit"}
    snap = snap_with_repos("paykit", "dotfiles", decisions=[decision])
    got = match.match_repo(project("c3", repo_name="payments library"), snap)
    assert (got["repo"], got["method"]) == ("x/paykit", "llm")
    # a decision for another claim does not apply
    assert match.match_repo(project("c9", repo_name="payments library"), snap)["repo"] is None


def test_recorded_none_decision_is_not_found():
    decision = {"kind": "repo_match", "claim_id": "c3", "model": "m", "prompt_version": "match-v1",
                "inputs": {}, "output": "none"}
    got = match.match_repo(project("c3", repo_name="mystery"), snap_with_repos("a", decisions=[decision]))
    assert got["repo"] is None and "chose none" in got["searched"]


def test_exact_name_beats_recorded_decision():
    decision = {"kind": "repo_match", "claim_id": "c1", "model": "m", "prompt_version": "match-v1",
                "inputs": {}, "output": "b"}
    got = match.match_repo(project(repo_name="a"), snap_with_repos("a", "b", decisions=[decision]))
    assert (got["repo"], got["method"]) == ("x/a", "name")


def test_no_login_or_no_repo_list_or_no_name():
    assert match.match_repo(project(repo_name="a"), FakeSnap(None))["repo"] is None
    unavailable = match.match_repo(project(repo_name="a"), FakeSnap("x"))
    assert unavailable["repo"] is None and "not available" in unavailable["searched"]
    nameless = match.match_repo(project(), snap_with_repos("a"))
    assert nameless["repo"] is None and "names no repo" in nameless["searched"]


def test_non_project_claim():
    claim = project()
    claim["type"] = "skill"
    assert match.match_repo(claim, snap_with_repos("a"))["repo"] is None


def test_match_repo_on_the_sample_snapshot():
    snap = Snapshot.load(SAMPLE)
    got = match.match_repo(project(repo_name="polytrack-ml"), snap)
    assert (got["repo"], got["method"]) == ("henrychen4736/Polytrack-ML", "name")
    assert match.match_repo(project(repo_name="nope"), snap)["repo"] is None


# ── match_commits ─────────────────────────────────────────────────────────────

CAND = {"name": "José García", "emails": ["jose@example.com"], "github_login": "JoseG"}


def anon(name, email, n):
    return {"type": "Anonymous", "name": name, "email": email, "contributions": n}


def test_linked_entry_by_login_any_case():
    body = [{"login": "joseg", "contributions": 7}, {"login": "other", "contributions": 3}]
    assert match.match_commits(body, CAND) == {
        "total": 10, "matched": 7, "by_method": {"login": 7, "email": 0, "name": 0}}


@pytest.mark.parametrize("email", ["joseg@users.noreply.github.com",
                                   "12345+JoseG@users.noreply.github.com"])
def test_noreply_email_counts_as_login(email):
    got = match.match_commits([anon("Someone Else", email, 4)], CAND)
    assert got["by_method"] == {"login": 4, "email": 0, "name": 0} and got["matched"] == 4


def test_noreply_for_another_login_does_not_match():
    got = match.match_commits([anon("X", "99+someoneelse@users.noreply.github.com", 4)], CAND)
    assert got["matched"] == 0 and got["total"] == 4


def test_candidate_email_counts_as_email():
    got = match.match_commits([anon("Different Name", "Jose@Example.com", 2)], CAND)
    assert got["by_method"] == {"login": 0, "email": 2, "name": 0}


def test_accented_and_punctuated_name_counts_as_name():
    body = [anon("jose garcia", "x@laptop.local", 5), anon("JOSÉ  GARCÍA.", "y@laptop.local", 1)]
    got = match.match_commits(body, CAND)
    assert got["by_method"] == {"login": 0, "email": 0, "name": 6} and got["total"] == 6


def test_mixed_entries_and_counts_are_commits():
    body = [{"login": "joseg", "contributions": 2}, anon("Jose Garcia", "a@b.c", 3),
            anon("Jose X", "jose@example.com", 4), anon("Nobody", "n@x.y", 10)]
    got = match.match_commits(body, CAND)
    assert got == {"total": 19, "matched": 9, "by_method": {"login": 2, "email": 4, "name": 3}}


def test_empty_and_odd_bodies():
    zero = {"total": 0, "matched": 0, "by_method": {"login": 0, "email": 0, "name": 0}}
    assert match.match_commits([], CAND) == zero
    assert match.match_commits({"message": "empty"}, CAND) == zero
    assert match.match_commits(None, CAND) == zero
    assert match.match_commits("not json", CAND) == zero
    assert match.match_commits(json.dumps([{"login": "joseg", "contributions": 1}]), CAND)["matched"] == 1


def test_candidate_without_login_still_matches_by_email_and_name():
    cand = {"name": "A B", "emails": ["a@b.c"], "github_login": None}
    got = match.match_commits([anon("A B", "q@q.q", 1), anon("Z", "a@b.c", 2),
                               {"login": "someone", "contributions": 9}], cand)
    assert got["by_method"] == {"login": 0, "email": 2, "name": 1}


def test_normalize_name():
    assert match.normalize_name("  Zoë   O'Brien-Smith ") == "zoe obriensmith"
    assert match.normalize_name(None) == ""


def test_match_commits_on_sample_contributors():
    snap = Snapshot.load(SAMPLE)
    _, body = snap.get("GET", match.contributors_url("henrychen4736/polytrack-ml"))
    cand = {"name": "Henry Chen", "emails": [], "github_login": "henrychen4736"}
    # Polytrack-ML: all 35 commits are anonymous (unlinked laptop email); the name matches.
    assert match.match_commits(body, cand) == {
        "total": 35, "matched": 35, "by_method": {"login": 0, "email": 0, "name": 35}}
    _, linked = snap.get("GET", match.contributors_url("henrychen4736/cac24"))
    assert match.match_commits(linked, cand) == {
        "total": 5, "matched": 5, "by_method": {"login": 1, "email": 0, "name": 4}}


# ── commit_timeline ───────────────────────────────────────────────────────────

def commit(date: str) -> dict:
    return {"sha": "s", "commit": {"author": {"name": "n", "date": date}, "committer": {"date": date}}}


def test_timeline_basic():
    body = [commit("2026-10-19T21:00:00Z"), commit("2026-10-18T09:00:00Z"),
            commit("2026-10-19T08:00:00Z")]
    assert match.commit_timeline(body) == {
        "count": 3, "first": "2026-10-18T09:00:00Z", "last": "2026-10-19T21:00:00Z", "distinct_days": 2}


def test_timeline_empty_repo_shapes():
    empty = {"count": 0, "first": None, "last": None, "distinct_days": 0}
    assert match.commit_timeline([]) == empty
    assert match.commit_timeline({"message": "Git Repository is empty."}) == empty
    assert match.commit_timeline(None) == empty


def test_timeline_reads_only_first_100():
    body = [commit(f"2026-01-{(i % 28) + 1:02d}T00:00:00Z") for i in range(150)]
    got = match.commit_timeline(body)
    assert got["count"] == 100 and got["distinct_days"] == 28


def test_timeline_falls_back_to_committer_date():
    body = [{"commit": {"author": {}, "committer": {"date": "2026-02-02T02:00:00Z"}}}]
    assert match.commit_timeline(body)["first"] == "2026-02-02T02:00:00Z"


def test_timeline_on_sample_commits():
    snap = Snapshot.load(SAMPLE)
    _, body = snap.get("GET", match.commits_url("henrychen4736/polytrack-ml"))
    got = match.commit_timeline(body)
    assert got["count"] == len(body) and got["distinct_days"] >= 1 and got["first"] <= got["last"]
