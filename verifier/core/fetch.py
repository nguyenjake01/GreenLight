"""GitHub fetcher, cache and recorder (design doc J2, §8.3 `fetch`, §7.4, §14).

`fetch_evidence(claims, run_dir=..., mode=...)` crawls GitHub (or the recorded cache) into a new
snapshot under `run_dir/snapshots/`. `python -m verifier.core.fetch record ...` fills the cache
(laptops only). Matching and URL builders live in `match.py` and are re-exported here, so the
recorder and `fetch_evidence` build identical URLs and therefore identical cache keys.

Modes: `record` calls GitHub and writes/replaces the cache file; `replay` reads the cache only;
`live` calls GitHub and leaves the cache alone. Responses (2xx, 404, 409, 410) are evidence;
everything else (401, 403, 429, 5xx, network failures, rate-limit stops, replay misses) goes
into `misses`. Network problems never raise; only local failures raise `FetchError`.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from verifier.contracts import registry
from verifier.contracts.config import env
from verifier.contracts.errors import FetchError
from verifier.contracts.snapshot import Snapshot, cache_key
from verifier.core import store
from verifier.core.match import (  # noqa: F401  (re-exported for the rubric and the recorder)
    API_BASE, any_prs_url, author_commits_url, commit_timeline, commits_url, contributors_url,
    github_url, languages_url, match_commits, match_repo, merged_prs_url, parse_repo_ref,
    repo_detail_url, repos_list_url, reviewed_prs_url, user_url,
)

PROMPT_VERSION = "match-v1"
TOP_REPOS = 30
SEARCH_GAP_S = 2.1
TIMEOUT_S = 15.0
MAX_ARCHIVE_BYTES = 50 * 1024 * 1024
RECORDABLE_OTHER = (404, 409, 410)
ROOT = Path(__file__).resolve().parents[2]

# Test hooks: tests replace these (a MockTransport, a fake clock) instead of patching httpx.
_TRANSPORT: httpx.BaseTransport | None = None
_CLOCK = time.monotonic
_SLEEP = time.sleep


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


# ── HTTP client with cache ────────────────────────────────────────────────────

@dataclass(frozen=True)
class Fetched:
    key: str
    status: int
    etag: str | None
    fetched_at: str
    body: str                      # raw response text

    @property
    def body_bytes(self) -> bytes:
        return self.body.encode("utf-8")

    @property
    def body_sha256(self) -> str:
        return store.sha256_bytes(self.body_bytes)


def _is_response(status: int) -> bool:
    """Statuses recorded as evidence; anything else is a miss."""
    return 200 <= status < 300 or status in RECORDABLE_OTHER


def _valid_json(text: str) -> bool:
    try:
        json.loads(text)
    except ValueError:
        return False
    return True


class GitHubClient:
    """GET-only GitHub client with the record/replay/live cache. Sequential; one per crawl.

    `fetch(url)` returns a `Fetched`, or None for a miss. Each key is fetched at most once per
    client (memoized), so a recorder run that shares one client never repeats a request.
    """

    def __init__(self, mode: str, cache_dir: Path, token: str | None = None, *,
                 transport: httpx.BaseTransport | None = None, clock=None, sleep=None):
        if mode not in ("record", "replay", "live"):
            raise FetchError(f"unknown fetch mode {mode!r}")
        self.mode = mode
        self.cache_dir = Path(cache_dir)
        self._clock = clock or _CLOCK
        self._sleep = sleep or _SLEEP
        self._last_search: float | None = None
        self._memo: dict[str, Fetched | None] = {}
        self.rate_remaining: dict[str, int] = {}     # resource -> last X-RateLimit-Remaining
        self._stopped: set[str] = set()              # resources whose limit is used up
        self.log: list[tuple[str, str]] = []         # (key, "200" | "MISS: why")
        self._http: httpx.Client | None = None
        if mode != "replay":
            headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            self._http = httpx.Client(headers=headers, timeout=TIMEOUT_S, follow_redirects=True,
                                      transport=transport or _TRANSPORT)

    def close(self) -> None:
        if self._http is not None:
            self._http.close()

    # -- cache files ---------------------------------------------------------

    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / f"{hashlib.sha256(key.encode('utf-8')).hexdigest()}.json"

    def _read_cache(self, key: str) -> Fetched | None:
        path = self._cache_path(key)
        if not path.is_file():
            return None
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
            fetched = Fetched(key=entry["key"], status=int(entry["status"]), etag=entry.get("etag"),
                              fetched_at=entry["fetched_at"], body=entry["body"])
            ok = (fetched.key == key and isinstance(fetched.body, str)
                  and fetched.body_sha256 == entry["body_sha256"] and _valid_json(fetched.body))
        except (OSError, ValueError, KeyError, TypeError) as e:
            raise FetchError(f"corrupt cache file {path}: {e}") from e
        if not ok:
            raise FetchError(f"corrupt cache file {path}: key or body hash mismatch")
        return fetched

    def _write_cache(self, f: Fetched) -> None:
        entry = {"key": f.key, "status": f.status, "etag": f.etag, "fetched_at": f.fetched_at,
                 "body_sha256": f.body_sha256, "body": f.body}
        path = self._cache_path(f.key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(store.dump_json(entry))
                os.chmod(tmp, 0o644)
                os.replace(tmp, path)
            finally:
                Path(tmp).unlink(missing_ok=True)
        except OSError as e:
            raise FetchError(f"cannot write cache file {path}: {e}") from e

    # -- requests ------------------------------------------------------------

    def fetch(self, url: str) -> Fetched | None:
        key = cache_key("GET", url)
        if key in self._memo:
            return self._memo[key]
        result = self._fetch_uncached(key, url)
        self._memo[key] = result
        return result

    def _miss(self, key: str, why: str) -> None:
        self.log.append((key, f"MISS: {why}"))
        return None

    def _fetch_uncached(self, key: str, url: str) -> Fetched | None:
        if self.mode == "replay":
            cached = self._read_cache(key)
            if cached is None:
                return self._miss(key, "not in cache")
            self.log.append((key, str(cached.status)))
            return cached

        resource = "search" if "/search/" in url else "core"
        if resource in self._stopped:
            return self._miss(key, f"rate limit used up ({resource})")
        resp, why = self._http_get(url, resource)
        if resp is None:
            return self._miss(key, why)
        if not _is_response(resp.status_code):
            return self._miss(key, f"HTTP {resp.status_code}")
        text = resp.text
        if not _valid_json(text):
            return self._miss(key, f"HTTP {resp.status_code} with a non-JSON body")
        fetched = Fetched(key=key, status=resp.status_code, etag=resp.headers.get("etag"),
                          fetched_at=store.now_iso(), body=text)
        if self.mode == "record":
            self._write_cache(fetched)
        self.log.append((key, str(fetched.status)))
        return fetched

    def _wait_for_search_slot(self) -> None:
        if self._last_search is not None:
            wait = self._last_search + SEARCH_GAP_S - self._clock()
            if wait > 0:
                self._sleep(wait)
        self._last_search = self._clock()

    def _http_get(self, url: str, resource: str) -> tuple[httpx.Response | None, str]:
        """One call, retried once on a 5xx or connection error. Returns (response, why-not)."""
        assert self._http is not None
        why = ""
        for attempt in (1, 2):
            if resource == "search":
                self._wait_for_search_slot()
            try:
                resp = self._http.get(url)
            except (httpx.HTTPError, httpx.InvalidURL) as e:
                why = f"network error: {type(e).__name__}"
                if attempt == 1:
                    self._sleep(0.5)
                    continue
                return None, why
            self._note_rate_limit(resp, resource)
            if resp.status_code >= 500 and attempt == 1:
                self._sleep(0.5)
                continue
            return resp, ""
        return None, why

    def _note_rate_limit(self, resp: httpx.Response, resource: str) -> None:
        header_resource = resp.headers.get("x-ratelimit-resource")
        if header_resource in ("core", "search"):
            resource = header_resource
        remaining = resp.headers.get("x-ratelimit-remaining")
        if remaining is not None and remaining.strip().lstrip("-").isdigit():
            self.rate_remaining[resource] = int(remaining)
            if int(remaining) <= 0:
                self._stopped.add(resource)
        if resp.status_code == 429:
            self._stopped.add(resource)


# ── In-memory evidence for one crawl (the stand-in `Snapshot` for `match_repo`) ───

class _Crawl:
    """Records what one crawl got. `get`/`evidence` make it usable wherever a Snapshot is."""

    def __init__(self, client: GitHubClient, login: str | None):
        self.client = client
        self.login = login
        self.responses: dict[str, Fetched] = {}
        self.misses: set[str] = set()
        self.decisions: list[dict[str, Any]] = []
        self._parsed: dict[str, Any] = {}

    @property
    def evidence(self) -> dict[str, Any]:
        return {"github_login": self.login, "decisions": self.decisions}

    def request(self, url: str) -> tuple[int, Any] | None:
        key = cache_key("GET", url)
        if key not in self.responses and key not in self.misses:
            fetched = self.client.fetch(url)
            if fetched is None:
                self.misses.add(key)
            else:
                self.responses[key] = fetched
                self._parsed[key] = json.loads(fetched.body)
        return self.get("GET", url)

    def get(self, method: str, url: str) -> tuple[int, Any] | None:
        key = cache_key(method, url)
        if key in self.misses or key not in self.responses:
            return None
        return self.responses[key].status, self._parsed[key]


# ── Crawl steps ───────────────────────────────────────────────────────────────

def _top_repos(repos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The 30 most recently pushed owned non-fork repos."""
    owned = [r for r in repos if not r.get("fork")]
    owned.sort(key=lambda r: r.get("pushed_at") or "", reverse=True)
    return owned[:TOP_REPOS]


