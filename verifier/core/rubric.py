"""Rubric: the deterministic verdict for ``project`` and ``skill`` claims (design doc J5, J10).

Pure: reads only its arguments and the snapshot's raw bodies. No clock (``computed_at`` is passed
in; "now" for context lines is ``snapshot.evidence["created_at"]``), no network, no LLM, no
environment. Thresholds use integer arithmetic; percentages in facts are whole numbers
(floored), so the output never varies with float rounding.

Checks for an ``oss_contribution`` claim (J13), in order: ``contribution``, ``pr_count`` (only
when ``min_prs`` is set), ``maintainer`` (only for ``role: maintainer``).

Checks, in order, for a project: ``repo``, ``fork``, ``start_date``, ``stars``,
``commit_share``, ``language``, ``substance``, ``run`` (the ``run`` check is J10's mapping from a
``RunTrace``; it can only add a soft result and a flag, never a hard result or an upgrade).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from verifier.contracts.snapshot import Snapshot, cache_key
from verifier.contracts.types import (
    Check, ClaimsDoc, Verdict, VerdictClaim, RunTrace,
)
from verifier.core import store
from verifier.core.match import (
    any_prs_url, author_commits_url, commit_timeline, commits_url, contributors_url,
    languages_url, match_commits, match_repo, merged_prs_url, parse_repo_ref, repo_detail_url,
    repos_list_url, reviewed_prs_url, user_url,
)

RUBRIC_VERSION = "rubric-v1"

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# ── Language names ────────────────────────────────────────────────────────────

_ALIASES = {
    "js": "JavaScript", "ts": "TypeScript", "py": "Python", "cpp": "C++", "c++": "C++",
    "golang": "Go", "c#": "C#", "jupyter": "Jupyter Notebook",
}
_SPLIT_RE = re.compile(r"\s*(?:,|/|&)\s*|\s+and\s+", re.IGNORECASE)


def canon_language(name: str) -> str:
    """Display name for a claimed language after the alias map."""
    s = (name or "").strip()
    return _ALIASES.get(s.lower(), s)


def split_languages(text: str) -> list[str]:
    """Split "Python and JavaScript", "Python/JS", "A, B & C" into canonical parts (deduped)."""
    out: list[str] = []
    for part in _SPLIT_RE.split(text or ""):
        part = part.strip()
        if not part:
            continue
        canon = canon_language(part)
        if canon.lower() not in (o.lower() for o in out):
            out.append(canon)
    return out


# Well-known frameworks, libraries and tools that GitHub's linguist does not list as languages.
# Real linguist languages (SQL, HTML, CSS, Shell, Dockerfile, Jupyter Notebook, ...) stay out.
_FRAMEWORKS = frozenset(n.lower() for n in (
    "Flutter", "React", "React Native", "ReactJS", "React.js", "Next.js", "NextJS", "Node.js", "NodeJS",
    "Node", "Vue", "Vue.js", "VueJS", "Nuxt", "Nuxt.js", "Angular", "AngularJS", "Svelte", "SvelteKit",
    "Django", "Flask", "FastAPI", "Spring", "Spring Boot", "Rails", "Ruby on Rails", "Express",
    "Express.js", "ExpressJS", ".NET", "Unity", "Unreal", "Unreal Engine", "TensorFlow", "PyTorch",
    "Keras", "scikit-learn", "sklearn", "pandas", "NumPy", "OpenCV", "Tailwind", "Tailwind CSS",
    "Bootstrap", "jQuery", "Firebase", "AWS", "Docker", "Kubernetes", "Git", "Linux",
    "PostgreSQL", "Postgres", "MySQL", "MongoDB", "Redis", "SQLite",
))


def is_framework(name: str) -> bool:
    """Is `name` a known framework/library/tool rather than a GitHub language? (case-insensitive)"""
    return (name or "").strip().lower() in _FRAMEWORKS


def split_language_parts(text: str) -> list[str]:
    """Like `split_languages`, with a parenthesized part ("JavaScript (React)") as another part."""
    return split_languages(re.sub(r"[()]", "/", text or ""))


def language_counts_for(claimed: str, repo_lang: str) -> bool:
    """Does a repo's language `repo_lang` count toward a claim naming `claimed`?

    Exact (case-insensitive) after aliases; C and C++ count for each other; Jupyter Notebook
    counts as Python; TypeScript counts for a JavaScript claim, not the reverse.
    """
    c, r = canon_language(claimed).lower(), canon_language(repo_lang).lower()
    if c == r:
        return True
    if {c, r} == {"c", "c++"}:
        return True
    if c == "python" and r == "jupyter notebook":
        return True
    if c == "javascript" and r == "typescript":
        return True
    return False


def _lang_stats(langs: dict[str, int], claimed: str) -> tuple[int, int, bool]:
    """(counted bytes, total bytes, is the repo's largest language one that counts)."""
    total = sum(v for v in langs.values() if isinstance(v, int) and v > 0)
    counted = sum(v for k, v in langs.items()
                  if isinstance(v, int) and v > 0 and language_counts_for(claimed, k))
    ranked = sorted(((k, v) for k, v in langs.items() if isinstance(v, int) and v > 0),
                    key=lambda kv: (-kv[1], kv[0]))
    is_main = bool(ranked) and language_counts_for(claimed, ranked[0][0])
    return counted, total, is_main


# ── Small helpers ─────────────────────────────────────────────────────────────

def _pct(num: int, den: int) -> int:
    """Whole-number percent, floored; never 0 for a non-zero numerator."""
    if den <= 0 or num <= 0:
        return 0
    return max(1, (num * 100) // den)


def _band(num: int, den: int, pass_pct: int, soft_pct: int) -> str:
    """Lower bounds inclusive: >= pass_pct pass; >= soft_pct soft; else hard."""
    if num * 100 >= pass_pct * den:
        return "pass"
    if num * 100 >= soft_pct * den:
        return "soft"
    return "hard"


def _plural(n: int | str, word: str) -> str:
    return f"{n} {word}" if str(n) == "1" else f"{n} {word}s"


def _mon_year(iso: str) -> str:
    try:
        return f"{_MONTHS[int(iso[5:7]) - 1]} {int(iso[:4])}"
    except (ValueError, IndexError, TypeError):
        return str(iso)


def _parse_ts(s: Any) -> datetime | None:
    if not isinstance(s, str):
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _check(name: str, result: str, reason: str, facts: dict[str, Any] | None = None) -> Check:
    c: Check = {"check": name, "result": result, "reason": reason}  # type: ignore[typeddict-item]
    if facts is not None:
        c["facts"] = facts
    return c


def _cut(s: str, n: int = 120) -> str:
    return (s or "").strip()[:n]


_WORST = {"hard": 3, "soft": 2, "pass": 1, "skip": 0}


def _worst(results: list[str]) -> str:
    return max(results, key=lambda r: _WORST[r]) if results else "skip"


def claim_key(claim: dict[str, Any]) -> str:
    """`project:<repo_name>`, `skill:<name>`, `oss:<repo>`, `unchecked:<category>` (§9)."""
    a = claim.get("assertions", {})
    t = claim.get("type")
    if t == "project":
        name = a.get("repo_name")
        if not name and a.get("repo_url"):
            name = str(a["repo_url"]).rstrip("/").rsplit("/", 1)[-1]
        return f"project:{name or claim.get('claim_id')}"
    if t == "skill":
        return f"skill:{a.get('name')}"
    if t == "oss_contribution":
        return f"oss:{a.get('repo')}"
    return f"unchecked:{a.get('category')}"


# ── Evidence access ───────────────────────────────────────────────────────────

class _Ev:
    """Snapshot reads that never raise: a missing, unreadable or malformed body is `None`."""

    def __init__(self, snapshot: Snapshot):
        self.snapshot = snapshot

    def get(self, url: str) -> tuple[int, Any] | None:
        try:
            return self.snapshot.get("GET", url)
        except (OSError, ValueError):
            return None

    def ok(self, url: str, kind: type) -> Any | None:
        """Body of a 200 response of the given JSON type, else None."""
        r = self.get(url)
        if r is not None and r[0] == 200 and isinstance(r[1], kind):
            return r[1]
        return None


class _Ctx:
    def __init__(self, claims: ClaimsDoc, snapshot: Snapshot):
        self.claims = claims
        self.snapshot = snapshot
        self.ev = _Ev(snapshot)
        cand = claims["candidate"]
        self.candidate = cand
        self.login: str | None = cand.get("github_login") or None
        self.user_resp = self.ev.get(user_url(self.login)) if self.login else None
        self.user = self.user_resp[1] if (self.user_resp and self.user_resp[0] == 200
                                         and isinstance(self.user_resp[1], dict)) else None
        self.repos_resp = self.ev.get(repos_list_url(self.login)) if self.login else None
        self.owned = self._owned()

    def _owned(self) -> list[dict[str, Any]] | None:
        """Owned non-fork repos, top 30 by pushed_at; None when the list is unavailable."""
        r = self.repos_resp
        if r is None:
            return None
        if r[0] == 404:
            return []
        if r[0] != 200 or not isinstance(r[1], list):
            return None
        repos = [x for x in r[1] if isinstance(x, dict) and not x.get("fork")
                 and isinstance(x.get("full_name"), str)]
        # newest push first; ties broken by name so input order never matters
        repos.sort(key=lambda x: (-_ts_key(x.get("pushed_at")), str(x.get("full_name")).lower()))
        return repos[:30]

    def user_state(self) -> str:
        if not self.login:
            return "none"
        if self.user is not None:
            return "ok"
        if self.user_resp is not None and self.user_resp[0] == 404:
            return "notfound"
        return "unavailable"


def _ts_key(s: Any) -> int:
    d = _parse_ts(s)
    return int(d.timestamp()) if d else 0


# ── Project claims ────────────────────────────────────────────────────────────

class _Found:
    def __init__(self, full: str, method: str, detail: dict[str, Any]):
        self.full, self.method, self.detail = full, method, detail


def _resolve_repo(claim: dict[str, Any], cx: _Ctx) -> _Found | Check:
    """The matched repo, or the `repo` check that explains why there is none."""
    a = claim["assertions"]
    m = match_repo(claim, cx.snapshot)
    searched = m.get("searched") or ""
    full = m.get("repo")
    if full:
        r = cx.ev.get(repo_detail_url(full))
        if r is None or r[0] not in (200, 404, 410):
            return _check("repo", "skip", "Repo details unavailable offline")
        if r[0] == 200 and isinstance(r[1], dict):
            detail = r[1]
            return _Found(str(detail.get("full_name") or full), m.get("method") or "name", detail)
        # recorded as gone
    elif not a.get("repo_url"):
        if cx.owned is None:
            return _check("repo", "skip", "Repo details unavailable offline")
        decided = any(d.get("kind") == "repo_match" and d.get("claim_id") == claim["claim_id"]
                      for d in cx.snapshot.evidence.get("decisions", []))
        if not decided:
            return _check("repo", "skip", "Couldn't match the repo")
    else:
        mt = re.search(r"github\.com/([^/\s]+)/([^/\s#?]+)", str(a["repo_url"]), re.IGNORECASE)
        if mt:
            name = mt.group(2)
            name = name[:-4] if name.lower().endswith(".git") else name
            r = cx.ev.get(repo_detail_url(f"{mt.group(1)}/{name}"))
            if r is None or r[0] not in (200, 404, 410):
                return _check("repo", "skip", "Repo details unavailable offline")
    if not searched:
        n = len(cx.owned or [])
        searched = (f"No repo named {a.get('repo_name') or a.get('repo_url') or '(unnamed)'} "
                    f"among @{cx.login}'s {n} public repos")
    if a.get("public_signal") in ("link", "open_source_wording"):
        return _check("repo", "hard", f"Repo not found: {searched}")
    return _check("repo", "skip", f"No matching public repo: {searched}")


_METHOD_TEXT = {"url": "linked on the resume", "name": "matched by name", "llm": "matched by the model"}


def _project(claim: dict[str, Any], cx: _Ctx, trace: RunTrace | None) -> VerdictClaim:
    a = claim["assertions"]
    found = _resolve_repo(claim, cx)
    notes: list[str] = []
    flags: list[str] = []
    if not isinstance(found, _Found):
        checks = [found]
        return _finish(claim, checks, notes, flags, [])
    detail, full = found.detail, found.full
    ev = cx.ev
    checks: list[Check] = [_check(
        "repo", "pass", f"Repo {full} ({_METHOD_TEXT.get(found.method, 'matched by name')})",
        {"repo": full, "method": found.method})]

    role = a.get("role")
    # fork
    if detail.get("fork"):
        parent = (detail.get("parent") or {}).get("full_name") if isinstance(detail.get("parent"), dict) else None
        facts = {"parent": parent}
        label = f"Repo is a fork of {parent}" if parent else "Repo is a fork"
        if role == "creator":
            checks.append(_check("fork", "hard", label, facts))
        else:
            checks.append(_check("fork", "skip", label, facts))
            notes.append(f"Fork of {parent}" if parent else "Fork")
    else:
        checks.append(_check("fork", "pass", "Not a fork"))

    # start_date
    year = a.get("start_year")
    created = detail.get("created_at")
    if year is not None and isinstance(created, str) and created[:4].isdigit():
        cy = int(created[:4])
        res = "pass" if cy <= year else ("soft" if cy == year + 1 else "hard")
        checks.append(_check("start_date", res, f"Created {_mon_year(created)}; claim says {year}",
                             {"repo_created_at": created[:10], "claim_start_year": year}))

    # stars
    want = a.get("min_stars")
    if want is not None:
        have = detail.get("stargazers_count")
        have = have if isinstance(have, int) else 0
        checks.append(_check("stars", _band(have, want, 80, 50),
                             f"{have} stars; claim says {want}",
                             {"stars": have, "claim_min_stars": want}))

    # commit_share
    if role in ("creator", "co-creator", "maintainer"):
        checks.append(_commit_share(cx, full, role, notes))

    # language
    if a.get("language"):
        checks.append(_language_check(ev, full, a["language"]))

    # substance
    if a.get("scale") == "substantial":
        checks.append(_substance(ev, full, detail))

    # run (J10)
    if trace is not None:
        chk, flag = _run_check(trace)
        checks.append(chk)
        flags.extend(flag)

    link = detail.get("html_url") if isinstance(detail.get("html_url"), str) else f"https://github.com/{full}"
    return _finish(claim, checks, notes, flags, [link])


def _commit_share(cx: _Ctx, full: str, role: str, notes: list[str]) -> Check:
    r = cx.ev.get(contributors_url(full))
    if r is None:
        return _check("commit_share", "skip", "Contributors unavailable offline")
    status, body = r
    if status in (204, 409):
        body = []
    elif status != 200 or not isinstance(body, list):
        return _check("commit_share", "skip", "Contributors unavailable offline")
    m = match_commits(body, cx.candidate)
    total, matched = int(m["total"]), int(m["matched"])
    by = {k: int(v) for k, v in m["by_method"].items()}
    pass_pct, soft_pct = (50, 20) if role == "creator" else (20, 5)
    res = "hard" if (total == 0 or matched == 0) else _band(matched, total, pass_pct, soft_pct)
    if by.get("email", 0) + by.get("name", 0) > 0:
        notes.append("Commits not linked to the GitHub account")
    return _check("commit_share", res, f"{matched} of {total} commits by the candidate",
                  {"total": total, "matched": matched, "by_method": by})


def _language_check(ev: _Ev, full: str, claimed: str) -> Check:
    parts = split_language_parts(claimed) or [claimed.strip()]
    real = [p for p in parts if not is_framework(p)]
    if not real:
        return _check("language", "skip", f"{', '.join(parts)} is a framework, not a language")
    parts = real
    langs = ev.ok(languages_url(full), dict)
    if langs is None:
        return _check("language", "skip", "Languages unavailable offline")
    total = sum(v for v in langs.values() if isinstance(v, int) and v > 0)
    facts = {"languages": {k: _pct(v, total) for k, v in langs.items()
                           if isinstance(v, int) and v > 0}}
    results: list[tuple[str, str]] = []
    for part in parts:
        counted, tot, is_main = _lang_stats(langs, part)
        if counted == 0:
            results.append(("hard", f"No {part} code in the repo"))
        elif is_main or counted * 100 >= 20 * tot:
            reason = (f"{part} is the main language" if is_main
                      else f"{part} is {_pct(counted, tot)}% of the code")
            results.append(("pass", reason))
        else:
            results.append(("soft", f"{part} is {_pct(counted, tot)}% of the code"))
    worst = _worst([r for r, _ in results])
    if worst == "pass":
        reason = "; ".join(t for _, t in results)
    else:
        reason = "; ".join(t for r, t in results if r == worst)
    return _check("language", worst, reason, facts)


def _substance(ev: _Ev, full: str, detail: dict[str, Any]) -> Check:
    r = ev.get(commits_url(full))
    if r is not None and r[0] == 409:          # empty repo
        commits: Any = []
    else:
        commits = r[1] if (r is not None and r[0] == 200 and isinstance(r[1], list)) else None
    if commits is None:
        return _check("substance", "skip", "Commit history unavailable offline")
    tl = commit_timeline(commits)
    count, days = int(tl["count"]), int(tl["distinct_days"])
    size = detail.get("size")
    size = size if isinstance(size, int) else 0
    count_txt = "100+" if count >= 100 else str(count)
    facts = {"size_kb": size, "commits": count, "distinct_days": days}
    if size < 10 or count <= 1:
        return _check("substance", "soft",
                      f"Described as a complete project, but the repo is {size} KB with "
                      f"{_plural(count_txt, 'commit')} on {_plural(days, 'day')}", facts)
    return _check("substance", "pass", f"Repo is {size} KB with {_plural(count_txt, 'commit')}", facts)


# ── The run check (J10) ───────────────────────────────────────────────────────

_ERR_WORDS = ("error", "fatal", "traceback", "exception", "failed")


def _first_error_line(output: str) -> str:
    lines = [ln.strip() for ln in (output or "").splitlines() if ln.strip()]
    for ln in lines:
        low = ln.lower()
        if any(w in low for w in _ERR_WORDS):
            return _cut(ln)
    return _cut(lines[-1]) if lines else ""


def _secs(d: Any) -> str:
    try:
        return f"{float(d):.1f}"
    except (TypeError, ValueError):
        return "0.0"


def _run_check(trace: RunTrace) -> tuple[Check, list[str]]:
    """Map a RunTrace to the `run` check and its flags. Soft at worst: never hard, never an upgrade."""
    base = trace.get("baseline") or {}
    agent = trace.get("agent") or None
    judge = trace.get("judge") or None
    step = base.get("step") or "skip"
    fk = base.get("failure_kind")
    outcome = judge.get("outcome") if judge else None
    rationale = _cut((judge or {}).get("rationale") or "")
    baseline_passed = (step != "skip" and base.get("exit_code") == 0 and not base.get("timed_out"))

    summary = ""
    if baseline_passed:
        summary = {"build": f"Built in {_secs(base.get('duration_s'))}s",
                   "tests": f"Tests passed in {_secs(base.get('duration_s'))}s",
                   "syntax": "Syntax check passed"}.get(step, "")

    # 1. baseline timed out
    if base.get("timed_out"):
        n = int(float(base.get("duration_s") or 0) + 0.5)
        return _check("run", "soft", f"Timed out after {n}s during {step}"), ["run_timeout"]
    # 2. baseline failed with a real error
    if step != "skip" and fk == "error":
        word = {"build": "Build failed", "tests": "Tests failed",
                "syntax": "Syntax check failed"}.get(step, "Build failed")
        line = _first_error_line(base.get("output_excerpt") or "")
        return _check("run", "soft", f"{word}: {line}" if line else word), ["run_failed"]
    # 3. judge: mismatch
    if outcome == "runs_but_mismatch":
        return (_check("run", "soft", f"Ran, but the output didn't match the claim: {rationale}"),
                ["run_failed"])
    # 4. judge: failed to run
    if outcome == "failed_to_run":
        flag = "run_timeout" if (agent and agent.get("stopped") == "timeout") else "run_failed"
        return _check("run", "soft", f"Failed to run: {rationale}"), [flag]
    # 5. judge: works as claimed
    if outcome == "works_as_claimed":
        text = f"{summary}; works as claimed" if summary else "Works as claimed"
        return _check("run", "pass", text), []
    # 6. baseline passed, no judge
    if baseline_passed and judge is None:
        return _check("run", "pass", summary or "Baseline passed"), []
    # 7. baseline passed, judge could not attempt
    if baseline_passed and outcome == "could_not_attempt":
        return _check("run", "pass", f"{summary}; couldn't test the behavior here"), []
    # 8. could not run here
    if fk in ("deps_missing", "needs_network") or outcome == "could_not_attempt":
        why = ("missing dependencies" if fk == "deps_missing"
               else "needs network" if fk == "needs_network" else rationale)
        return _check("run", "skip", f"Could not run here: {why}"), []
    # 9. anything else
    return _check("run", "skip", "Nothing to build or run"), []


# ── Combining ────────────────────────────────────────────────────────────────

_PASS_ORDER = ("contribution", "pr_count", "maintainer", "commit_share", "language", "start_date", "run", "stars", "substance", "fork", "repo")


def _top_reason(checks: list[Check]) -> str:
    for res in ("hard", "soft"):
        for c in checks:
            if c["result"] == res:
                return c["reason"]
    passes = {c["check"]: c for c in checks if c["result"] == "pass"}
    if "commit_share" in passes and "language" in passes:
        return passes["commit_share"]["reason"] + "; " + passes["language"]["reason"]
    for name in _PASS_ORDER:
        if name in passes:
            return passes[name]["reason"]
    for c in checks:
        return c["reason"]
    return ""


def _finish(claim: dict[str, Any], checks: list[Check], notes: list[str], flags: list[str],
            links: list[str]) -> VerdictClaim:
    repo = checks[0]
    if repo["check"] == "repo" and repo["result"] != "pass":
        label = "contradicted" if repo["result"] == "hard" else "no_evidence"
    elif any(c["result"] == "hard" for c in checks):
        label = "contradicted"
    elif any(c["result"] == "soft" for c in checks):
        label = "partial"
    else:
        label = "supported"
    seen: set[str] = set()
    uniq_notes = [n for n in notes if not (n in seen or seen.add(n))]
    return {  # type: ignore[return-value]
        "claim_id": claim["claim_id"], "type": claim["type"], "label": label,
        "flags": sorted(set(flags)), "top_reason": _top_reason(checks), "checks": checks,
        "notes": uniq_notes, "evidence_links": links,
    }


def _simple(claim: dict[str, Any], label: str, reason: str, checks: list[Check] | None = None,
            notes: list[str] | None = None, links: list[str] | None = None) -> VerdictClaim:
    return {  # type: ignore[return-value]
        "claim_id": claim["claim_id"], "type": claim["type"], "label": label, "flags": [],
        "top_reason": reason, "checks": checks or [], "notes": notes or [],
        "evidence_links": links or [],
    }


# ── Skills ────────────────────────────────────────────────────────────────────

def _skill(claim: dict[str, Any], cx: _Ctx) -> VerdictClaim:
    a = claim["assertions"]
    if a.get("kind") != "language":
        text = "Framework or tool; not checked in v1"
        return _simple(claim, "no_evidence", text, [_check("skill_presence", "skip", text)])
    login = cx.login
    parts = split_language_parts(a.get("name") or "") or [str(a.get("name") or "").strip()]
    real = [p for p in parts if not is_framework(p)]
    if not real:
        text = f"{', '.join(parts)} is a framework, not checked as a language"
        return _simple(claim, "no_evidence", text, [_check("skill_presence", "skip", text)])
    parts = real
    owned = cx.owned or []
    if cx.owned is None:
        text = "Evidence unavailable offline"
        return _simple(claim, "no_evidence", text, [_check("skill_presence", "skip", text)])

    notes: list[str] = []
    langs_by_repo: dict[str, dict[str, Any]] = {}
    unavailable = 0
    for repo in owned:
        full = str(repo["full_name"])
        d = cx.ev.ok(languages_url(full), dict)
        if d is None:
            unavailable += 1
        else:
            langs_by_repo[full] = d
    if owned and not langs_by_repo:
        text = "Evidence unavailable offline"
        return _simple(claim, "no_evidence", text, [_check("skill_presence", "skip", text)])
    if unavailable:
        notes.append(f"Language data unavailable offline for {_plural(unavailable, 'repo')}")

    link_of = {str(r["full_name"]): r.get("html_url") or f"https://github.com/{r['full_name']}"
               for r in owned}
    ranks: list[tuple[int, str]] = []          # (0 none, 1 partial, 2 supported), reason
    merged: dict[str, int] = {}
    for part in parts:
        entries: list[tuple[str, int, int, bool]] = []     # repo, counted, total, is_main
        for full, langs in langs_by_repo.items():
            counted, total, is_main = _lang_stats(langs, part)
            if counted > 0:
                entries.append((full, counted, total, is_main))
        for full, counted, total, _ in entries:
            merged[full] = max(merged.get(full, 0), _pct(counted, total))
        n = len(entries)
        if n == 0:
            ranks.append((0, f"No public {part} code found in @{login}'s {_plural(len(owned), 'repo')}"))
            continue
        strong = any(c * 100 >= 20 * t for _, c, t, _ in entries)
        if strong or n >= 2:
            mains = sum(1 for e in entries if e[3])
            best = max(_pct(c, t) for _, c, t, _ in entries)
            reason = (f"Main language in {_plural(mains, 'repo')}" if mains
                      else f"In {_plural(n, 'repo')}, up to {best}% of the code")
            ranks.append((2, reason))
        else:
            full, counted, total, _ = entries[0]
            short = full.split("/", 1)[-1]
            ranks.append((1, f"Only in {short}, {_pct(counted, total)}% of the code"))

    worst = min(r for r, _ in ranks)
    reason = "; ".join(t for r, t in ranks if r == worst)
    label, result = {2: ("supported", "pass"), 1: ("partial", "soft"), 0: ("no_evidence", "skip")}[worst]
    top = sorted(merged.items(), key=lambda kv: (-kv[1], kv[0].lower()))
    facts = {"repos": [{"repo": r, "percent": p} for r, p in top[:10]]}
    links = [link_of[r] for r, _ in top[:3] if r in link_of]
    return _simple(claim, label, reason, [_check("skill_presence", result, reason, facts)],
                   notes, links)


# ── Open-source contribution claims (J13) ────────────────────────────────────

_OSS_NOT_RUN = "Contribution check not run"
_OSS_OFFLINE = "Contribution evidence unavailable offline"
_OSS_MAX_LINKS = 5


def _oss_simple(claim: dict[str, Any], reason: str, facts: dict[str, Any] | None = None) -> VerdictClaim:
    return _simple(claim, "no_evidence", reason, [_check("contribution", "skip", reason, facts)])


def _pr_sort_key(item: dict[str, Any]) -> tuple[int, int]:
    """Newest first: merge time, else close time, else creation time; ties by PR number."""
    pr = item.get("pull_request") if isinstance(item.get("pull_request"), dict) else {}
    ts = _ts_key(pr.get("merged_at")) or _ts_key(item.get("closed_at")) or _ts_key(item.get("created_at"))
    num = item.get("number")
    return (-ts, -(num if isinstance(num, int) else 0))


def _search_total(body: Any) -> int | None:
    """`total_count` of a search response, or None when the body is not one."""
    if not isinstance(body, dict):
        return None
    total = body.get("total_count")
    if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
        return total
    return None


def _oss(claim: dict[str, Any], cx: _Ctx) -> VerdictClaim:
    a = claim["assertions"]
    login = str(cx.login)
    repo = parse_repo_ref(a.get("repo"))
    if not repo:
        return _oss_simple(claim, _OSS_NOT_RUN)
    maintainer = a.get("role") == "maintainer"
    merged_u, any_u = merged_prs_url(repo, login), any_prs_url(repo, login)
    commits_u = author_commits_url(repo, login)
    review_u = reviewed_prs_url(repo, login) if maintainer else None
    urls = [merged_u, any_u, commits_u] + ([review_u] if review_u else [])

    keys = {u: cache_key("GET", u) for u in urls}
    recorded = {r.get("key") for r in cx.snapshot.evidence.get("responses", []) if isinstance(r, dict)}
    misses = set(cx.snapshot.evidence.get("misses") or [])
    if not any(k in recorded or k in misses for k in keys.values()):
        return _oss_simple(claim, _OSS_NOT_RUN)
    if any(k in misses or k not in recorded for k in keys.values()):
        return _oss_simple(claim, _OSS_OFFLINE)

    got = {u: cx.ev.get(u) for u in urls}
    if any(r is None for r in got.values()):
        return _oss_simple(claim, _OSS_OFFLINE)

    def total_of(url: str) -> int | None:
        status, body = got[url]          # type: ignore[misc]
        return _search_total(body) if status == 200 else None

    merged_total, opened_total = total_of(merged_u), total_of(any_u)
    reviews_total = total_of(review_u) if review_u else 0
    cstatus, cbody = got[commits_u]      # type: ignore[misc]
    if cstatus == 200 and isinstance(cbody, list):
        landed = len(cbody)
    elif cstatus in (404, 409, 410):     # repo gone or empty: nothing landed
        landed = 0
    else:
        landed = None
    if merged_total is None or opened_total is None or reviews_total is None or landed is None:
        return _oss_simple(claim, _OSS_OFFLINE)

    mstatus, mbody = got[merged_u]       # type: ignore[misc]
    items = [i for i in (mbody.get("items") if isinstance(mbody, dict) else None) or []
             if isinstance(i, dict)]
    items.sort(key=_pr_sort_key)
    items = items[:_OSS_MAX_LINKS]
    titles = [_cut(str(i.get("title") or "")) for i in items]
    facts: dict[str, Any] = {"merged_total": merged_total, "opened_total": opened_total,
                             "reviews_total": reviews_total, "landed_commits": landed,
                             "pr_titles": titles}

    if merged_total == 0 and opened_total == 0 and landed == 0:
        return _oss_simple(claim, f"Searched {repo} for PRs and commits by @{login}; none found.", facts)

    checks: list[Check] = []
    commit_txt = "100+" if landed >= 100 else str(landed)
    if merged_total >= 1 or landed >= 1:
        checks.append(_check(
            "contribution", "pass",
            f"{_plural(merged_total, 'merged PR')} and {_plural(commit_txt, 'commit')} "
            f"by @{login} in {repo}", facts))
    else:
        checks.append(_check("contribution", "soft",
                             f"{_plural(opened_total, 'PR')} opened in {repo}, none merged", facts))

    want = a.get("min_prs")
    if isinstance(want, int) and not isinstance(want, bool) and want >= 1:
        checks.append(_check("pr_count", _band(merged_total, want, 80, 50),
                             f"{_plural(merged_total, 'merged PR')}; claim says {want}",
                             {"merged_total": merged_total, "claim_min_prs": want}))

    if maintainer:
        enough = reviews_total >= 5
        checks.append(_check(
            "maintainer", "pass" if enough else "soft",
            f"{_plural(reviews_total, 'PR')} reviewed in {repo}" if enough
            else f"Contributions found, but only {_plural(reviews_total, 'PR review')}",
            {"reviews_total": reviews_total}))

    links: list[str] = []
    for i in items:
        url = i.get("html_url")
        if isinstance(url, str) and url and url not in links:
            links.append(url)
    if landed >= 1:
        links.append(f"https://github.com/{repo}/commits?author={login}")
    return _finish(claim, checks, [], [], links)


# ── Context and account ───────────────────────────────────────────────────────

def _context(cx: _Ctx) -> list[str]:
    lines: list[str] = []
    state = cx.user_state()
    if cx.user is not None and isinstance(cx.user.get("created_at"), str):
        lines.append(f"Account created {_mon_year(cx.user['created_at'])}.")
    now = _parse_ts(cx.snapshot.evidence.get("created_at"))
    if cx.owned and now is not None:
        cutoff = now - timedelta(days=30)
        recent = 0
        for r in cx.owned:
            p = _parse_ts(r.get("pushed_at"))
            if p is not None and p >= cutoff:
                recent += 1
        if recent * 2 > len(cx.owned):
            lines.append("Most public activity is from the last 30 days.")
    conflict = cx.candidate.get("github_login_conflict")
    if conflict and cx.login:
        lines.append(f"Resume links @{cx.login}; the application lists @{conflict['login']}. "
                     f"Using @{cx.login}.")
    if state == "none":
        lines.append("No GitHub account found on the resume or application.")
    elif state == "notfound":
        lines.append(f"GitHub account @{cx.login} was not found.")
    misses = cx.snapshot.evidence.get("misses") or []
    if misses:
        lines.append(f"Some GitHub evidence was unavailable offline ({len(misses)} requests).")
    return lines


def _account(cx: _Ctx) -> dict[str, Any] | None:
    if not cx.login:
        return None
    u = cx.user or {}
    created = u.get("created_at")

    def num(k: str) -> int | None:
        v = u.get(k)
        return v if isinstance(v, int) and not isinstance(v, bool) else None

    return {
        "login": cx.login,
        "login_source": cx.candidate.get("github_login_source", "none"),
        "login_conflict": cx.candidate.get("github_login_conflict"),
        "created_at": created[:10] if isinstance(created, str) else None,
        "public_repos": num("public_repos"),
        "followers": num("followers"),
    }


# ── Entry point ───────────────────────────────────────────────────────────────

def compute_verdict(claims: ClaimsDoc, snapshot: Snapshot, run_traces: list[RunTrace], *,
                    verdict_n: int, computed_at: str) -> Verdict:
    cx = _Ctx(claims, snapshot)
    traces = {t["claim_id"]: t for t in run_traces}
    out: list[VerdictClaim] = []
    for claim in claims["claims"]:
        t = claim["type"]
        if t == "unchecked":
            reason = ("Couldn't classify this claim; check the resume." if claim.get("needs_review")
                      else "Not verifiable from public code")
            out.append(_simple(claim, "no_evidence", reason))
        elif not cx.login:
            out.append(_simple(claim, "no_evidence", "No GitHub account found"))
        elif t == "project":
            out.append(_project(claim, cx, traces.get(claim["claim_id"])))
        elif t == "skill":
            out.append(_skill(claim, cx))
        else:  # oss_contribution (J13)
            out.append(_oss(claim, cx))

    tally = {"supported": 0, "partial": 0, "contradicted": 0, "no_evidence": 0}
    for vc in out:
        tally[vc["label"]] += 1
    return {  # type: ignore[return-value]
        "schema_version": "1",
        "verdict_id": f"verdict_{verdict_n}",
        "application_id": claims["application_id"],
        "computed_at": computed_at,
        "rubric_version": RUBRIC_VERSION,
        "inputs": {
            "claims_version": claims["claims_version"],
            "claims_sha256": store.sha256_json(claims),
            "snapshot_id": snapshot.snapshot_id,
            "evidence_sha256": store.sha256_json(snapshot.evidence),
            "run_traces": [{"claim_id": t["claim_id"], "sha256": store.sha256_json(t)}
                           for t in sorted(run_traces, key=lambda x: x["claim_id"])],
        },
        "account": _account(cx),
        "tally": tally,
        "context": _context(cx),
        "claims": out,
    }
