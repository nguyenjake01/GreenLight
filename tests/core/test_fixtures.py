"""Every JSON fixture validates against its schema, and the fixtures agree with each other."""
import json
from pathlib import Path

import pytest

from verifier.core import resume, store

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
HENRY = "henrychen4736"
AIDAN = "aidanhong8"
LABELS = {"supported", "partial", "contradicted", "no_evidence"}


def load(rel):
    return json.loads((FIXTURES / rel).read_text(encoding="utf-8"))


def claim_key(c):
    a = c["assertions"]
    return {"project": lambda: f"project:{a.get('repo_name')}",
            "skill": lambda: f"skill:{a.get('name')}",
            "oss_contribution": lambda: f"oss:{a.get('repo')}",
            "unchecked": lambda: f"unchecked:{a.get('category')}"}[c["type"]]()


# ── schema validation ────────────────────────────────────────────────────────

@pytest.mark.parametrize("rel, schema", [
    (f"claims/{HENRY}.json", "claims"),
    (f"claims/{AIDAN}.json", "claims"),
    (f"verdicts/{HENRY}.json", "verdict"),
    ("run_traces/c3.json", "run_trace"),
    ("snapshots/sample/evidence.json", "evidence"),
    ("llm/extraction_output.json", "extraction_output"),
    ("llm/repo_match.json", "repo_match"),
    ("llm/agent_step.json", "agent_step"),
    ("llm/judge_outcome.json", "judge_outcome"),
])
def test_fixture_validates(rel, schema):
    store.validate(load(rel), schema)


def test_llm_schema_titles_match_fixture_names():
    for name in ("extraction_output", "repo_match", "agent_step", "judge_outcome"):
        schema = json.loads((store.SCHEMAS_DIR / f"{name}.schema.json").read_text())
        assert schema["title"] == name
        assert (FIXTURES / "llm" / f"{name}.json").is_file()


def _keywords(node, found):
    if isinstance(node, dict):
        for k, v in node.items():
            found.add(k)
            if k == "properties":
                for sub in v.values():
                    _keywords(sub, found)
            else:
                _keywords(v, found)
    elif isinstance(node, list):
        for v in node:
            _keywords(v, found)
    return found


def test_llm_schemas_use_only_guided_decoding_safe_keywords():
    safe = {"title", "type", "properties", "required", "enum", "items", "additionalProperties"}
    for name in ("extraction_output", "repo_match", "agent_step", "judge_outcome"):
        schema = json.loads((store.SCHEMAS_DIR / f"{name}.schema.json").read_text())
        assert _keywords(schema, set()) <= safe, name


def test_every_stored_schema_exists_and_is_valid():
    import jsonschema
    for name in ("claims", "evidence", "run_trace", "verdict", "generated", "meta"):
        schema = json.loads((store.SCHEMAS_DIR / f"{name}.schema.json").read_text())
        jsonschema.Draft202012Validator.check_schema(schema)


# ── candidates, webhooks, resumes ────────────────────────────────────────────

def test_candidates_json():
    cands = load("candidates.json")
    assert {"henry", "aidan"} <= set(cands)
    assert cands["henry"]["github_login"] == HENRY
    assert cands["aidan"]["github_login"] == AIDAN
    for entry in cands.values():
        assert set(entry) == {"name", "github_login", "resume", "webhook"}
        assert (FIXTURES / "resumes" / entry["resume"]).is_file()
        assert (FIXTURES / "webhooks" / entry["webhook"]).is_file()


@pytest.mark.parametrize("name", ["henry", "aidan"])
def test_webhook_shape(name):
    body = load(f"webhooks/{name}.json")
    assert body["action"] == "new_candidate_application"
    app = body["payload"]["application"]
    for key in ("id", "status", "applied_at", "url", "source", "current_stage",
                "custom_fields", "candidate", "jobs"):
        assert key in app
    cand = app["candidate"]
    for key in ("id", "first_name", "last_name", "email_addresses", "phone_numbers",
                "website_addresses", "social_media_addresses", "educations", "employments",
                "attachments", "tags", "custom_fields"):
        assert key in cand
    resumes = [a for a in cand["attachments"] if a["type"] == "resume"]
    entry = load("candidates.json")[name]
    assert resumes and resumes[0]["filename"] == entry["resume"]
    assert f"{cand['first_name']} {cand['last_name']}" == entry["name"]


@pytest.mark.parametrize("name", ["henry", "aidan"])
def test_resume_json_matches_pdf(name):
    saved = load(f"resumes/{name}.resume.json")
    assert saved == resume.read_resume(FIXTURES / "resumes" / f"{name}.pdf")


def test_resume_links():
    assert [l["uri"] for l in load("resumes/henry.resume.json")["links"]] == [f"https://github.com/{HENRY}"]
    assert [l["uri"] for l in load("resumes/aidan.resume.json")["links"]] == [f"https://github.com/{AIDAN}"]


# ── claims against resumes ───────────────────────────────────────────────────

