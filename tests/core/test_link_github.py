"""J4 `link_github`: table-driven tests (pure, no network)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from verifier.core.resume import link_github

ROOT = Path(__file__).resolve().parents[2]


def _app(**kw) -> dict:
    base = {
        "candidate_name": "Test Person",
        "emails": [],
        "website_addresses": [],
        "social_media_addresses": [],
    }
    base.update(kw)
    return base


def _resume(text: str = "", uris: tuple[str, ...] = ()) -> dict:
    return {"text": text, "links": [{"uri": u, "anchor_text": "", "page": 0} for u in uris]}


# (id, resume, app overrides, expected login, expected source)
CASES = [
    ("profile_link", _resume(uris=("https://github.com/alice",)), {}, "alice", "resume_link"),
    ("repo_link_only", _resume(uris=("https://github.com/alice/cool-repo",)), {}, "alice", "resume_link"),
    ("github_io_only", _resume(uris=("https://alice.github.io/",)), {}, "alice", "resume_link"),
    ("text_only", _resume("Find me at github.com/alice today"), {}, "alice", "resume_link"),
    ("text_www_scheme", _resume("see https://www.github.com/alice"), {}, "alice", "resume_link"),
    ("profile_beats_repo", _resume(uris=("https://github.com/org/repo", "github.com/alice")), {}, "alice", "resume_link"),
    ("repo_beats_pages", _resume(uris=("https://bob.github.io", "https://github.com/alice/r")), {}, "alice", "resume_link"),
    ("frequency_wins", _resume(uris=("github.com/alice", "github.com/bob", "github.com/bob")), {}, "bob", "resume_link"),
    ("earliest_on_tie", _resume(uris=("github.com/bob", "github.com/alice")), {}, "bob", "resume_link"),
    ("links_beat_text", _resume("github.com/textuser", uris=("https://github.com/linkuser",)), {}, "linkuser", "resume_link"),
    ("query_dropped", _resume(uris=("https://github.com/alice?tab=repositories",)), {}, "alice", "resume_link"),
    ("fragment_dropped", _resume(uris=("https://github.com/alice#readme",)), {}, "alice", "resume_link"),
    ("trailing_paren", _resume("(github.com/alice)"), {}, "alice", "resume_link"),
    ("trailing_period", _resume("Code: github.com/alice."), {}, "alice", "resume_link"),
    ("trailing_slash", _resume("github.com/alice/"), {}, "alice", "resume_link"),
    ("case_kept", _resume(uris=("https://GitHub.com/AliceDev",)), {}, "AliceDev", "resume_link"),
    ("hyphen_ok", _resume("github.com/alice-dev"), {}, "alice-dev", "resume_link"),
    ("greenhouse_website", _resume("no links"), {"website_addresses": ["https://github.com/carol"]}, "carol", "greenhouse_website"),
    ("greenhouse_pages", _resume("no links"), {"website_addresses": ["https://carol.github.io"]}, "carol", "greenhouse_website"),
    ("greenhouse_social", _resume("no links"), {"social_media_addresses": ["github.com/dave"]}, "dave", "greenhouse_social"),
    ("nothing_found", _resume("just prose, no links"), {"website_addresses": ["https://example.com"]}, None, "none"),
    ("empty_resume", {}, {}, None, "none"),
]


@pytest.mark.parametrize("resume,overrides,login,source", [c[1:] for c in CASES], ids=[c[0] for c in CASES])
def test_login_and_source(resume, overrides, login, source):
    cand = link_github(resume, _app(**overrides))
    assert cand["github_login"] == login
    assert cand["github_login_source"] == source
    assert cand["github_login_conflict"] is None


@pytest.mark.parametrize("text", [
    "github.com/orgs/acme",
    "github.com/features/copilot",
    "https://github.com/topics/python",
    "github.com/sponsors/alice",
    "github.com/settings/profile",
    "github.com/marketplace",
    "github.com/explore",
    "github.com/about",
    "github.com/pricing",
    "github.com/login",
    "github.com/join",
    "github.com/apps/dependabot",
    "github.com/collections/x",
    "github.com/trending",
    "github.com/events",
    "github.com/notifications",
    "github.com/enterprise",
    "github.com/security",
    "github.com/site/terms",
    "github.com/contact",
    "github.com/search?q=foo",
    "github.com/pulls",
    "github.com/issues",
    "github.com/new",
    "github.com/codespaces",
    "https://gist.github.com/alice/abc123",
    "https://raw.githubusercontent.com/alice/repo/main/x.py",
    "https://notgithub.com/alice",
    "github.com/-bad",
    "github.com/bad-",
    "github.com/bad--name",
    "github.com/" + "a" * 40,
])
def test_ignored(text):
    for resume in (_resume(text), _resume(uris=(text,))):
        cand = link_github(resume, _app())
        assert cand["github_login"] is None, text
        assert cand["github_login_source"] == "none"


def test_reserved_skipped_but_real_login_found():
    cand = link_github(_resume("github.com/features and github.com/orgs/acme and github.com/alice"), _app())
    assert cand["github_login"] == "alice"


def test_max_length_login_allowed():
    login = "a" * 39
    assert link_github(_resume(f"github.com/{login}"), _app())["github_login"] == login


@pytest.mark.parametrize("website,social,conflict", [
    (["https://github.com/other"], [], {"login": "other", "source": "greenhouse_website"}),
    ([], ["github.com/other"], {"login": "other", "source": "greenhouse_social"}),
    (["https://other.github.io"], [], {"login": "other", "source": "greenhouse_website"}),
    (["https://github.com/ALICE"], [], None),  # same login, different case: no conflict
    (["https://github.com/other"], ["github.com/third"], {"login": "other", "source": "greenhouse_website"}),
])
def test_conflict(website, social, conflict):
    cand = link_github(
        _resume(uris=("https://github.com/alice",)),
        _app(website_addresses=website, social_media_addresses=social),
    )
    assert cand["github_login"] == "alice"
    assert cand["github_login_source"] == "resume_link"
    assert cand["github_login_conflict"] == conflict


def test_conflict_between_greenhouse_sources():
    cand = link_github(_resume(), _app(website_addresses=["github.com/one"], social_media_addresses=["github.com/two"]))
    assert cand["github_login"] == "one"
    assert cand["github_login_source"] == "greenhouse_website"
    assert cand["github_login_conflict"] == {"login": "two", "source": "greenhouse_social"}


def test_email_merge():
    cand = link_github(
        _resume("Reach me: Alice@Example.com, alice@work.io. or a@b.co"),
        _app(candidate_name="Alice A", emails=["alice@example.com", "other@x.org"]),
    )
    assert cand["name"] == "Alice A"
    assert cand["emails"] == ["alice@example.com", "other@x.org", "alice@work.io", "a@b.co"]


def test_no_emails():
    assert link_github(_resume("nothing"), _app())["emails"] == []


def test_output_shape():
    cand = link_github(_resume(), _app())
    assert set(cand) == {"name", "emails", "github_login", "github_login_source", "github_login_conflict"}


def test_henry_fixture():
    resume = json.loads((ROOT / "fixtures" / "resumes" / "henry.resume.json").read_text())
    hook = json.loads((ROOT / "fixtures" / "webhooks" / "henry.json").read_text())["payload"]["application"]["candidate"]
    app = _app(
        candidate_name="Henry Chen",
        emails=[e["value"] for e in hook["email_addresses"]],
        website_addresses=[w["value"] for w in hook["website_addresses"]],
        social_media_addresses=[w["value"] for w in hook["social_media_addresses"]],
    )
    cand = link_github(resume, app)
    assert cand["github_login"] == "henrychen4736"
    assert cand["github_login_source"] == "resume_link"
    assert cand["github_login_conflict"] is None
    assert cand["emails"] == ["henry@example.com"]

    # Same webhook, a different GitHub profile in website_addresses: recorded as a conflict.
    app["website_addresses"] = ["https://github.com/someone-else"]
    cand = link_github(resume, app)
    assert cand["github_login"] == "henrychen4736"
    assert cand["github_login_conflict"] == {"login": "someone-else", "source": "greenhouse_website"}


@pytest.mark.parametrize("name", ["henry", "fake"])
def test_fixtures_match_candidates_json(name):
    resume = json.loads((ROOT / "fixtures" / "resumes" / f"{name}.resume.json").read_text())
    expected = json.loads((ROOT / "fixtures" / "candidates.json").read_text())[name]["github_login"]
    assert link_github(resume, _app())["github_login"] == expected