def _fetch_account(crawl: _Crawl) -> list[dict[str, Any]]:
    login = crawl.login or ""
    crawl.request(user_url(login))
    got = crawl.request(repos_list_url(login))
    if got is None or got[0] != 200 or not isinstance(got[1], list):
        return []
    return [r for r in got[1] if isinstance(r, dict) and r.get("name")]


def _fetch_repo(crawl: _Crawl, repo: str, *, detail: bool = True) -> None:
    if detail:
        crawl.request(repo_detail_url(repo))
    crawl.request(languages_url(repo))
    crawl.request(contributors_url(repo))
    crawl.request(commits_url(repo))


def _llm_match(crawl: _Crawl, claim: dict[str, Any], repos: list[dict[str, Any]]) -> None:
    """Ask the llm module which repo the claim means; record the decision. Failure records nothing."""
    names = [r["name"] for r in repos]
    if not names:
        return
    schema = json.loads((store.SCHEMAS_DIR / "repo_match.schema.json").read_text(encoding="utf-8"))
    schema["properties"]["repo"]["enum"] = names + ["none"]
    lines = []
    for r in repos:
        desc = " ".join(str(r.get("description") or "").split())[:200]
        lines.append(f"- {r['name']} | {desc or '(no description)'} | {r.get('language') or 'unknown'}")
    source_text = claim.get("source_text", "")
    messages = [
        {"role": "system", "content": (
            "You match a project claim from a resume to one of the candidate's GitHub repos. "
            'Answer with the repo name exactly as listed, or "none" if no repo is a clear match. '
            "Do not guess.")},
        {"role": "user", "content": (
            f"Resume claim:\n{source_text}\n\nRepos owned by @{crawl.login}:\n" + "\n".join(lines))},
    ]
    try:
        resp = registry.get("llm").llm_chat(messages, schema, temperature=0.0, max_tokens=256)
        data = resp.get("json")
        answer = data.get("repo") if isinstance(data, dict) else None
        model = str(resp.get("model") or "unknown")
    except Exception:  # noqa: BLE001  any LLM failure: record nothing, J5 reports "couldn't match"
        return
    output = answer if isinstance(answer, str) and answer in names else "none"
    crawl.decisions.append({
        "kind": "repo_match", "claim_id": claim["claim_id"], "model": model,
        "prompt_version": PROMPT_VERSION,
        "inputs": {"claimed": source_text, "candidates": names}, "output": output})


