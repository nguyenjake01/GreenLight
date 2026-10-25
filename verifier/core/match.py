"""Repo matching, commit matching and commit timeline: pure functions (design doc J2).

Also home of the GitHub URL builders, so `fetch` (which records) and the rubric (which reads
the snapshot) produce identical request URLs and therefore identical cache keys. `fetch`
re-exports them. Logins and owner/repo names are lowercased in every URL.

Nothing here touches the network, the clock, the disk or an LLM.
"""
from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Protocol
from urllib.parse import quote

API_BASE = "https://api.github.com"


# ── URL builders (the only place request URLs are made) ───────────────────────

def _seg(name: str) -> str:
    return quote(str(name).strip().lower(), safe="")


def _repo_path(repo: str) -> str:
    owner, _, name = str(repo).strip().partition("/")
    return f"{_seg(owner)}/{_seg(name)}"


def github_url(path: str, query: str = "") -> str:
    """`API_BASE + path`, with an already-encoded query string appended when given."""
    return f"{API_BASE}{path}" + (f"?{query}" if query else "")


def user_url(login: str) -> str:
    return github_url(f"/users/{_seg(login)}")


def repos_list_url(login: str) -> str:
    return github_url(f"/users/{_seg(login)}/repos", "per_page=100&sort=pushed&type=owner")


def repo_detail_url(repo: str) -> str:
    return github_url(f"/repos/{_repo_path(repo)}")


def languages_url(repo: str) -> str:
    return github_url(f"/repos/{_repo_path(repo)}/languages")


def contributors_url(repo: str) -> str:
    return github_url(f"/repos/{_repo_path(repo)}/contributors", "anon=1&per_page=100")


def commits_url(repo: str) -> str:
    return github_url(f"/repos/{_repo_path(repo)}/commits", "per_page=100")


def author_commits_url(repo: str, login: str) -> str:
    return github_url(f"/repos/{_repo_path(repo)}/commits", f"author={_seg(login)}&per_page=100")


def _search_url(terms: list[str]) -> str:
    return github_url("/search/issues", "q=" + "+".join(terms))


def merged_prs_url(repo: str, login: str) -> str:
    return _search_url(["type:pr", "is:merged", f"author:{_seg(login)}", f"repo:{_repo_path(repo)}"])


def any_prs_url(repo: str, login: str) -> str:
    return _search_url(["type:pr", f"author:{_seg(login)}", f"repo:{_repo_path(repo)}"])


def reviewed_prs_url(repo: str, login: str) -> str:
    return _search_url(["type:pr", f"reviewed-by:{_seg(login)}", f"repo:{_repo_path(repo)}"])


_GH_URL = re.compile(r"^(?:https?://)?(?:www\.)?github\.com/([^/\s?#]+)/([^/\s?#]+)", re.IGNORECASE)


def parse_repo_ref(ref: str | None) -> str | None:
    """'owner/name' from a github.com URL, or from an 'owner/name' string; None otherwise."""
    if not ref or not isinstance(ref, str):
        return None
    ref = ref.strip()
    m = _GH_URL.match(ref)
    if m:
        owner, name = m.group(1), m.group(2)
    elif re.fullmatch(r"[\w.-]+/[\w.-]+", ref):
        owner, name = ref.split("/", 1)
    else:
        return None
    if name.lower().endswith(".git"):
        name = name[:-4]
    return f"{owner}/{name}" if owner and name else None


# ── Matching ──────────────────────────────────────────────────────────────────

class SnapshotLike(Protocol):
    """What `match_repo` needs: a real `Snapshot`, or fetch's in-memory stand-in."""
    evidence: Any

    def get(self, method: str, url: str) -> tuple[int, Any] | None: ...


def _not_found(searched: str) -> dict[str, Any]:
    return {"repo": None, "method": None, "searched": searched}


def owned_repos(snapshot: SnapshotLike) -> list[dict[str, Any]] | None:
    """The login's repo list (first page) from the snapshot; None if absent or unreadable."""
    login = snapshot.evidence.get("github_login")
    if not login:
        return None
    got = snapshot.get("GET", repos_list_url(login))
    if got is None or got[0] != 200 or not isinstance(got[1], list):
        return None
    return [r for r in got[1] if isinstance(r, dict) and r.get("name")]