@pytest.mark.parametrize("login, name", [(HENRY, "henry"), (AIDAN, "aidan")])
def test_claims_match_resume(login, name):
    claims, saved = load(f"claims/{login}.json"), load(f"resumes/{name}.resume.json")
    assert claims["extraction"]["resume_sha256"] == saved["sha256"]
    assert claims["candidate"]["github_login"] == login
    assert [c["claim_id"] for c in claims["claims"]] == [f"c{i}" for i in range(1, len(claims["claims"]) + 1)]
    lines = set(saved["text"].splitlines())
    for c in claims["claims"]:
        assert any(c["source_text"] in line for line in lines), c["source_text"]


def test_henry_claims_follow_the_design_doc():
    claims = load(f"claims/{HENRY}.json")["claims"]
    by_key = {claim_key(c): c for c in claims}
    cube = by_key["project:cubesolver"]
    assert cube["claim_id"] == "c3"
    assert cube["source_text"] == "Built cubesolver, a full Rubik's cube solver in C++ (2024)"
    assert cube["assertions"]["scale"] == "substantial"
    assert all(c["assertions"]["min_stars"] is None for c in claims if c["type"] == "project")
    assert {"skill:Python", "skill:Dart", "skill:JavaScript", "skill:C++",
            "unchecked:employment", "unchecked:education"} <= set(by_key)


def test_aidan_claims_follow_the_design_doc():
    claims = load(f"claims/{AIDAN}.json")["claims"]
    by_key = {claim_key(c): c for c in claims}
    portfolio = by_key["project:portfolio-aidan-hong"]["assertions"]
    assert portfolio["min_stars"] == 500 and portfolio["start_year"] == 2021
    oss = by_key["oss:boomty/CameraDetectionforTraffic"]["assertions"]
    assert oss["role"] == "contributor" and oss["min_prs"] == 2
    assert {"unchecked:education", "unchecked:other"} <= set(by_key)


def test_extraction_output_flattens_to_the_claims_fixture():
    claims = load(f"claims/{HENRY}.json")["claims"]
    flat = load("llm/extraction_output.json")["claims"]
    assert len(flat) == len(claims)
    for f, c in zip(flat, claims):
        assert (f["type"], f["section"], f["source_text"]) == (c["type"], c["section"], c["source_text"])
        a = c["assertions"]
        if c["type"] == "project":
            assert all(f[k] == v for k, v in a.items())
        elif c["type"] == "skill":
            assert (f["skill_name"], f["skill_kind"]) == (a["name"], a["kind"])
        else:
            assert f["category"] == a["category"]


# ── snapshot ─────────────────────────────────────────────────────────────────

def test_sample_snapshot_is_self_consistent():
    snap = FIXTURES / "snapshots" / "sample"
    ev = load("snapshots/sample/evidence.json")
    keys = [r["key"] for r in ev["responses"]]
    assert keys == sorted(keys) and len(set(keys)) == len(keys)
    for r in ev["responses"]:
        data = (snap / r["body_path"]).read_bytes()
        assert store.sha256_bytes(data) == r["body_sha256"]
        assert r["body_path"] == f"raw/{r['body_sha256']}.json"
        json.loads(data)
    for needed in (
        f"GET https://api.github.com/users/{HENRY}",
        f"GET https://api.github.com/users/{HENRY}/repos?per_page=100&sort=pushed&type=owner",
    ):
        assert needed in keys
    for repo in ("cubesolver", "polytrack-ml"):
        base = f"GET https://api.github.com/repos/{HENRY}/{repo}"
        for suffix in ("", "/languages", "/contributors?anon=1&per_page=100", "/commits?per_page=100"):
            assert base + suffix in keys, base + suffix
    # keys are in J2's canonical form: lowercased path, sorted query
    for k in keys:
        assert k.startswith("GET https://api.github.com/")
        assert k[len("GET "):] == k[len("GET "):].lower()
    assert ev["misses"] == [] and ev["decisions"] == []


def test_sample_snapshot_matches_design_doc_data():
    snap = FIXTURES / "snapshots" / "sample"
    ev = load("snapshots/sample/evidence.json")
    body = lambda key: json.loads(  # noqa: E731
        (snap / next(r["body_path"] for r in ev["responses"] if r["key"] == key)).read_text())
    base = "GET https://api.github.com"
    user = body(f"{base}/users/{HENRY}")
    assert (user["created_at"][:10], user["public_repos"], user["followers"]) == ("2021-09-03", 5, 4)
    repos = body(f"{base}/users/{HENRY}/repos?per_page=100&sort=pushed&type=owner")
    created = {r["name"]: r["created_at"][:10] for r in repos}
    assert created == {"Polytrack-ML": "2026-10-11", "CAC24": "2024-10-24", "cubesolver": "2024-12-05",
                       "fbla-coding": "2024-02-15", "henrychen4736.github.io": "2026-10-11"}
    assert all(not r["fork"] and r["stargazers_count"] == 0 for r in repos)
    cube_commits = body(f"{base}/repos/{HENRY}/cubesolver/commits?per_page=100")
    assert len(cube_commits) == 2
    assert body(f"{base}/repos/{HENRY}/cubesolver")["size"] == 3
    assert body(f"{base}/repos/{HENRY}/cubesolver/languages") == {"C++": 11434}
    poly = body(f"{base}/repos/{HENRY}/polytrack-ml/commits?per_page=100")
    assert len(poly) == 35 and all(c["author"] is None for c in poly)
    contribs = body(f"{base}/repos/{HENRY}/polytrack-ml/contributors?anon=1&per_page=100")
    assert contribs[0]["type"] == "Anonymous" and contribs[0]["name"] == "Henry Chen"
    assert set(body(f"{base}/repos/{HENRY}/polytrack-ml/languages")) == {"HTML", "JavaScript", "Python", "Shell"}