def _cut() -> set[str]:
    return {p.strip() for p in env("CUT").split(",") if p.strip()}


def _crawl_claims(crawl: _Crawl, claims: dict[str, Any], *, use_llm: bool,
                  deep: bool = False) -> list[str]:
    """Everything `fetch_evidence` requests for these claims. Returns the matched repos
    ("owner/name") in claim order. `deep` also fetches detail/contributors/commits for the top 30."""
    login = crawl.login or ""
    repos = _fetch_account(crawl)
    matched: list[str] = []
    for claim in claims.get("claims", []):
        if claim.get("type") != "project":
            continue
        a = claim.get("assertions") or {}
        ref = parse_repo_ref(a.get("repo_url"))
        if ref:
            crawl.request(repo_detail_url(ref))  # its answer decides found / not found
        m = match_repo(claim, crawl)
        if m["repo"] is None and use_llm and not a.get("repo_url"):
            _llm_match(crawl, claim, repos)
            m = match_repo(claim, crawl)
        if m["repo"] and m["repo"] not in matched:
            matched.append(m["repo"])

    top = _top_repos(repos)
    for r in top:
        crawl.request(languages_url(f"{login}/{r['name']}"))
    if deep:
        for r in top:
            _fetch_repo(crawl, f"{login}/{r['name']}")
    for repo in matched:
        _fetch_repo(crawl, repo)

    if "oss" not in _cut():
        for claim in claims.get("claims", []):
            if claim.get("type") != "oss_contribution":
                continue
            a = claim.get("assertions") or {}
            repo = parse_repo_ref(a.get("repo"))
            if not repo:
                continue
            crawl.request(merged_prs_url(repo, login))
            crawl.request(any_prs_url(repo, login))
            if a.get("role") == "maintainer":
                crawl.request(reviewed_prs_url(repo, login))
            crawl.request(author_commits_url(repo, login))
    return matched


