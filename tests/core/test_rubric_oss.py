"""Unit tests for the oss_contribution checks (design doc J13): every row and boundary.

Snapshots come from `World` in test_rubric.py; the contribution requests are recorded under the
same URLs the J2 fetcher uses (``verifier.core.match``'s builders).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.core.test_rubric import World, check, doc, names, only, verdict
from verifier.contracts.snapshot import cache_key
from verifier.core import match, rubric, store

REPO = "acme/lib"
LOGIN = "alice"


def pr(n: int, merged_at: str | None = None, *, created: str = "2026-01-01T00:00:00Z",
       closed: str | None = None, title: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {
        "html_url": f"https://github.com/{REPO}/pull/{n}", "number": n,
        "title": title or f"PR {n}", "state": "closed" if (merged_at or closed) else "open",
        "created_at": created, "closed_at": merged_at or closed,
        "pull_request": {"merged_at": merged_at},
    }
    return item


def search(total: int | None = None, items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    items = items or []
    return {"total_count": len(items) if total is None else total, "incomplete_results": False,
            "items": items}


def oss_claim(*, role: str = "contributor", min_prs: int | None = 1, repo: str = REPO,
              cid: str = "c1") -> dict[str, Any]:
    return {"claim_id": cid, "type": "oss_contribution", "section": "Open Source",
            "needs_review": False, "source_text": f"Contributed to {repo}",
            "assertions": {"repo": repo, "role": role, "min_prs": min_prs, "description": None}}


def key_url(url: str) -> str:
    """The URL in the canonical form the fetcher records it under (`cache_key`), so the
    snapshot's keys match what `Snapshot.get` looks up."""
    return cache_key("GET", url)[len("GET "):]


def record(w: World, *, merged: dict[str, Any] | None = None, opened: dict[str, Any] | None = None,
           reviews: dict[str, Any] | None = None, commits: Any = None, repo: str = REPO) -> None:
    """Record the contribution requests; `reviews=None` leaves the reviewed-by search out."""
    w.put(key_url(match.merged_prs_url(repo, LOGIN)), 200, merged if merged is not None else search())
    items = (merged or {}).get("items") or []
    w.put(key_url(match.any_prs_url(repo, LOGIN)), 200, opened if opened is not None else (
        search(merged["total_count"], items) if merged is not None else search()))
    if reviews is not None:
        w.put(key_url(match.reviewed_prs_url(repo, LOGIN)), 200, reviews)
    w.put(key_url(match.author_commits_url(repo, LOGIN)), 200, commits if commits is not None else [])


def run(tmp_path: Path, claim: dict[str, Any], w: World) -> dict[str, Any]:
    return only(verdict(tmp_path, [claim], w))


def commit_list(n: int) -> list[dict[str, Any]]:
    return [{"sha": f"{i:040x}", "commit": {"message": f"c{i}"}} for i in range(n)]


# ── Which case applies ────────────────────────────────────────────────────────

def test_no_requests_at_all_is_not_run(tmp_path):
    vc = run(tmp_path, oss_claim(), World(tmp_path, LOGIN))
    assert vc["label"] == "no_evidence" and vc["top_reason"] == "Contribution check not run"
    assert [(c["check"], c["result"]) for c in vc["checks"]] == [("contribution", "skip")]
    assert vc["evidence_links"] == []


def test_unparseable_repo_is_not_run(tmp_path):
    vc = run(tmp_path, oss_claim(repo="not a repo"), World(tmp_path, LOGIN))
    assert vc["top_reason"] == "Contribution check not run"


@pytest.mark.parametrize("which", ["merged", "any", "commits", "reviews"])
def test_any_key_in_misses_is_unavailable_offline(tmp_path, which):
    w = World(tmp_path, LOGIN)
    record(w, merged=search(3, [pr(1, "2026-02-01T00:00:00Z")]), reviews=search(9),
           commits=commit_list(2))
    url = {"merged": match.merged_prs_url, "any": match.any_prs_url,
           "commits": match.author_commits_url, "reviews": match.reviewed_prs_url}[which](REPO, LOGIN)
    url = key_url(url)
    del w.responses[url]
    w.miss(url)
    vc = run(tmp_path, oss_claim(role="maintainer", min_prs=3), w)
    assert vc["label"] == "no_evidence"
    assert vc["top_reason"] == "Contribution evidence unavailable offline"
    assert [c["check"] for c in vc["checks"]] == ["contribution"]
    assert vc["evidence_links"] == []