def match_repo(claim: dict[str, Any], snapshot: SnapshotLike) -> dict[str, Any]:
    """{"repo": "owner/name" | None, "method": "url" | "name" | "llm" | None, "searched": str}.

    1. `repo_url` set: that repo, any owner. A recorded 404/410 on its detail means not found;
       a 200 or no recorded answer means found (the rubric then reports missing evidence).
    2. `repo_name` equals an owned repo's name, ignoring case (forks count).
    3. The recorded `repo_match` decision for this claim.
    4. Not found; `searched` says what was tried.
    """
    if claim.get("type") != "project":
        return _not_found("Not a project claim")
    assertions = claim.get("assertions") or {}
    login = snapshot.evidence.get("github_login") or ""

    repo_url = assertions.get("repo_url")
    if repo_url:
        ref = parse_repo_ref(repo_url)
        if ref is None:
            return _not_found(f"Repo URL {repo_url} is not a GitHub repo URL")
        got = snapshot.get("GET", repo_detail_url(ref))
        if got is not None and got[0] in (404, 410):
            return _not_found(f"Repo URL {repo_url} returned {got[0]} (repo not found)")
        return {"repo": ref, "method": "url", "searched": f"Repo URL {repo_url}"}

    repos = owned_repos(snapshot) if login else None
    if not login:
        return _not_found("No GitHub login to search")
    if repos is None:
        return _not_found(f"@{login}'s public repo list is not available")
    by_lower = {r["name"].lower(): r["name"] for r in repos}

    claimed = (assertions.get("repo_name") or "").strip()
    if claimed:
        bare = claimed
        owner, _, rest = claimed.partition("/")
        if rest and owner.lower() == login.lower():
            bare = rest
        actual = by_lower.get(bare.lower())
        if actual:
            return {"repo": f"{login}/{actual}", "method": "name",
                    "searched": f"Repo named {bare} among @{login}'s {len(repos)} public repos"}

    for d in snapshot.evidence.get("decisions", []):
        if d.get("kind") == "repo_match" and d.get("claim_id") == claim.get("claim_id"):
            output = d.get("output")
            if output and output != "none":
                actual = by_lower.get(str(output).lower(), output)
                return {"repo": f"{login}/{actual}", "method": "llm",
                        "searched": f"Repo chosen by the repo-match model among @{login}'s "
                                    f"{len(repos)} public repos"}
            tried = f"No repo named {claimed} among" if claimed else "No repo name given; looked among"
            return _not_found(f"{tried} @{login}'s {len(repos)} public repos; "
                              "the repo-match model chose none")

    if claimed:
        return _not_found(f"No repo named {claimed} among @{login}'s {len(repos)} public repos")
    return _not_found(f"Claim names no repo; @{login} has {len(repos)} public repos")


# ── Commits ───────────────────────────────────────────────────────────────────

def normalize_name(name: str | None) -> str:
    """Case-folded, accents stripped, punctuation dropped, whitespace collapsed."""
    if not name:
        return ""
    decomposed = unicodedata.normalize("NFKD", str(name))
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    kept = "".join(c for c in stripped.casefold() if c.isalnum() or c.isspace())
    return " ".join(kept.split())  # punctuation dropped, whitespace collapsed


def _as_list(body: Any) -> list[Any]:
    if isinstance(body, (str, bytes)):
        try:
            body = json.loads(body)
        except ValueError:
            return []
    return body if isinstance(body, list) else []


def match_commits(contributors_body: Any, candidate: dict[str, Any]) -> dict[str, Any]:
    """Commit counts attributed to the candidate, over `contributors?anon=1` entries.

    {"total": all commits, "matched": candidate's commits,
     "by_method": {"login": n, "email": n, "name": n}}   (commit counts, not entries)

    Linked entry with the candidate's login (any case) -> login. Anonymous entry with a
    noreply email for the login (`{login}@users.noreply.github.com` or `{id}+{login}@...`)
    -> login; one of the candidate's emails -> email; the same normalized name -> name.
    """
    login = (candidate.get("github_login") or "").lower()
    emails = {e.strip().lower() for e in (candidate.get("emails") or []) if e}
    cand_name = normalize_name(candidate.get("name"))
    noreply = re.compile(rf"^(?:\d+\+)?{re.escape(login)}@users\.noreply\.github\.com$") if login else None

    by_method = {"login": 0, "email": 0, "name": 0}
    total = 0
    for entry in _as_list(contributors_body):
        if not isinstance(entry, dict):
            continue
        n = entry.get("contributions")
        n = n if isinstance(n, int) and n > 0 else 0
        total += n
        entry_login = entry.get("login")
        if entry_login:
            if login and str(entry_login).lower() == login:
                by_method["login"] += n
            continue
        email = (entry.get("email") or "").strip().lower()
        if noreply and email and noreply.match(email):
            by_method["login"] += n
        elif email and email in emails:
            by_method["email"] += n
        elif cand_name and normalize_name(entry.get("name")) == cand_name:
            by_method["name"] += n
    return {"total": total, "matched": sum(by_method.values()), "by_method": by_method}


def _commit_date(commit: dict[str, Any]) -> str | None:
    info = commit.get("commit") if isinstance(commit.get("commit"), dict) else {}
    for who in ("author", "committer"):
        part = info.get(who)
        if isinstance(part, dict) and isinstance(part.get("date"), str) and part["date"]:
            return part["date"]
    return None


def commit_timeline(commits_body: Any) -> dict[str, Any]:
    """{"count", "first", "last", "distinct_days"} over the first 100 commits.

    `first`/`last` are the earliest/latest commit timestamps (ISO strings, as GitHub sent
    them); `distinct_days` counts distinct dates. An empty repo (a 409 message object, an
    empty list) gives count 0 and None dates.
    """
    commits = [c for c in _as_list(commits_body)[:100] if isinstance(c, dict)]
    dates = sorted(d for d in (_commit_date(c) for c in commits) if d)
    return {
        "count": len(commits),
        "first": dates[0] if dates else None,
        "last": dates[-1] if dates else None,
        "distinct_days": len({d[:10] for d in dates}),
    }