# ── fetch_evidence ────────────────────────────────────────────────────────────

def _cache_dir() -> Path:
    return Path(env("GITHUB_CACHE_DIR"))


def _new_snapshot_dir(run_dir: Path) -> tuple[str, Path, str]:
    """Create `snapshots/snap_<UTC now>`; if that second is taken, wait a second and retry."""
    parent = Path(run_dir) / "snapshots"
    parent.mkdir(parents=True, exist_ok=True)
    while True:
        when = _utcnow()
        snapshot_id = "snap_" + when.strftime("%Y%m%dT%H%M%SZ")
        path = parent / snapshot_id
        try:
            path.mkdir()
        except FileExistsError:
            _SLEEP(1.0)
            continue
        return snapshot_id, path, when.strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_evidence(claims: dict[str, Any], *, run_dir: Path, mode: str) -> Snapshot:
    try:
        snapshot_id, snap_dir, created_at = _new_snapshot_dir(Path(run_dir))
    except OSError as e:
        raise FetchError(f"cannot create snapshot folder under {run_dir}: {e}") from e
    client: GitHubClient | None = None
    try:
        login = ((claims.get("candidate") or {}).get("github_login")) or None
        crawl = _Crawl(None, login)  # type: ignore[arg-type]
        if login:
            client = GitHubClient(mode, _cache_dir(), env("GITHUB_TOKEN") or None)
            crawl.client = client
            _crawl_claims(crawl, claims, use_llm=True)
        responses = []
        for key in sorted(crawl.responses):
            f = crawl.responses[key]
            body_path = f"raw/{f.body_sha256}.json"
            store.write_bytes(snap_dir / body_path, f.body_bytes)
            responses.append({"key": key, "status": f.status, "fetched_at": f.fetched_at,
                              "etag": f.etag, "body_sha256": f.body_sha256, "body_path": body_path})
        evidence = {
            "schema_version": "1", "snapshot_id": snapshot_id,
            "application_id": str(claims["application_id"]), "github_login": login,
            "fetch_mode": mode, "created_at": created_at, "responses": responses,
            "misses": sorted(crawl.misses), "decisions": crawl.decisions,
        }
        store.write_json(snap_dir / "evidence.json", evidence)
        return Snapshot.load(snap_dir)
    except Exception as e:  # noqa: BLE001  contract: only FetchError escapes
        shutil.rmtree(snap_dir, ignore_errors=True)
        if isinstance(e, FetchError):
            raise
        raise FetchError(f"fetch failed: {type(e).__name__}: {e}") from e
    finally:
        if client is not None:
            client.close()


# ── Recorder CLI ──────────────────────────────────────────────────────────────

def _git(args: list[str], cwd: Path | None = None) -> str:
    out = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                         timeout=300, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    return out.stdout.strip()


def _clone_repo(repo: str, detail: dict[str, Any] | None, repos_dir: Path, force: bool) -> str:
    """Shallow-clone `repo` and save `git archive HEAD` plus a .json. Returns a status line."""
    owner, _, name = repo.partition("/")
    stem = f"{owner.lower()}__{name.lower()}"
    archive, meta = repos_dir / f"{stem}.tar.gz", repos_dir / f"{stem}.json"
    if archive.exists() and not force:
        return f"skip {repo}: {archive.name} already exists (use --force to replace)"
    size_kb = (detail or {}).get("size")
    if isinstance(size_kb, int) and size_kb * 1024 > MAX_ARCHIVE_BYTES:
        return f"skip {repo}: repo is {size_kb} KB, over the 50 MB limit"
    with tempfile.TemporaryDirectory(prefix="rv-clone-") as td:
        work, out = Path(td) / "repo", Path(td) / "out.tar.gz"
        try:
            _git(["clone", "--depth", "1", "--quiet", f"https://github.com/{repo}.git", str(work)])
            sha = _git(["rev-parse", "HEAD"], cwd=work)
            _git(["archive", "--format=tar.gz", "-o", str(out), "HEAD"], cwd=work)
        except (subprocess.SubprocessError, OSError) as e:
            return f"FAILED {repo}: {type(e).__name__}: {str(e)[-200:]}"
        if out.stat().st_size > MAX_ARCHIVE_BYTES:
            return f"skip {repo}: archive is over the 50 MB limit"
        repos_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(out), archive)
    full = (detail or {}).get("full_name") or repo
    meta.write_bytes(store.dump_json({"repo": full, "commit_sha": sha, "cloned_at": store.now_iso()}))
    return f"cloned {repo} @ {sha[:10]} -> {archive.name}"