def test_all_keys_in_misses_is_unavailable_not_not_run(tmp_path):
    w = World(tmp_path, LOGIN)
    for u in (key_url(match.merged_prs_url(REPO, LOGIN)), key_url(match.any_prs_url(REPO, LOGIN)),
              key_url(match.author_commits_url(REPO, LOGIN))):
        w.miss(u)
    vc = run(tmp_path, oss_claim(), w)
    assert vc["top_reason"] == "Contribution evidence unavailable offline"


def test_review_miss_ignored_for_a_contributor(tmp_path):
    """The reviewed-by request is only made for maintainers, so its miss does not matter."""
    w = World(tmp_path, LOGIN)
    record(w, merged=search(1, [pr(1, "2026-02-01T00:00:00Z")]))
    w.miss(key_url(match.reviewed_prs_url(REPO, LOGIN)))
    vc = run(tmp_path, oss_claim(), w)
    assert vc["label"] == "supported"


def test_partly_recorded_evidence_is_unavailable(tmp_path):
    w = World(tmp_path, LOGIN)
    w.put(key_url(match.merged_prs_url(REPO, LOGIN)), 200, search(1, [pr(1, "2026-02-01T00:00:00Z")]))
    vc = run(tmp_path, oss_claim(), w)
    assert vc["top_reason"] == "Contribution evidence unavailable offline"


def test_malformed_search_body_is_unavailable(tmp_path):
    w = World(tmp_path, LOGIN)
    record(w)
    w.put(key_url(match.merged_prs_url(REPO, LOGIN)), 200, {"message": "boom"})
    assert run(tmp_path, oss_claim(), w)["top_reason"] == "Contribution evidence unavailable offline"


def test_no_login_still_short_circuits(tmp_path):
    vc = only(verdict(tmp_path, [oss_claim()], World(tmp_path, None)))
    assert vc["top_reason"] == "No GitHub account found"


# ── Nothing found ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("claim", [
    oss_claim(min_prs=None), oss_claim(min_prs=5), oss_claim(role="maintainer", min_prs=10)])
def test_nothing_found_is_no_evidence_never_red(tmp_path, claim):
    w = World(tmp_path, LOGIN)
    record(w, reviews=search(0))
    vc = run(tmp_path, claim, w)
    assert vc["label"] == "no_evidence"
    assert vc["top_reason"] == f"Searched {REPO} for PRs and commits by @{LOGIN}; none found."
    assert [(c["check"], c["result"]) for c in vc["checks"]] == [("contribution", "skip")]
    assert vc["evidence_links"] == []


def test_nothing_found_with_commits_404_or_empty_repo(tmp_path):
    for status in (404, 409):
        w = World(tmp_path, LOGIN)
        record(w)
        w.put(key_url(match.author_commits_url(REPO, LOGIN)), status, {"message": "x"})
        vc = run(tmp_path, oss_claim(), w)
        assert vc["top_reason"].startswith("Searched ")


# ── contribution row ──────────────────────────────────────────────────────────

def test_one_merged_pr_passes_with_singular_wording(tmp_path):
    w = World(tmp_path, LOGIN)
    record(w, merged=search(1, [pr(7, "2026-02-01T00:00:00Z", title="Fix it")]))
    vc = run(tmp_path, oss_claim(min_prs=None), w)
    assert vc["label"] == "supported"
    assert vc["top_reason"] == f"1 merged PR and 0 commits by @{LOGIN} in {REPO}"
    assert names(vc) == ["contribution"]
    assert check(vc, "contribution")["facts"] == {
        "merged_total": 1, "opened_total": 1, "reviews_total": 0, "landed_commits": 0,
        "pr_titles": ["Fix it"]}
    assert vc["evidence_links"] == [f"https://github.com/{REPO}/pull/7"]


