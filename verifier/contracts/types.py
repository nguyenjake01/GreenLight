"""Shared types (design doc §8.2). JSON-shaped TypedDicts, so they serialize directly."""
from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict

Label        = Literal["supported", "partial", "contradicted", "no_evidence"]
CheckResult  = Literal["pass", "soft", "hard", "skip"]
FetchMode    = Literal["record", "replay", "live"]
ClaimType    = Literal["project", "skill", "oss_contribution", "unchecked"]
Flag         = Literal["run_failed", "run_timeout"]
JudgeOutcome = Literal["works_as_claimed", "runs_but_mismatch", "failed_to_run", "could_not_attempt"]
RunStatus    = Literal["queued", "running", "done", "skipped", "error"]

# ── ATS ───────────────────────────────────────────────────────────
class AttachmentRef(TypedDict):
    filename: str
    url: str
    type: str                        # "resume" | "cover_letter" | "other" | …

class Application(TypedDict):        # normalized from the webhook payload
    application_id: str
    candidate_id: str
    candidate_name: str              # "first last"
    emails: list[str]                # email_addresses[].value
    website_addresses: list[str]     # website_addresses[].value
    social_media_addresses: list[str]
    job_title: str | None            # jobs[0].name
    applied_at: str
    profile_url: str                 # application.url: the candidate's page in the ATS
    resume: AttachmentRef | None     # first attachment with type "resume"
    event_id: str                    # Greenhouse-Event-ID header

class WriteBackResult(TypedDict):
    note_id: str
    attachment_id: str
    attachment_url: str              # expires after 7 days in real Greenhouse

# ── Resume ────────────────────────────────────────────────────────
class ResumeLink(TypedDict):
    uri: str
    anchor_text: str                 # text under the link rectangle; "" if none
    page: int                        # 0-based

class ResumeText(TypedDict):
    text: str                        # pdftotext output
    links: list[ResumeLink]          # PDF link annotations (PyMuPDF)
    sha256: str                      # of the PDF bytes
    page_count: int

class LoginConflict(TypedDict):
    login: str
    source: str

class Candidate(TypedDict):
    name: str
    emails: list[str]
    github_login: str | None
    github_login_source: Literal["resume_link", "greenhouse_website", "greenhouse_social", "none"]
    github_login_conflict: LoginConflict | None

# ── Claims ────────────────────────────────────────────────────────
class ProjectAssertions(TypedDict):
    repo_url: str | None
    repo_name: str | None
    role: Literal["creator", "co-creator", "maintainer"] | None
    language: str | None
    min_stars: int | None
    start_year: int | None
    scale: Literal["small", "substantial"] | None
    public_signal: Literal["link", "open_source_wording", "none"]
    behavior: str | None

class SkillAssertions(TypedDict):
    name: str
    kind: Literal["language", "framework_tool"]

class ContributionAssertions(TypedDict):
    repo: str                        # "owner/repo"
    role: Literal["contributor", "maintainer"]
    min_prs: int | None
    description: str | None

class UncheckedAssertions(TypedDict):
    category: Literal["employment", "title", "education", "certification",
                      "impact_metric", "private_work", "other"]

class Claim(TypedDict):
    claim_id: str
    type: ClaimType
    section: str | None
    source_text: str
    needs_review: bool
    assertions: ProjectAssertions | SkillAssertions | ContributionAssertions | UncheckedAssertions

class Extraction(TypedDict):
    model: str
    prompt_version: str
    temperature: float
    resume_sha256: str
    status: Literal["ok", "retried", "failed"]

ClaimEdit = TypedDict("ClaimEdit", {    # functional form because "from" is a keyword
    "claims_version": int, "claim_id": str, "field": str,
    "from": Any, "to": Any, "by": str, "at": str})

class ClaimsDoc(TypedDict):
    schema_version: Literal["1"]
    application_id: str
    claims_version: int
    created_at: str
    candidate: Candidate
    extraction: Extraction
    edits: list[ClaimEdit]
    claims: list[Claim]

# ── Evidence ──────────────────────────────────────────────────────
class ResponseRecord(TypedDict):
    key: str                         # cache_key(method, url)
    status: int
    fetched_at: str
    etag: str | None
    body_sha256: str
    body_path: str                   # relative to the snapshot dir: "raw/<sha>.json"

class Decision(TypedDict):
    kind: Literal["repo_match"]
    claim_id: str
    model: str
    prompt_version: str
    inputs: dict[str, Any]
    output: str                      # repo name or "none"

class EvidenceDoc(TypedDict):
    schema_version: Literal["1"]
    snapshot_id: str
    application_id: str
    github_login: str | None
    fetch_mode: FetchMode
    created_at: str
    responses: list[ResponseRecord]
    misses: list[str]                # keys unavailable (replay miss or network failure)
    decisions: list[Decision]

# ── Run check ─────────────────────────────────────────────────────
class ExecResult(TypedDict):         # in memory only, never serialized
    exit_code: int | None            # None if killed on timeout
    stdout: bytes                    # capped at 1 MB
    stderr: bytes                    # capped at 1 MB
    duration_s: float
    timed_out: bool