def test_run_trace_matches_cubesolver_claim_and_its_raw_files_exist():
    claims = load(f"claims/{HENRY}.json")["claims"]
    cube = next(c for c in claims if c["assertions"].get("repo_name") == "cubesolver")
    trace = load(f"run_traces/{cube['claim_id']}.json")
    assert trace["claim_id"] == cube["claim_id"]
    snap = FIXTURES / "snapshots" / "sample"
    for cmd in trace["agent"]["commands"]:
        assert (snap / cmd["output_path"]).is_file()
        assert store.sha256_bytes((snap / cmd["output_path"]).read_bytes()) in cmd["output_path"]


# ── verdict and expected labels ──────────────────────────────────────────────

def test_verdict_matches_claims_and_inputs():
    claims, verdict = load(f"claims/{HENRY}.json"), load(f"verdicts/{HENRY}.json")
    ev, trace = load("snapshots/sample/evidence.json"), load("run_traces/c3.json")
    assert verdict["application_id"] == claims["application_id"]
    assert [v["claim_id"] for v in verdict["claims"]] == [c["claim_id"] for c in claims["claims"]]
    assert [v["type"] for v in verdict["claims"]] == [c["type"] for c in claims["claims"]]
    inputs = verdict["inputs"]
    assert inputs["claims_sha256"] == store.sha256_json(claims)
    assert inputs["evidence_sha256"] == store.sha256_json(ev)
    assert inputs["snapshot_id"] == ev["snapshot_id"]
    assert inputs["run_traces"] == [{"claim_id": "c3", "sha256": store.sha256_json(trace)}]
    tally = {k: 0 for k in LABELS}
    for v in verdict["claims"]:
        tally[v["label"]] += 1
    assert verdict["tally"] == tally
    assert verdict["account"]["login"] == HENRY


def test_every_project_claim_has_a_passing_repo_check_with_facts():
    verdict = load(f"verdicts/{HENRY}.json")
    projects = [v for v in verdict["claims"] if v["type"] == "project"]
    assert len(projects) == 3
    for v in projects:
        repo = next(c for c in v["checks"] if c["check"] == "repo")
        assert repo["result"] == "pass" and repo["facts"]["repo"].startswith(f"{HENRY}/")
        assert v["checks"][0] is repo or v["checks"][0] == repo  # repo is always first


def test_verdict_labels_match_expected_labels():
    claims, verdict = load(f"claims/{HENRY}.json"), load(f"verdicts/{HENRY}.json")
    expected = load(f"expected/{HENRY}.labels.json")
    got = {claim_key(c): v["label"] for c, v in zip(claims["claims"], verdict["claims"])}
    assert got == expected
    # design doc 11.4
    for key in ("project:Polytrack-ML", "project:CAC24", "skill:Python", "skill:Dart", "skill:JavaScript"):
        assert expected[key] == "supported"
    assert expected["project:cubesolver"] == "partial"
    assert expected["unchecked:employment"] == expected["unchecked:education"] == "no_evidence"


def test_aidan_expected_labels_cover_every_claim():
    claims = load(f"claims/{AIDAN}.json")["claims"]
    expected = load(f"expected/{AIDAN}.labels.json")
    assert set(expected) == {claim_key(c) for c in claims}
    assert set(expected.values()) <= LABELS
    assert expected["project:portfolio-aidan-hong"] == "contradicted"
    assert expected["oss:boomty/CameraDetectionforTraffic"] == "supported"


# ── Slack payloads ───────────────────────────────────────────────────────────

def test_slack_payloads():
    recheck = load("slack/recheck.json")
    assert recheck["type"] == "block_actions"
    assert recheck["actions"][0]["action_id"] == "openclaw:rv:recheck"
    overflow = load("slack/overflow_more_context.json")
    act = overflow["actions"][0]
    assert act["action_id"] == "openclaw:rv:claim_menu" and act["block_id"] == "claim_c3"
    assert act["selected_option"]["value"].split("|")[0] == "more_context"
    edit = load("slack/edit_submit.json")
    assert edit["type"] == "view_submission"
    assert edit["view"]["callback_id"] == "openclaw:rv:edit_claim"
    meta = json.loads(edit["view"]["private_metadata"])
    assert set(meta) == {"application_id", "claim_id", "claims_version"}
    assert all(set(v) == {"v"} for v in edit["view"]["state"]["values"].values())