def test_landed_commits_count_as_contribution(tmp_path):
    w = World(tmp_path, LOGIN)
    record(w, commits=commit_list(3))
    vc = run(tmp_path, oss_claim(min_prs=None), w)
    assert vc["label"] == "supported"
    assert vc["top_reason"] == f"0 merged PRs and 3 commits by @{LOGIN} in {REPO}"
    assert check(vc, "contribution")["facts"]["landed_commits"] == 3
    assert vc["evidence_links"] == [f"https://github.com/{REPO}/commits?author={LOGIN}"]


def test_one_commit_wording_and_100_plus(tmp_path):
    w = World(tmp_path, LOGIN)
    record(w, commits=commit_list(1))
    assert run(tmp_path, oss_claim(min_prs=None), w)["top_reason"] == \
        f"0 merged PRs and 1 commit by @{LOGIN} in {REPO}"
    w = World(tmp_path, LOGIN)
    record(w, commits=commit_list(100))
    vc = run(tmp_path, oss_claim(min_prs=None), w)
    assert vc["top_reason"] == f"0 merged PRs and 100+ commits by @{LOGIN} in {REPO}"
    assert check(vc, "contribution")["facts"]["landed_commits"] == 100


def test_prs_opened_none_merged_is_soft_partial(tmp_path):
    w = World(tmp_path, LOGIN)
    record(w, merged=search(0), opened=search(2, [pr(1), pr(2)]))
    vc = run(tmp_path, oss_claim(min_prs=None), w)
    assert vc["label"] == "partial"
    assert vc["top_reason"] == f"2 PRs opened in {REPO}, none merged"
    assert check(vc, "contribution")["result"] == "soft"
    assert vc["evidence_links"] == []
    assert check(vc, "contribution")["facts"]["opened_total"] == 2


def test_one_opened_pr_wording(tmp_path):
    w = World(tmp_path, LOGIN)
    record(w, merged=search(0), opened=search(1, [pr(1)]))
    assert run(tmp_path, oss_claim(min_prs=None), w)["top_reason"] == \
        f"1 PR opened in {REPO}, none merged"


def test_opened_prs_do_not_make_it_soft_when_commits_landed(tmp_path):
    w = World(tmp_path, LOGIN)
    record(w, merged=search(0), opened=search(2, [pr(1), pr(2)]), commits=commit_list(4))
    vc = run(tmp_path, oss_claim(min_prs=None), w)
    assert vc["label"] == "supported" and check(vc, "contribution")["result"] == "pass"


# ── pr_count row and its boundaries ───────────────────────────────────────────

def pr_count_label(tmp_path, merged: int, claim: int) -> tuple[str, dict[str, Any]]:
    w = World(tmp_path, LOGIN)
    items = [pr(i + 1, f"2026-02-{i % 27 + 1:02d}T00:00:00Z") for i in range(merged)]
    record(w, merged=search(merged, items))
    vc = run(tmp_path, oss_claim(min_prs=claim), w)
    return vc["label"], check(vc, "pr_count")


@pytest.mark.parametrize("merged, claim, result, label", [
    (10, 10, "pass", "supported"),
    (8, 10, "pass", "supported"),       # exactly 80%
    (7, 10, "soft", "partial"),         # 70%
    (5, 10, "soft", "partial"),         # exactly 50%
    (4, 10, "hard", "contradicted"),    # 40%
    (1, 3, "hard", "contradicted"),     # 33%
    (2, 3, "soft", "partial"),          # 66%
    (4, 5, "pass", "supported"),        # 80% of 5
    (3, 5, "soft", "partial"),          # 60%
    (1, 2, "soft", "partial"),          # 50% of 2
    (1, 1, "pass", "supported"),
    (12, 5, "pass", "supported"),       # more than claimed
])
def test_pr_count_boundaries(tmp_path, merged, claim, result, label):
    got_label, chk = pr_count_label(tmp_path, merged, claim)
    assert (chk["result"], got_label) == (result, label)
    assert chk["reason"] == f"{rubric._plural(merged, 'merged PR')}; claim says {claim}"