class CommandRecord(TypedDict):
    cmd: str
    exit_code: int | None
    duration_s: float
    timed_out: bool
    output_excerpt: str              # last 20 lines, ≤ 1,500 chars (stdout, then stderr)
    output_path: str                 # "raw/<sha>.txt", relative to the snapshot dir

class BaselineRecord(TypedDict):
    step: Literal["tests", "build", "syntax", "skip"]
    command: str | None
    exit_code: int | None
    duration_s: float
    timed_out: bool
    failure_kind: Literal["deps_missing", "needs_network", "error"] | None
    output_excerpt: str
    output_path: str | None

AgentStop = Literal["done", "max_commands", "timeout", "error"]

class AgentResult(TypedDict):        # what agent_probe returns
    model: str
    prompt_version: str
    stopped: AgentStop
    final_note: str

class AgentRecord(TypedDict):        # what the trace stores (the harness adds commands)
    model: str
    prompt_version: str
    limits: dict[str, int]           # {"max_commands": 15, "timeout_s": 300}
    stopped: AgentStop
    timed_out: bool
    commands: list[CommandRecord]

class JudgeResult(TypedDict):
    model: str
    prompt_version: str
    outcome: JudgeOutcome
    rationale: str                   # ≤ 300 chars

class RunTrace(TypedDict):
    schema_version: Literal["1"]
    claim_id: str
    repo: str                        # "owner/repo"
    commit_sha: str
    sandbox_backend: str             # "openshell" | "docker" | "stub"
    baseline: BaselineRecord
    agent: AgentRecord | None
    judge: JudgeResult | None

# ── Verdict ───────────────────────────────────────────────────────
class Check(TypedDict):
    check: str                       # check names are listed in the rubric spec (Part D)
    result: CheckResult
    reason: str
    facts: NotRequired[dict[str, Any]]

class VerdictClaim(TypedDict):
    claim_id: str
    type: ClaimType
    label: Label
    flags: list[Flag]
    top_reason: str
    checks: list[Check]
    notes: list[str]
    evidence_links: list[str]

class Account(TypedDict):
    login: str
    login_source: str
    login_conflict: LoginConflict | None
    created_at: str | None           # YYYY-MM-DD
    public_repos: int | None
    followers: int | None

class Verdict(TypedDict):
    schema_version: Literal["1"]
    verdict_id: str                  # "verdict_{n}"
    application_id: str
    computed_at: str
    rubric_version: str              # "rubric-v1"
    inputs: dict[str, Any]           # claims_version, claims_sha256, snapshot_id,
                                     # evidence_sha256, run_traces[{claim_id, sha256}]
    account: Account | None
    tally: dict[str, int]            # supported, partial, contradicted, no_evidence
    context: list[str]
    claims: list[VerdictClaim]

# ── Generated text ────────────────────────────────────────────────
class Generated(TypedDict):
    claim_id: str
    kind: Literal["more_context", "suggest_questions"]
    verdict_id: str
    model: str
    prompt_version: str
    created_at: str
    text: str

# ── Slack ─────────────────────────────────────────────────────────
class SlackMessage(TypedDict):
    text: str                        # notification/fallback text, always set
    blocks: list[dict[str, Any]]     # ≤ 50 blocks

class MessageRef(TypedDict):
    channel: str
    ts: str

class UploadRef(TypedDict):
    file_id: str
    permalink: str

class CardOptions(TypedDict):        # built by the pipeline from config
    profile_url: str                 # "Open in Greenhouse"
    annotated_pdf_url: str | None    # Slack permalink of the latest uploaded PDF; button hidden while None
    action_buttons: bool             # Re-check, overflow menus, Show transcript; False when SLACK_ACTIONS=off
    menu: list[Literal["more_context", "suggest_questions", "edit_claim"]]  # overflow options, from SLACK_ACTIONS and CUT

# ── LLM ───────────────────────────────────────────────────────────
class ChatMessage(TypedDict):
    role: Literal["system", "user", "assistant"]
    content: str

class LLMResponse(TypedDict):
    text: str
    json: Any | None                 # parsed and validated when a schema was given
    model: str
    duration_s: float
    usage: dict[str, int]            # prompt_tokens, completion_tokens

# ── Other ─────────────────────────────────────────────────────────
class AnnotateReport(TypedDict):
    out_path: str
    located: list[str]               # claim_ids drawn on the page
    not_located: list[str]           # claim_ids only on the notes page

class Meta(TypedDict):               # runs/{id}/meta.json (§7.2)
    application_id: str
    candidate_id: str
    candidate_name: str
    job_title: str | None
    event_id: str
    applied_at: str                  # copied from the Application [new]
    latest: dict[str, Any]           # claims_version, snapshot_id, verdict_n
    slack: dict[str, Any]            # channel, card_ts, card_permalink, pdf_permalink
    runs: dict[str, RunStatus]       # claim_id → status
    stage_errors: list[dict[str, Any]]
    writebacks: list[dict[str, Any]]