def _record(args: argparse.Namespace) -> int:
    cache_dir = Path(args.cache_dir or _cache_dir())
    token = env("GITHUB_TOKEN") or None
    client = GitHubClient("record", cache_dir, token)
    try:
        docs = []
        for p in args.claims:
            try:
                docs.append(json.loads(Path(p).read_text(encoding="utf-8")))
            except (OSError, ValueError) as e:
                raise FetchError(f"cannot read claims file {p}: {e}") from e
        logins = [l.lower() for l in args.login]
        for d in docs:
            lg = ((d.get("candidate") or {}).get("github_login") or "").lower()
            if lg:
                logins.append(lg)
        logins = list(dict.fromkeys(logins))

        crawls: dict[str, _Crawl] = {}
        clone_targets: list[str] = []
        for login in logins:  # per-login requests, plus detail/contributors/commits for the top 30
            crawls[login] = _Crawl(client, login)
            _crawl_claims(crawls[login], {"claims": []}, use_llm=False, deep=True)
        for d in docs:  # claim-driven requests
            lg = ((d.get("candidate") or {}).get("github_login") or "").lower()
            if not lg:
                continue
            crawl = crawls.setdefault(lg, _Crawl(client, lg))
            clone_targets += _crawl_claims(crawl, d, use_llm=False)
        extra = crawls[logins[0]] if logins else _Crawl(client, None)
        for repo in args.repo:
            ref = parse_repo_ref(repo)
            if not ref:
                raise FetchError(f"--repo {repo!r} is not owner/name")
            _fetch_repo(extra, ref)
            clone_targets.append(ref)

        _print_summary(client, token)
        if args.clone:
            repos_dir = Path(args.repos_dir)
            print("\nClones:")
            seen: set[str] = set()
            for repo in clone_targets:
                if repo.lower() in seen:
                    continue
                seen.add(repo.lower())
                got = None
                for c in crawls.values():
                    got = got or c.get("GET", repo_detail_url(repo))
                detail = got[1] if got and got[0] == 200 and isinstance(got[1], dict) else None
                print("  " + _clone_repo(detail.get("full_name", repo) if detail else repo, detail,
                                          repos_dir, args.force))
        return 0
    finally:
        client.close()


def _print_summary(client: GitHubClient, token: str | None) -> None:
    print(f"GitHub requests ({'with' if token else 'WITHOUT'} GITHUB_TOKEN):")
    misses = []
    for key, outcome in client.log:
        print(f"  {outcome:<28} {key.removeprefix('GET ')}")
        if outcome.startswith("MISS"):
            misses.append(key)
    print(f"\n{len(client.log)} requests, {len(client.log) - len(misses)} recorded, {len(misses)} misses")
    for key in misses:
        print(f"  miss: {key}")
    for resource, remaining in sorted(client.rate_remaining.items()):
        print(f"rate limit remaining ({resource}): {remaining}")


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv
    load_dotenv()
    ap = argparse.ArgumentParser(prog="python -m verifier.core.fetch",
                                 description="GitHub recorder (laptops only, never the GB10)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    rec = sub.add_parser("record", help="fill the GitHub cache (and optionally clone repos)")
    rec.add_argument("--login", action="append", default=[], help="GitHub login (repeatable)")
    rec.add_argument("--claims", action="append", default=[], help="claims JSON file (repeatable)")
    rec.add_argument("--repo", action="append", default=[],
                     help="extra owner/name for claims that need the LLM match (repeatable)")
    rec.add_argument("--clone", action="store_true", help="save git archives of the matched repos")
    rec.add_argument("--force", action="store_true", help="replace existing repo archives")
    rec.add_argument("--cache-dir", default=None, help="default: GITHUB_CACHE_DIR")
    rec.add_argument("--repos-dir", default=str(ROOT / "fixtures" / "repos"))
    args = ap.parse_args(argv)
    try:
        return _record(args)
    except FetchError as e:
        print(f"fetch record FAILED: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