def test_pr_count_only_with_min_prs(tmp_path):
    w = World(tmp_path, LOGIN)
    record(w, merged=search(1, [pr(1, "2026-02-01T00:00:00Z")]))
    assert names(run(tmp_path, oss_claim(min_prs=None), w)) == ["contribution"]
    w = World(tmp_path, LOGIN)
    record(w, merged=search(1, [pr(1, "2026-02-01T00:00:00Z")]))
    assert names(run(tmp_path, oss_claim(min_prs=1), w)) == ["contribution", "pr_count"]


def test_pr_count_hard_beats_contribution_pass_in_top_reason(tmp_path):
    w = World(tmp_path, LOGIN)
    record(w, merged=search(1, [pr(1, "2026-02-01T00:00:00Z")]))
    vc = run(tmp_path, oss_claim(min_prs=10), w)
    assert vc["label"] == "contradicted"
    assert vc["top_reason"] == "1 merged PR; claim says 10"


def test_pr_count_soft_with_pass_contribution_top_reason_is_the_soft_one(tmp_path):
    label, chk = pr_count_label(tmp_path, 6, 10)
    assert label == "partial"


# ── maintainer row ────────────────────────────────────────────────────────────

def maintainer(tmp_path, reviews: int, *, min_prs: int | None = None) -> dict[str, Any]:
    w = World(tmp_path, LOGIN)
    record(w, merged=search(2, [pr(1, "2026-02-01T00:00:00Z"), pr(2, "2026-03-01T00:00:00Z")]),
           reviews=search(reviews))
    return run(tmp_path, oss_claim(role="maintainer", min_prs=min_prs), w)


@pytest.mark.parametrize("reviews, result", [(5, "pass"), (6, "pass"), (100, "pass"),
                                            (4, "soft"), (1, "soft"), (0, "soft")])
def test_maintainer_review_threshold_is_five(tmp_path, reviews, result):
    vc = maintainer(tmp_path, reviews)
    chk = check(vc, "maintainer")
    assert chk["result"] == result
    assert vc["label"] == ("supported" if result == "pass" else "partial")
    if result == "pass":
        assert chk["reason"] == f"{reviews} PRs reviewed in {REPO}"
    else:
        assert chk["reason"] == ("Contributions found, but only 1 PR review" if reviews == 1
                                 else f"Contributions found, but only {reviews} PR reviews")
        assert vc["top_reason"] == chk["reason"]
    assert check(vc, "contribution")["facts"]["reviews_total"] == reviews


def test_maintainer_check_skipped_for_other_roles(tmp_path):
    w = World(tmp_path, LOGIN)
    record(w, merged=search(1, [pr(1, "2026-02-01T00:00:00Z")]), reviews=search(0))
    assert "maintainer" not in names(run(tmp_path, oss_claim(role="contributor"), w))


def test_maintainer_with_missing_review_search_is_unavailable(tmp_path):
    w = World(tmp_path, LOGIN)
    record(w, merged=search(1, [pr(1, "2026-02-01T00:00:00Z")]))   # reviews not recorded
    vc = run(tmp_path, oss_claim(role="maintainer"), w)
    assert vc["top_reason"] == "Contribution evidence unavailable offline"


def test_maintainer_with_commits_only_and_few_reviews_is_partial(tmp_path):
    w = World(tmp_path, LOGIN)
    record(w, reviews=search(2), commits=commit_list(3))
    vc = run(tmp_path, oss_claim(role="maintainer", min_prs=None), w)
    assert vc["label"] == "partial"
    assert [(c["check"], c["result"]) for c in vc["checks"]] == [
        ("contribution", "pass"), ("maintainer", "soft")]


def test_all_three_checks_in_order(tmp_path):
    vc = maintainer(tmp_path, 7, min_prs=2)
    assert names(vc) == ["contribution", "pr_count", "maintainer"]
    assert vc["label"] == "supported"
    assert vc["top_reason"] == f"2 merged PRs and 0 commits by @{LOGIN} in {REPO}"


