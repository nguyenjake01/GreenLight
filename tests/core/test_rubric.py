"""Unit tests for the rubric (design doc J5): thresholds, aliases, combining, context lines.

Snapshots are built in a temp folder by `World`, which writes the same request URLs the J2
fetcher records (via ``verifier.core.match``'s URL builders).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from verifier.contracts.snapshot import Snapshot
from verifier.core import match, rubric, store

NOW = "2026-10-25T14:00:00Z"
COMPUTED = "2026-10-25T14:06:40Z"


# ── Builders ──────────────────────────────────────────────────────────────────

class World:
    """A fake GitHub account recorded as a snapshot."""

    def __init__(self, root: Path, login: str | None = "alice", *, now: str = NOW,
                 created: str | None = "2020-01-15T00:00:00Z", user_status: int = 200,
                 public_repos: int | None = None, followers: int = 3):
        self.root, self.login, self.now = root, login, now
        self.responses: dict[str, tuple[int, Any]] = {}
        self.misses: list[str] = []
        self.decisions: list[dict[str, Any]] = []
        self.repos: list[dict[str, Any]] = []
        self.repo_list_status = 200
        self.repo_list_miss = False
        self.user = {"login": login, "created_at": created, "public_repos": public_repos,
                     "followers": followers}
        if login:
            if user_status == 200:
                self.put(match.user_url(login), 200, self.user)
            elif user_status == 0:
                self.miss(match.user_url(login))
            else:
                self.put(match.user_url(login), user_status, {"message": "Not Found"})

    def decide(self, claim_id: str, output: str = "none") -> None:
        """Record a repo-match model decision (fetch records one for every unmatched name)."""
        self.decisions.append({"kind": "repo_match", "claim_id": claim_id, "model": "m",
                               "prompt_version": "match-v1", "inputs": {}, "output": output})

    def put(self, url: str, status: int, body: Any) -> None:
        self.responses[url] = (status, body)

    def miss(self, url: str) -> None:
        self.misses.append(url)

    def repo(self, name: str, *, owner: str | None = None, created: str = "2024-03-01T00:00:00Z",
             pushed: str | None = None, stars: int = 0, size: int = 500, fork: bool = False,
             parent: str | None = None, languages: dict[str, int] | None | str = "default",
             contributors: list[dict[str, Any]] | str | None = "default",
             commits: int | list[dict[str, Any]] | str = 10, detail: str = "ok",
             commit_days: int | None = None, listed: bool = True) -> str:
        """Register a repo. `languages`/`contributors`/`commits`/`detail` may be "miss"."""
        owner = owner or self.login or "alice"
        full = f"{owner}/{name}"
        body: dict[str, Any] = {
            "name": name, "full_name": full, "html_url": f"https://github.com/{full}",
            "fork": fork, "created_at": created, "pushed_at": pushed or created, "size": size,
            "stargazers_count": stars, "language": None,
        }
        if parent:
            body["parent"] = {"full_name": parent}
        if listed:
            self.repos.append(body)
        if detail == "miss":
            self.miss(match.repo_detail_url(full))
        elif detail == "ok":
            self.put(match.repo_detail_url(full), 200, body)
        else:
            self.put(match.repo_detail_url(full), int(detail), {"message": "Not Found"})
        if languages == "default":
            languages = {"Python": 1000}
        if languages == "miss":
            self.miss(match.languages_url(full))
        elif languages is not None:
            self.put(match.languages_url(full), 200, languages)
        if isinstance(commits, int):
            n = commits
            days = commit_days or max(1, min(n, 5))
            commit_list = [{"sha": f"{i:040x}", "commit": {"author": {
                "name": "A", "email": "a@x", "date": f"2026-0{1 + (i % days) % 9}-10T10:00:00Z"}}}
                for i in range(n)]
        else:
            commit_list = commits
        if contributors == "default":
            n = len(commit_list) if isinstance(commit_list, list) else 0
            contributors = [{"login": self.login, "contributions": n}]
        if contributors == "miss":
            self.miss(match.contributors_url(full))
        elif contributors is not None:
            self.put(match.contributors_url(full), 200, contributors)
        if commit_list == "miss":
            self.miss(match.commits_url(full))
        elif commit_list == "empty":
            self.put(match.commits_url(full), 409, {"message": "Git Repository is empty."})
        else:
            self.put(match.commits_url(full), 200, commit_list)
        return full

    def build(self) -> Snapshot:
        root = self.root / f"snap_{len(list(self.root.glob('snap_*')))}"
        (root / "raw").mkdir(parents=True)
        if self.login:
            if self.repo_list_miss:
                self.miss(match.repos_list_url(self.login))
            else:
                self.put(match.repos_list_url(self.login), self.repo_list_status, self.repos)
        responses = []
        for url in sorted(self.responses):
            status, body = self.responses[url]
            data = store.dump_json(body)
            sha = store.sha256_bytes(data)
            (root / "raw" / f"{sha}.json").write_bytes(data)
            responses.append({"key": f"GET {url}", "status": status, "fetched_at": self.now,
                              "etag": None, "body_sha256": sha, "body_path": f"raw/{sha}.json"})
        evidence = {
            "schema_version": "1", "snapshot_id": "snap_20261025T140000Z",
            "application_id": "app1", "github_login": self.login, "fetch_mode": "replay",
            "created_at": self.now, "responses": responses,
            "misses": sorted(f"GET {u}" for u in self.misses), "decisions": self.decisions,
        }
        store.validate(evidence, "evidence")
        (root / "evidence.json").write_bytes(store.dump_json(evidence))
        return Snapshot.load(root)


def project(cid: str = "c1", name: str | None = "proj", **kw: Any) -> dict[str, Any]:
    a = {"behavior": None, "language": None, "min_stars": None, "public_signal": "none",
         "repo_name": name, "repo_url": None, "role": None, "scale": None, "start_year": None}
    a.update(kw)
    return {"claim_id": cid, "type": "project", "section": "Projects", "needs_review": False,
            "source_text": f"{name} project", "assertions": a}


def skill(cid: str, name: str, kind: str = "language") -> dict[str, Any]:
    return {"claim_id": cid, "type": "skill", "section": "Skills", "needs_review": False,
            "source_text": f"Skills: {name}", "assertions": {"name": name, "kind": kind}}


def unchecked(cid: str = "c9", category: str = "employment", needs_review: bool = False):
    return {"claim_id": cid, "type": "unchecked", "section": None, "needs_review": needs_review,
            "source_text": "Worked somewhere", "assertions": {"category": category}}


def oss(cid: str = "c8", repo: str = "someone/lib") -> dict[str, Any]:
    return {"claim_id": cid, "type": "oss_contribution", "section": None, "needs_review": False,
            "source_text": f"Contributed to {repo}",
            "assertions": {"repo": repo, "role": "contributor", "min_prs": 1, "description": None}}


def doc(claims: list[dict[str, Any]], login: str | None = "alice", *,
        conflict: dict[str, str] | None = None, name: str = "Alice Doe",
        emails: list[str] | None = None) -> dict[str, Any]:
    return {
        "schema_version": "1", "application_id": "app1", "claims_version": 1,
        "created_at": "2026-10-25T14:02:11Z",
        "candidate": {"name": name, "emails": emails if emails is not None else ["alice@example.com"],
                      "github_login": login,
                      "github_login_source": "resume_link" if login else "none",
                      "github_login_conflict": conflict},
        "extraction": {"model": "m", "prompt_version": "extract-v1", "temperature": 0,
                       "resume_sha256": "0" * 64, "status": "ok"},
        "edits": [], "claims": claims,
    }


def verdict(tmp_path: Path, claims: list[dict[str, Any]], world: World | None = None, *,
            traces: list[dict[str, Any]] | None = None, **doc_kw: Any) -> dict[str, Any]:
    w = world or World(tmp_path)
    d = doc(claims, w.login, **doc_kw)
    v = rubric.compute_verdict(d, w.build(), traces or [], verdict_n=1, computed_at=COMPUTED)
    store.validate(v, "verdict")
    return v


def only(v: dict[str, Any]) -> dict[str, Any]:
    assert len(v["claims"]) == 1
    return v["claims"][0]


def check(vc: dict[str, Any], name: str) -> dict[str, Any]:
    found = [c for c in vc["checks"] if c["check"] == name]
    assert found, f"no {name} check in {[c['check'] for c in vc['checks']]}"
    return found[0]


def names(vc: dict[str, Any]) -> list[str]:
    return [c["check"] for c in vc["checks"]]


# ── Language names ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("claimed, repo_lang, expected", [
    ("Python", "Python", True), ("python", "PYTHON", True), ("py", "Python", True),
    ("js", "JavaScript", True), ("ts", "TypeScript", True), ("golang", "Go", True),
    ("cpp", "C++", True), ("c++", "C++", True), ("c#", "C#", True),
    ("Java", "JavaScript", False), ("JavaScript", "Java", False),
    ("C", "C++", True), ("C++", "C", True), ("C", "C#", False),
    ("Python", "Jupyter Notebook", True), ("Jupyter Notebook", "Python", False),
    ("jupyter", "Jupyter Notebook", True),
    ("JavaScript", "TypeScript", True), ("TypeScript", "JavaScript", False),
])
def test_language_counting(claimed, repo_lang, expected):
    assert rubric.language_counts_for(claimed, repo_lang) is expected


@pytest.mark.parametrize("text, parts", [
    ("Python and JavaScript", ["Python", "JavaScript"]),
    ("Python/JS", ["Python", "JavaScript"]),
    ("Python, Go & Rust", ["Python", "Go", "Rust"]),
    ("C/C++", ["C", "C++"]),
    ("Dart", ["Dart"]),
    ("Brand and Sand", ["Brand", "Sand"]),
    ("py and PY", ["Python"]),
])
def test_split_languages(text, parts):
    assert rubric.split_languages(text) == parts


def test_multi_language_claim_takes_worst_part(tmp_path):
    w = World(tmp_path)
    w.repo("proj", languages={"Python": 900, "Go": 100})
    vc = only(verdict(tmp_path, [project(language="Python and Rust")], w))
    chk = check(vc, "language")
    assert chk["result"] == "hard" and chk["reason"] == "No Rust code in the repo"
    assert vc["label"] == "contradicted"


def test_multi_language_claim_all_pass(tmp_path):
    w = World(tmp_path)
    w.repo("proj", languages={"Python": 560, "JavaScript": 340, "HTML": 100})
    vc = only(verdict(tmp_path, [project(language="Python/JS")], w))
    chk = check(vc, "language")
    assert chk["result"] == "pass"
    assert chk["reason"] == "Python is the main language; JavaScript is 34% of the code"
    assert chk["facts"]["languages"] == {"Python": 56, "JavaScript": 34, "HTML": 10}


def test_typescript_counts_for_javascript_not_reverse(tmp_path):
    w = World(tmp_path)
    w.repo("site", languages={"TypeScript": 800, "CSS": 200})
    vc = only(verdict(tmp_path, [project("c1", "site", language="JavaScript")], w))
    assert check(vc, "language")["result"] == "pass"
    vc = only(verdict(tmp_path, [project("c1", "site", language="TypeScript")], w))
    assert check(vc, "language")["result"] == "pass"
    w2 = World(tmp_path / "b")
    w2.repo("site", languages={"JavaScript": 800, "CSS": 200})
    vc = only(verdict(tmp_path, [project("c1", "site", language="TypeScript")], w2))
    assert check(vc, "language")["result"] == "hard"


# ── Thresholds ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("created, claim_year, result", [
    ("2024-06-01T00:00:00Z", 2024, "pass"), ("2023-06-01T00:00:00Z", 2024, "pass"),
    ("2025-01-01T00:00:00Z", 2024, "soft"), ("2026-10-24T00:00:00Z", 2024, "hard"),
    ("2026-10-24T00:00:00Z", 2025, "soft"), ("2026-10-24T00:00:00Z", 2026, "pass"),
])
def test_start_date_thresholds(tmp_path, created, claim_year, result):
    w = World(tmp_path)
    w.repo("proj", created=created)
    vc = only(verdict(tmp_path, [project(start_year=claim_year)], w))
    chk = check(vc, "start_date")
    assert chk["result"] == result
    assert chk["facts"] == {"repo_created_at": created[:10], "claim_start_year": claim_year}


def test_start_date_reason(tmp_path):
    w = World(tmp_path)
    w.repo("proj", created="2024-12-05T21:14:03Z")
    vc = only(verdict(tmp_path, [project(start_year=2024)], w))
    assert check(vc, "start_date")["reason"] == "Created Dec 2024; claim says 2024"


@pytest.mark.parametrize("have, result", [
    (100, "pass"), (80, "pass"), (79, "soft"), (50, "soft"), (49, "hard"), (0, "hard"),
])
def test_stars_thresholds(tmp_path, have, result):
    w = World(tmp_path)
    w.repo("proj", stars=have)
    vc = only(verdict(tmp_path, [project(min_stars=100)], w))
    chk = check(vc, "stars")
    assert chk["result"] == result
    assert chk["reason"] == f"{have} stars; claim says 100"


def _share(tmp_path, matched, total, role):
    w = World(tmp_path)
    contribs = [{"login": "alice", "contributions": matched}]
    if total > matched:
        contribs.append({"login": "bob", "contributions": total - matched})
    w.repo("proj", contributors=contribs, commits=total)
    return only(verdict(tmp_path, [project(role=role)], w))


@pytest.mark.parametrize("role, matched, result", [
    ("creator", 100, "pass"), ("creator", 50, "pass"), ("creator", 49, "soft"),
    ("creator", 20, "soft"), ("creator", 19, "hard"), ("creator", 0, "hard"),
    ("co-creator", 20, "pass"), ("co-creator", 19, "soft"), ("co-creator", 5, "soft"),
    ("co-creator", 4, "hard"),
    ("maintainer", 20, "pass"), ("maintainer", 19, "soft"), ("maintainer", 5, "soft"),
    ("maintainer", 4, "hard"),
])
def test_commit_share_thresholds(tmp_path, role, matched, result):
    vc = _share(tmp_path, matched, 100, role)
    chk = check(vc, "commit_share")
    assert chk["result"] == result
    assert chk["reason"] == f"{matched} of 100 commits by the candidate"
    assert chk["facts"]["total"] == 100 and chk["facts"]["matched"] == matched


def test_commit_share_not_checked_without_role(tmp_path):
    w = World(tmp_path)
    w.repo("proj")
    assert "commit_share" not in names(only(verdict(tmp_path, [project()], w)))


def test_commit_share_nothing_matched_is_hard(tmp_path):
    w = World(tmp_path)
    w.repo("proj", contributors=[{"login": "bob", "contributions": 7}])
    chk = check(only(verdict(tmp_path, [project(role="creator")], w)), "commit_share")
    assert chk["result"] == "hard"


def test_commit_share_unlinked_commits_add_note(tmp_path):
    w = World(tmp_path)
    w.repo("proj", contributors=[{"email": "alice@example.com", "name": "Whoever",
                                  "type": "Anonymous", "contributions": 4}])
    vc = only(verdict(tmp_path, [project(role="creator")], w))
    chk = check(vc, "commit_share")
    assert chk["result"] == "pass" and chk["facts"]["by_method"] == {"login": 0, "email": 4, "name": 0}
    assert vc["notes"] == ["Commits not linked to the GitHub account"]


def test_commit_share_linked_commits_no_note(tmp_path):
    w = World(tmp_path)
    w.repo("proj")
    assert only(verdict(tmp_path, [project(role="creator")], w))["notes"] == []


def test_commit_share_contributors_unavailable_is_skip(tmp_path):
    w = World(tmp_path)
    w.repo("proj", contributors="miss")
    vc = only(verdict(tmp_path, [project(role="creator")], w))
    assert check(vc, "commit_share")["result"] == "skip"
    assert vc["label"] == "supported"        # a skip never lowers the label


@pytest.mark.parametrize("langs, result, reason", [
    ({"Python": 900, "Go": 100}, "pass", "Python is the main language"),
    ({"Go": 800, "Python": 200}, "pass", "Python is 20% of the code"),
    ({"Go": 801, "Python": 199}, "soft", "Python is 19% of the code"),
    ({"Go": 999, "Python": 1}, "soft", "Python is 1% of the code"),
    ({"Go": 1000}, "hard", "No Python code in the repo"),
    ({}, "hard", "No Python code in the repo"),
])
def test_language_thresholds(tmp_path, langs, result, reason):
    w = World(tmp_path)
    w.repo("proj", languages=langs)
    chk = check(only(verdict(tmp_path, [project(language="Python")], w)), "language")
    assert (chk["result"], chk["reason"]) == (result, reason)


def test_language_unavailable_is_skip(tmp_path):
    w = World(tmp_path)
    w.repo("proj", languages="miss")
    assert check(only(verdict(tmp_path, [project(language="Python")], w)), "language")["result"] == "skip"


def test_language_alias_in_claim(tmp_path):
    w = World(tmp_path)
    w.repo("proj", languages={"JavaScript": 900, "CSS": 100})
    chk = check(only(verdict(tmp_path, [project(language="JS")], w)), "language")
    assert chk["result"] == "pass" and chk["reason"] == "JavaScript is the main language"


@pytest.mark.parametrize("size, commits, result", [
    (10, 2, "pass"), (9, 2, "soft"), (500, 1, "soft"), (500, 2, "pass"), (3, 1, "soft"),
])
def test_substance_thresholds(tmp_path, size, commits, result):
    w = World(tmp_path)
    w.repo("proj", size=size, commits=commits)
    vc = only(verdict(tmp_path, [project(scale="substantial")], w))
    assert check(vc, "substance")["result"] == result


def test_substance_wording(tmp_path):
    w = World(tmp_path)
    w.repo("proj", size=3, commits=2, commit_days=1)
    chk = check(only(verdict(tmp_path, [project(scale="substantial")], w)), "substance")
    assert chk["reason"] == ("Described as a complete project, but the repo is 3 KB "
                             "with 2 commits on 1 day")
    w = World(tmp_path / "b")
    w.repo("proj", size=300, commits=1)
    chk = check(only(verdict(tmp_path, [project(scale="substantial")], w)), "substance")
    assert chk["reason"].endswith("with 1 commit on 1 day")
    w = World(tmp_path / "c")
    w.repo("proj", size=2150, commits=100, commit_days=3)
    chk = check(only(verdict(tmp_path, [project(scale="substantial")], w)), "substance")
    assert chk["result"] == "pass" and chk["reason"] == "Repo is 2150 KB with 100+ commits"


def test_substance_only_for_substantial(tmp_path):
    w = World(tmp_path)
    w.repo("proj", size=1, commits=1)
    assert "substance" not in names(only(verdict(tmp_path, [project(scale="small")], w)))
    assert "substance" not in names(only(verdict(tmp_path, [project(scale=None)], w)))


def test_substance_empty_repo(tmp_path):
    w = World(tmp_path)
    w.repo("proj", size=0, commits="empty", languages={}, contributors=None)
    vc = only(verdict(tmp_path, [project(scale="substantial")], w))
    chk = check(vc, "substance")
    assert chk["result"] == "soft" and "with 0 commits on 0 days" in chk["reason"]


def test_assertion_unset_means_check_absent(tmp_path):
    w = World(tmp_path)
    w.repo("proj")
    vc = only(verdict(tmp_path, [project()], w))
    assert names(vc) == ["repo", "fork"]


def test_check_order(tmp_path):
    w = World(tmp_path)
    w.repo("proj", size=3, commits=2)
    vc = only(verdict(tmp_path, [project(role="creator", language="Python", min_stars=0,
                                         start_year=2024, scale="substantial")], w))
    assert names(vc) == ["repo", "fork", "start_date", "stars", "commit_share", "language",
                         "substance"]


# ── repo and fork ────────────────────────────────────────────────────────────

def test_repo_matched_by_name_ignoring_case(tmp_path):
    w = World(tmp_path)
    w.repo("Polytrack-ML")
    vc = only(verdict(tmp_path, [project(name="polytrack-ml")], w))
    chk = check(vc, "repo")
    assert chk["result"] == "pass"
    assert chk["reason"] == "Repo alice/Polytrack-ML (matched by name)"
    assert chk["facts"] == {"repo": "alice/Polytrack-ML", "method": "name"}
    assert vc["evidence_links"] == ["https://github.com/alice/Polytrack-ML"]


def test_repo_matched_by_url(tmp_path):
    w = World(tmp_path)
    w.repo("lib", owner="acme-org", listed=False)
    c = project(name=None, repo_url="https://github.com/acme-org/lib", public_signal="link")
    vc = only(verdict(tmp_path, [c], w))
    assert check(vc, "repo")["reason"] == "Repo acme-org/lib (linked on the resume)"
    assert check(vc, "repo")["facts"]["method"] == "url"


def test_repo_matched_by_model_decision(tmp_path):
    w = World(tmp_path)
    w.repo("Real-Name")
    w.decisions.append({"kind": "repo_match", "claim_id": "c1", "model": "m",
                        "prompt_version": "match-v1", "inputs": {}, "output": "Real-Name"})
    vc = only(verdict(tmp_path, [project(name="the racing thing")], w))
    assert check(vc, "repo")["reason"] == "Repo alice/Real-Name (matched by the model)"


def test_repo_not_found_with_link_is_hard(tmp_path):
    w = World(tmp_path)
    w.repo("other")
    w.decide("c1")
    vc = only(verdict(tmp_path, [project(name="ghost", public_signal="link")], w))
    assert vc["label"] == "contradicted" and names(vc) == ["repo"]
    assert vc["top_reason"] == ("Repo not found: No repo named ghost among @alice's 1 public repos; "
                              "the repo-match model chose none")
    assert vc["evidence_links"] == []


def test_repo_not_found_with_open_source_wording_is_hard(tmp_path):
    w = World(tmp_path)
    w.repo("other")
    w.decide("c1")
    vc = only(verdict(tmp_path, [project(name="ghost", public_signal="open_source_wording")], w))
    assert vc["label"] == "contradicted"


def test_repo_not_found_with_no_signal_is_skip(tmp_path):
    w = World(tmp_path)
    w.repo("other")
    w.decide("c1")
    vc = only(verdict(tmp_path, [project(name="ghost", public_signal="none")], w))
    assert vc["label"] == "no_evidence" and names(vc) == ["repo"]
    assert check(vc, "repo")["result"] == "skip"
    assert vc["top_reason"].startswith("No matching public repo: No repo named ghost")


def test_repo_model_decision_none_recorded(tmp_path):
    w = World(tmp_path)
    w.repo("other")
    w.decisions.append({"kind": "repo_match", "claim_id": "c1", "model": "m",
                        "prompt_version": "match-v1", "inputs": {}, "output": "none"})
    vc = only(verdict(tmp_path, [project(name="the thing", public_signal="link")], w))
    assert vc["label"] == "contradicted"


def test_repo_model_decision_missing_is_skip_never_red(tmp_path):
    w = World(tmp_path)
    w.repo("other")
    vc = only(verdict(tmp_path, [project(name="the thing", public_signal="link")], w))
    assert vc["label"] == "no_evidence"
    assert vc["top_reason"] == "Couldn't match the repo"


def test_repo_details_unavailable_is_skip(tmp_path):
    w = World(tmp_path)
    w.repo("proj", detail="miss")
    vc = only(verdict(tmp_path, [project(public_signal="link")], w))
    assert vc["label"] == "no_evidence"
    assert vc["top_reason"] == "Repo details unavailable offline"


def test_repo_list_unavailable_is_skip(tmp_path):
    w = World(tmp_path)
    w.repo("proj")
    w.repo_list_miss = True
    vc = only(verdict(tmp_path, [project(public_signal="link")], w))
    assert vc["label"] == "no_evidence"
    assert vc["top_reason"] == "Repo details unavailable offline"


def test_repo_url_404_is_hard(tmp_path):
    w = World(tmp_path)
    w.repo("lib", owner="acme-org", detail="404", listed=False)
    c = project(name=None, repo_url="https://github.com/acme-org/lib", public_signal="link")
    vc = only(verdict(tmp_path, [c], w))
    assert vc["label"] == "contradicted"
    assert vc["top_reason"].startswith("Repo not found: ")


def test_repo_url_detail_missing_is_skip(tmp_path):
    w = World(tmp_path)
    w.repo("lib", owner="acme-org", detail="miss", listed=False)
    c = project(name=None, repo_url="https://github.com/acme-org/lib", public_signal="link")
    vc = only(verdict(tmp_path, [c], w))
    assert vc["label"] == "no_evidence"
    assert vc["top_reason"] == "Repo details unavailable offline"


def test_fork_creator_is_hard(tmp_path):
    w = World(tmp_path)
    w.repo("proj", fork=True, parent="big/proj")
    vc = only(verdict(tmp_path, [project(role="creator")], w))
    chk = check(vc, "fork")
    assert chk["result"] == "hard" and chk["facts"] == {"parent": "big/proj"}
    assert vc["label"] == "contradicted"


@pytest.mark.parametrize("role", ["maintainer", "co-creator", None])
def test_fork_other_role_is_skip_with_note(tmp_path, role):
    w = World(tmp_path)
    w.repo("proj", fork=True, parent="big/proj")
    vc = only(verdict(tmp_path, [project(role=role)], w))
    assert check(vc, "fork")["result"] == "skip"
    assert "Fork of big/proj" in vc["notes"]
    assert vc["label"] != "contradicted"


def test_not_a_fork(tmp_path):
    w = World(tmp_path)
    w.repo("proj")
    chk = check(only(verdict(tmp_path, [project(role="creator")], w)), "fork")
    assert chk["result"] == "pass" and chk["reason"] == "Not a fork"


# ── Combining rule and top_reason ────────────────────────────────────────────

def test_passes_do_not_cancel_soft(tmp_path):
    w = World(tmp_path)
    w.repo("proj", stars=60, size=500)
    vc = only(verdict(tmp_path, [project(min_stars=100, role="creator", language="Python")], w))
    assert check(vc, "stars")["result"] == "soft"
    assert check(vc, "commit_share")["result"] == "pass"
    assert vc["label"] == "partial"


def test_hard_beats_soft(tmp_path):
    w = World(tmp_path)
    w.repo("proj", stars=60, created="2026-01-01T00:00:00Z")
    vc = only(verdict(tmp_path, [project(min_stars=100, start_year=2023)], w))
    assert vc["label"] == "contradicted"
    assert vc["top_reason"] == "Created Jan 2026; claim says 2023"   # first hard, not the soft


def test_top_reason_first_soft_when_no_hard(tmp_path):
    w = World(tmp_path)
    w.repo("proj", stars=60, size=3, commits=2)
    vc = only(verdict(tmp_path, [project(min_stars=100, scale="substantial")], w))
    assert vc["top_reason"] == "60 stars; claim says 100"


def test_top_reason_best_pass_joins_commit_share_and_language(tmp_path):
    w = World(tmp_path)
    w.repo("proj", languages={"Python": 900, "Go": 100}, commits=4)
    vc = only(verdict(tmp_path, [project(role="creator", language="Python", start_year=2024)], w))
    assert vc["label"] == "supported"
    assert vc["top_reason"] == "4 of 4 commits by the candidate; Python is the main language"


def test_top_reason_best_pass_order(tmp_path):
    w = World(tmp_path)
    w.repo("proj", created="2024-01-01T00:00:00Z", stars=500)
    vc = only(verdict(tmp_path, [project(start_year=2024, min_stars=100)], w))
    assert vc["top_reason"] == "Created Jan 2024; claim says 2024"      # start_date before stars
    vc = only(verdict(tmp_path, [project(min_stars=100)], w))
    assert vc["top_reason"] == "500 stars; claim says 100"
    vc = only(verdict(tmp_path, [project()], w))
    assert vc["top_reason"] == "Not a fork"                              # fork before repo


def test_top_reason_commit_share_alone(tmp_path):
    w = World(tmp_path)
    w.repo("proj", commits=3)
    vc = only(verdict(tmp_path, [project(role="creator", start_year=2024)], w))
    assert vc["top_reason"] == "3 of 3 commits by the candidate"


def test_skipped_checks_do_not_change_label(tmp_path):
    w = World(tmp_path)
    w.repo("proj", languages="miss", contributors="miss")
    vc = only(verdict(tmp_path, [project(role="creator", language="Python")], w))
    assert vc["label"] == "supported"


# ── Skills ───────────────────────────────────────────────────────────────────

def _skill_world(tmp_path, repos):
    w = World(tmp_path)
    for name, langs, kw in repos:
        w.repo(name, languages=langs, **kw)
    return w


def test_skill_main_language(tmp_path):
    w = _skill_world(tmp_path, [("a", {"Python": 900, "Go": 100}, {})])
    vc = only(verdict(tmp_path, [skill("c1", "Python")], w))
    assert vc["label"] == "supported" and vc["top_reason"] == "Main language in 1 repo"
    chk = check(vc, "skill_presence")
    assert chk["result"] == "pass" and chk["facts"] == {"repos": [{"repo": "alice/a", "percent": 90}]}
    assert vc["evidence_links"] == ["https://github.com/alice/a"]


def test_skill_twenty_percent_in_one_repo_is_supported(tmp_path):
    w = _skill_world(tmp_path, [("a", {"Go": 800, "Python": 200}, {})])
    vc = only(verdict(tmp_path, [skill("c1", "Python")], w))
    assert vc["label"] == "supported"
    assert vc["top_reason"] == "In 1 repo, up to 20% of the code"


def test_skill_present_in_two_small_repos_is_supported(tmp_path):
    w = _skill_world(tmp_path, [("a", {"Go": 950, "Python": 50}, {}),
                                ("b", {"Go": 900, "Python": 100}, {})])
    vc = only(verdict(tmp_path, [skill("c1", "Python")], w))
    assert vc["label"] == "supported"
    assert vc["top_reason"] == "In 2 repos, up to 10% of the code"


def test_skill_one_small_repo_is_partial(tmp_path):
    w = _skill_world(tmp_path, [("a", {"Go": 950, "Python": 50}, {}), ("b", {"Go": 1}, {})])
    vc = only(verdict(tmp_path, [skill("c1", "Python")], w))
    assert vc["label"] == "partial" and vc["top_reason"] == "Only in a, 5% of the code"
    assert check(vc, "skill_presence")["result"] == "soft"


def test_skill_absent_is_no_evidence_never_red(tmp_path):
    w = _skill_world(tmp_path, [("a", {"Go": 1}, {}), ("b", {"Go": 1}, {})])
    vc = only(verdict(tmp_path, [skill("c1", "Rust")], w))
    assert vc["label"] == "no_evidence"
    assert vc["top_reason"] == "No public Rust code found in @alice's 2 repos"
    assert check(vc, "skill_presence")["result"] == "skip"


def test_skill_ignores_forks_and_only_looks_at_top_30(tmp_path):
    w = World(tmp_path)
    w.repo("forked", fork=True, languages={"Rust": 1000}, pushed="2026-10-20T00:00:00Z")
    for i in range(30):
        w.repo(f"r{i:02d}", languages={"Go": 10}, pushed=f"2026-09-{(i % 28) + 1:02d}T00:00:00Z")
    w.repo("ancient", languages={"Rust": 1000}, pushed="2019-01-01T00:00:00Z")
    vc = only(verdict(tmp_path, [skill("c1", "Rust")], w))
    assert vc["label"] == "no_evidence"
    assert "@alice's 30 repos" in vc["top_reason"]


def test_skill_aliases_and_counting_rules(tmp_path):
    w = _skill_world(tmp_path, [("a", {"TypeScript": 900, "CSS": 100}, {}),
                                ("b", {"Jupyter Notebook": 700, "Go": 300}, {}),
                                ("c", {"C": 600, "Go": 400}, {})])
    assert only(verdict(tmp_path, [skill("c1", "JS")], w))["label"] == "supported"
    assert only(verdict(tmp_path, [skill("c1", "Python")], w))["label"] == "supported"
    assert only(verdict(tmp_path, [skill("c1", "cpp")], w))["label"] == "supported"
    assert only(verdict(tmp_path, [skill("c1", "TypeScript")], w))["label"] == "supported"
    assert only(verdict(tmp_path, [skill("c1", "Java")], w))["label"] == "no_evidence"


def test_skill_javascript_does_not_match_java(tmp_path):
    w = _skill_world(tmp_path, [("a", {"JavaScript": 1000}, {})])
    assert only(verdict(tmp_path, [skill("c1", "Java")], w))["label"] == "no_evidence"


def test_skill_multi_language_takes_worst(tmp_path):
    w = _skill_world(tmp_path, [("a", {"Python": 900, "Go": 100}, {})])
    vc = only(verdict(tmp_path, [skill("c1", "Python and Rust")], w))
    assert vc["label"] == "no_evidence"
    vc = only(verdict(tmp_path, [skill("c1", "Python, Go")], w))
    assert vc["label"] == "partial"           # Go is only 10% of one repo
    vc = only(verdict(tmp_path, [skill("c1", "Python/Python")], w))
    assert vc["label"] == "supported"


def test_skill_facts_cap_at_ten_highest_first_and_links_at_three(tmp_path):
    w = World(tmp_path)
    for i in range(12):
        w.repo(f"r{i:02d}", languages={"Python": 100 + i, "Go": 1000})
    vc = only(verdict(tmp_path, [skill("c1", "Python")], w))
    repos = check(vc, "skill_presence")["facts"]["repos"]
    assert len(repos) == 10
    assert [r["percent"] for r in repos] == sorted((r["percent"] for r in repos), reverse=True)
    assert len(vc["evidence_links"]) == 3


def test_skill_framework_tool(tmp_path):
    w = World(tmp_path)
    w.repo("a")
    vc = only(verdict(tmp_path, [skill("c1", "React", kind="framework_tool")], w))
    assert vc["label"] == "no_evidence"
    assert vc["top_reason"] == "Framework or tool; not checked in v1"
    assert check(vc, "skill_presence")["result"] == "skip"
    assert vc["evidence_links"] == []


def test_skill_languages_unavailable_offline(tmp_path):
    w = World(tmp_path)
    w.repo("a", languages="miss")
    vc = only(verdict(tmp_path, [skill("c1", "Python")], w))
    assert vc["label"] == "no_evidence" and vc["top_reason"] == "Evidence unavailable offline"


def test_skill_repo_list_unavailable_offline(tmp_path):
    w = World(tmp_path)
    w.repo_list_miss = True
    vc = only(verdict(tmp_path, [skill("c1", "Python")], w))
    assert vc["label"] == "no_evidence" and vc["top_reason"] == "Evidence unavailable offline"


# ── unchecked, oss, no login ─────────────────────────────────────────────────

def test_unchecked_claims(tmp_path):
    v = verdict(tmp_path, [unchecked("c1"), unchecked("c2", "other", needs_review=True)])
    a, b = v["claims"]
    assert (a["label"], a["checks"], a["top_reason"]) == ("no_evidence", [], "Not verifiable from public code")
    assert b["top_reason"] == "Couldn't classify this claim; check the resume."
    assert a["evidence_links"] == []


def test_oss_not_run_yet(tmp_path):
    vc = only(verdict(tmp_path, [oss()]))
    assert vc["label"] == "no_evidence"
    assert vc["top_reason"] == "Contribution check not run"
    assert check(vc, "contribution")["result"] == "skip"


def test_no_login(tmp_path):
    w = World(tmp_path, login=None)
    v = verdict(tmp_path, [project(), skill("c2", "Python"), oss("c3"), unchecked("c4")], w)
    assert v["account"] is None
    for vc in v["claims"][:3]:
        assert vc["label"] == "no_evidence" and vc["checks"] == []
        assert vc["top_reason"] == "No GitHub account found"
    assert v["claims"][3]["top_reason"] == "Not verifiable from public code"
    assert v["context"] == ["No GitHub account found on the resume or application."]
    assert v["tally"] == {"supported": 0, "partial": 0, "contradicted": 0, "no_evidence": 4}


# ── Verdict-level fields ─────────────────────────────────────────────────────

def test_tally_counts_every_claim(tmp_path):
    w = World(tmp_path)
    w.repo("good")
    w.repo("bad", stars=0)
    w.decide("c3")
    claims = [project("c1", "good"), project("c2", "bad", min_stars=100),
              project("c3", "ghost", public_signal="link"), skill("c4", "Python"),
              unchecked("c5"), oss("c6")]
    v = verdict(tmp_path, claims, w)
    assert [c["label"] for c in v["claims"]] == ["supported", "contradicted", "contradicted",
                                                 "supported", "no_evidence", "no_evidence"]
    assert v["tally"] == {"supported": 2, "partial": 0, "contradicted": 2, "no_evidence": 2}


def test_claims_in_claim_order(tmp_path):
    w = World(tmp_path)
    w.repo("a")
    v = verdict(tmp_path, [unchecked("c3"), skill("c1", "Python"), project("c2", "a")], w)
    assert [c["claim_id"] for c in v["claims"]] == ["c3", "c1", "c2"]


def test_inputs_and_ids(tmp_path):
    w = World(tmp_path)
    w.repo("a")
    d = doc([project("c1", "a"), project("c10", "a"), project("c2", "a")], w.login)
    snap = w.build()
    traces = [{"schema_version": "1", "claim_id": cid, "repo": "alice/a", "commit_sha": "0" * 40,
               "sandbox_backend": "stub",
               "baseline": {"step": "skip", "command": None, "exit_code": None, "duration_s": 0.0,
                            "timed_out": False, "failure_kind": None, "output_excerpt": "",
                            "output_path": None}, "agent": None, "judge": None}
              for cid in ("c2", "c1")]
    v = rubric.compute_verdict(d, snap, traces, verdict_n=7, computed_at=COMPUTED)
    assert v["verdict_id"] == "verdict_7" and v["rubric_version"] == "rubric-v1"
    assert v["application_id"] == "app1" and v["computed_at"] == COMPUTED
    inp = v["inputs"]
    assert inp["claims_version"] == 1 and inp["snapshot_id"] == snap.snapshot_id
    assert inp["claims_sha256"] == store.sha256_json(d)
    assert inp["evidence_sha256"] == store.sha256_json(snap.evidence)
    assert [t["claim_id"] for t in inp["run_traces"]] == ["c1", "c2"]
    assert inp["run_traces"][0]["sha256"] == store.sha256_json(traces[1])


def test_deterministic(tmp_path):
    w = World(tmp_path)
    w.repo("a", languages={"Python": 10, "Go": 90})
    d = doc([project("c1", "a", role="creator", language="Python"), skill("c2", "Python")], w.login)
    snap = w.build()
    one = rubric.compute_verdict(d, snap, [], verdict_n=1, computed_at=COMPUTED)
    two = rubric.compute_verdict(d, snap, [], verdict_n=1, computed_at=COMPUTED)
    assert store.dump_json(one) == store.dump_json(two)


def test_account_fields(tmp_path):
    w = World(tmp_path, public_repos=5, followers=4, created="2021-09-03T19:42:11Z")
    v = verdict(tmp_path, [unchecked()], w, conflict={"login": "bob", "source": "greenhouse_website"})
    assert v["account"] == {"login": "alice", "login_source": "resume_link",
                            "login_conflict": {"login": "bob", "source": "greenhouse_website"},
                            "created_at": "2021-09-03", "public_repos": 5, "followers": 4}


def test_account_404_keeps_login_and_nulls_numbers(tmp_path):
    w = World(tmp_path, user_status=404)
    v = verdict(tmp_path, [skill("c1", "Python")], w)
    assert v["account"] == {"login": "alice", "login_source": "resume_link", "login_conflict": None,
                            "created_at": None, "public_repos": None, "followers": None}
    assert v["context"] == ["GitHub account @alice was not found."]


def test_account_miss_keeps_login_and_nulls_numbers(tmp_path):
    w = World(tmp_path, user_status=0)
    v = verdict(tmp_path, [unchecked()], w)
    assert v["account"]["login"] == "alice" and v["account"]["created_at"] is None
    assert v["context"] == ["Some GitHub evidence was unavailable offline (1 requests)."]


# ── Context lines ────────────────────────────────────────────────────────────

def test_context_account_created(tmp_path):
    w = World(tmp_path, created="2021-09-03T19:42:11Z")
    assert verdict(tmp_path, [unchecked()], w)["context"] == ["Account created Sep 2021."]


def test_context_recent_activity_more_than_half(tmp_path):
    w = World(tmp_path)
    w.repo("a", pushed="2026-10-20T00:00:00Z")
    w.repo("b", pushed="2026-10-01T00:00:00Z")
    w.repo("c", pushed="2025-01-01T00:00:00Z")
    ctx = verdict(tmp_path, [unchecked()], w)["context"]
    assert ctx == ["Account created Jan 2020.", "Most public activity is from the last 30 days."]


def test_context_recent_activity_exactly_half_is_not_enough(tmp_path):
    w = World(tmp_path)
    w.repo("a", pushed="2026-10-20T00:00:00Z")
    w.repo("b", pushed="2025-01-01T00:00:00Z")
    assert verdict(tmp_path, [unchecked()], w)["context"] == ["Account created Jan 2020."]


def test_context_recent_window_is_30_days_before_snapshot(tmp_path):
    w = World(tmp_path)
    w.repo("a", pushed="2026-09-25T14:00:00Z")      # exactly 30 days before NOW: inside
    assert "Most public activity is from the last 30 days." in verdict(tmp_path, [unchecked()], w)["context"]
    w = World(tmp_path / "b")
    w.repo("a", pushed="2026-09-25T13:59:59Z")
    assert "Most public activity is from the last 30 days." not in verdict(tmp_path, [unchecked()], w)["context"]


def test_context_recent_activity_ignores_forks(tmp_path):
    w = World(tmp_path)
    w.repo("f1", fork=True, pushed="2026-10-20T00:00:00Z")
    w.repo("f2", fork=True, pushed="2026-10-20T00:00:00Z")
    w.repo("a", pushed="2025-01-01T00:00:00Z")
    assert "Most public activity is from the last 30 days." not in verdict(tmp_path, [unchecked()], w)["context"]


def test_context_login_conflict(tmp_path):
    w = World(tmp_path)
    v = verdict(tmp_path, [unchecked()], w, conflict={"login": "bob", "source": "greenhouse_website"})
    assert "Resume links @alice; the application lists @bob. Using @alice." in v["context"]


def test_context_order(tmp_path):
    w = World(tmp_path)
    w.repo("a", pushed="2026-10-20T00:00:00Z")
    w.miss(match.languages_url("alice/a"))
    v = verdict(tmp_path, [unchecked()], w, conflict={"login": "bob", "source": "greenhouse_social"})
    assert v["context"] == [
        "Account created Jan 2020.",
        "Most public activity is from the last 30 days.",
        "Resume links @alice; the application lists @bob. Using @alice.",
        "Some GitHub evidence was unavailable offline (1 requests).",
    ]
    w = World(tmp_path / "b", user_status=404)
    assert verdict(tmp_path, [unchecked()], w)["context"] == ["GitHub account @alice was not found."]


def test_misses_count(tmp_path):
    w = World(tmp_path)
    w.repo("a", languages="miss", contributors="miss", commits="miss")
    ctx = verdict(tmp_path, [unchecked()], w)["context"]
    assert ctx[-1] == "Some GitHub evidence was unavailable offline (3 requests)."


def test_malformed_body_is_skip_not_crash(tmp_path):
    w = World(tmp_path)
    w.repo("proj", languages=["not", "a", "dict"], contributors={"not": "a list"})
    vc = only(verdict(tmp_path, [project(role="creator", language="Python")], w))
    assert check(vc, "language")["result"] == "skip"
    assert check(vc, "commit_share")["result"] == "skip"


def test_claim_key():
    assert rubric.claim_key(project("c1", "Polytrack-ML")) == "project:Polytrack-ML"
    assert rubric.claim_key(skill("c1", "C++")) == "skill:C++"
    assert rubric.claim_key(oss("c1", "a/b")) == "oss:a/b"
    assert rubric.claim_key(unchecked("c1", "education")) == "unchecked:education"
    assert rubric.claim_key(project("c1", None, repo_url="https://github.com/o/lib")) == "project:lib"


def test_rubric_does_not_read_environment_or_clock():
    import inspect
    src = inspect.getsource(rubric)
    for banned in ("os.environ", "datetime.now", "time.time", "import httpx", "import requests", "llm_chat",
                   "getenv"):
        assert banned not in src