def test_hard_pr_count_and_soft_maintainer_hard_wins(tmp_path):
    vc = maintainer(tmp_path, 1, min_prs=10)
    assert vc["label"] == "contradicted"
    assert vc["top_reason"] == "2 merged PRs; claim says 10"


# ── Evidence links ────────────────────────────────────────────────────────────

def test_links_are_at_most_five_merged_prs_newest_first_plus_commits(tmp_path):
    w = World(tmp_path, LOGIN)
    items = [pr(n, f"2026-0{n}-15T00:00:00Z") for n in (3, 1, 6, 2, 5, 4, 7)]   # arbitrary order
    record(w, merged=search(7, items), commits=commit_list(2))
    vc = run(tmp_path, oss_claim(min_prs=None), w)
    assert vc["evidence_links"] == (
        [f"https://github.com/{REPO}/pull/{n}" for n in (7, 6, 5, 4, 3)]
        + [f"https://github.com/{REPO}/commits?author={LOGIN}"])
    facts = check(vc, "contribution")["facts"]
    assert facts["merged_total"] == 7 and facts["pr_titles"] == [f"PR {n}" for n in (7, 6, 5, 4, 3)]


def test_no_commits_link_without_landed_commits(tmp_path):
    w = World(tmp_path, LOGIN)
    record(w, merged=search(1, [pr(1, "2026-02-01T00:00:00Z")]))
    assert run(tmp_path, oss_claim(), w)["evidence_links"] == [f"https://github.com/{REPO}/pull/1"]


def test_link_order_breaks_ties_by_pr_number(tmp_path):
    w = World(tmp_path, LOGIN)
    same = "2026-02-01T00:00:00Z"
    record(w, merged=search(3, [pr(2, same), pr(9, same), pr(5, same)]))
    links = run(tmp_path, oss_claim(), w)["evidence_links"]
    assert links == [f"https://github.com/{REPO}/pull/{n}" for n in (9, 5, 2)]


def test_pr_titles_are_never_compared_with_the_description(tmp_path):
    w = World(tmp_path, LOGIN)
    record(w, merged=search(1, [pr(1, "2026-02-01T00:00:00Z", title="Unrelated typo fix")]))
    claim = oss_claim(min_prs=1)
    claim["assertions"]["description"] = "Rewrote the GPU scheduler"
    vc = run(tmp_path, claim, w)
    assert vc["label"] == "supported"


# ── Plumbing ──────────────────────────────────────────────────────────────────

def test_repo_given_as_url_uses_the_same_requests(tmp_path):
    w = World(tmp_path, LOGIN)
    record(w, merged=search(1, [pr(1, "2026-02-01T00:00:00Z")]))
    vc = run(tmp_path, oss_claim(repo=f"https://github.com/{REPO}"), w)
    assert vc["label"] == "supported"


def test_tally_counts_oss_labels(tmp_path):
    w = World(tmp_path, LOGIN)
    record(w, merged=search(1, [pr(1, "2026-02-01T00:00:00Z")]))
    v = verdict(tmp_path, [oss_claim(cid="c1"), oss_claim(cid="c2", repo="x/y")], w)
    assert v["tally"] == {"supported": 1, "partial": 0, "contradicted": 0, "no_evidence": 1}
    assert [c["top_reason"] for c in v["claims"]][1] == "Contribution check not run"


def test_verdict_is_deterministic(tmp_path):
    w = World(tmp_path, LOGIN)
    record(w, merged=search(2, [pr(1, "2026-02-01T00:00:00Z"), pr(2, "2026-02-02T00:00:00Z")]),
           reviews=search(5), commits=commit_list(2))
    snap = w.build()
    d = doc([oss_claim(role="maintainer", min_prs=2)], LOGIN)
    a = rubric.compute_verdict(d, snap, [], verdict_n=1, computed_at="2026-10-25T14:06:40Z")
    b = rubric.compute_verdict(d, snap, [], verdict_n=1, computed_at="2026-10-25T14:06:40Z")
    assert store.dump_json(a) == store.dump_json(b)
