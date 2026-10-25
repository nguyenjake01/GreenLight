"""Function signatures + docstrings for every swappable module (design doc §8.3).

These are the contract: real modules and ``stubs.py`` expose the same names with the
same parameters. Bodies are intentionally empty; nothing here runs.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal, Protocol

from verifier.contracts.snapshot import Snapshot
from verifier.contracts.types import (
    AgentResult, AnnotateReport, Application, CardOptions, ChatMessage, Claim, ClaimEdit,
    ClaimsDoc, Candidate, CommandRecord, ExecResult, FetchMode, Generated, JudgeResult, Label,
    LLMResponse, MessageRef, Meta, ResumeText, RunTrace, SlackMessage, UploadRef, Verdict,
    VerdictClaim, WriteBackResult,
)


# ══ Henry ═════════════════════════════════════════════════════════

# ── llm (stack/llm.py) ────────────────────────────────────────────
def llm_chat(messages: list[ChatMessage], schema: dict | None = None, *,
             temperature: float = 0.0, max_tokens: int = 2048,
             reasoning: bool = False, timeout_s: float = 120.0) -> LLMResponse:
    """POST {LLM_BASE_URL}/chat/completions with LLM_MODEL. Thread-safe. With `schema`, sends
    a strict json_schema response_format and validates the reply into `json`.
    No internal retries. Raises LLMError (HTTP/timeout) or LLMSchemaError (bad reply)."""
    ...


# ── extract (stack/extract.py) ────────────────────────────────────
def extract_claims(resume: ResumeText, candidate: Candidate, *, application_id: str) -> ClaimsDoc:
    """One llm_chat call with the extraction_output schema; returns claims_version 1 with
    `candidate` copied through unchanged. Raises LLMError only."""
    ...


# ── slack (stack/slack.py) ────────────────────────────────────────
def post_card(channel: str, msg: SlackMessage) -> MessageRef: ...
def update_card(ref: MessageRef, msg: SlackMessage) -> None: ...
def post_thread(parent: MessageRef, msg: SlackMessage) -> MessageRef: ...
def upload_file(parent: MessageRef, path: Path, *, title: str,
                comment: str | None = None) -> UploadRef:
    """Upload into the parent's thread."""
    ...
def get_permalink(ref: MessageRef) -> str: ...
def open_modal(trigger_id: str, view: dict) -> None:
    """The service opens the Edit modal (§8.4). All slack functions raise SlackError."""
    ...


# ── sandbox (stack/sandbox_openshell.py or core/harness.py) ───────
class Sandbox(Protocol):
    backend: str                                     # "openshell" | "docker" | "stub"

    def exec(self, cmd: str, timeout_s: float) -> ExecResult:
        """Run `bash -lc "<cmd>"` in /work; timeout kills (timed_out, exit_code None);
        each output stream capped at 1 MB."""
        ...


@contextmanager
def open_sandbox(repo_dir: Path) -> Iterator[Sandbox]:
    """A fresh sandbox per run; repo_dir copied in as /work. No network, non-root.
    Always torn down. Raises SandboxError."""
    ...


# ── agent (stack/agent.py) ────────────────────────────────────────
def agent_probe(sb: Sandbox, claim: Claim, readme: str | None, *,
                max_commands: int = 15, timeout_s: float = 300.0) -> AgentResult:
    """Loop llm_chat(agent_step) -> sb.exec until done, max_commands or timeout. `sb` is the
    harness's RecordingSandbox, so only metadata is returned. No errors escape."""
    ...


# ── judge (stack/judge.py) ────────────────────────────────────────
def judge(claim: Claim, transcript: list[CommandRecord]) -> JudgeResult:
    """One llm_chat call with the judge_outcome schema. Raises LLMError, LLMSchemaError."""
    ...


# ══ Jake ══════════════════════════════════════════════════════════

# ── resume (core/resume.py) ───────────────────────────────────────
def read_resume(pdf_path: Path) -> ResumeText:
    """pdftotext for text; PyMuPDF for link annotations. Raises ResumeError."""
    ...

def link_github(resume: ResumeText, app: Application) -> Candidate:
    """Find github.com/<login> in resume link URIs, then text, then Greenhouse websites,
    then social addresses. Resume sources win; a different Greenhouse login becomes
    github_login_conflict. No errors."""
    ...


# ── fetch (core/fetch.py) ─────────────────────────────────────────
def fetch_evidence(claims: ClaimsDoc, *, run_dir: Path, mode: FetchMode) -> Snapshot:
    """Create a new snapshot under run_dir/snapshots/, crawl GitHub, write evidence.json once.
    Network failures and replay misses go into `misses`. Raises FetchError (local failures)."""
    ...


# ── rubric (core/rubric.py) ───────────────────────────────────────
def compute_verdict(claims: ClaimsDoc, snapshot: Snapshot, run_traces: list[RunTrace], *,
                    verdict_n: int, computed_at: str) -> Verdict:
    """Pure: no clock, network or LLM. No errors."""
    ...


# ── render (core/render.py), all pure, no errors ──────────────────
def render_placeholder(app: Application) -> SlackMessage: ...
def render_card(app: Application, claims: ClaimsDoc | None, verdict: Verdict | None,
                meta: Meta, opts: CardOptions) -> SlackMessage:
    """verdict=None renders the "Could not read claims" card."""
    ...
def render_run_update(application_id: str, claim: Claim,
                      status: Literal["started", "done", "skipped", "error"],
                      trace: RunTrace | None, *, buttons: bool = True) -> SlackMessage: ...
def render_label_change(claim: Claim, edits: list[ClaimEdit],
                        old: Label, new: Label, by: str) -> SlackMessage: ...
def render_generated(claim: Claim, gen: Generated) -> SlackMessage: ...
def render_edit_modal(application_id: str, claims_version: int, claim: Claim) -> dict: ...
def render_note(app: Application, claims: ClaimsDoc, verdict: Verdict,
                permalink: str | None) -> str: ...


# ── annotate (core/annotate.py) ───────────────────────────────────
def annotate_pdf(resume_pdf: Path, claims: ClaimsDoc, verdict: Verdict,
                 out_path: Path) -> AnnotateReport:
    """Writes a new file; never touches resume_pdf. Raises AnnotateError."""
    ...


# ── ats (ats/adapter.py) ──────────────────────────────────────────
class GreenhouseAdapter:
    def __init__(self, *, webhook_secret: str, harvest_auth_url: str, harvest_base_url: str,
                 client_id: str, client_secret: str, note_visibility: str = "private"): ...

    def on_new_application(self, callback: Callable[[Application], None]) -> None: ...

    def handle_webhook(self, raw_body: bytes,
                       headers: Mapping[str, str]) -> Literal["accepted", "duplicate", "ignored"]:
        """Verify `Signature: sha256 <hex>` (else SignatureError); dedupe by event id; ignore
        other actions; save webhook.json; call the callbacks."""
        ...

    def get_application(self, application_id: str) -> Application: ...
    def fetch_resume(self, app: Application, dest: Path) -> Path: ...
    def write_back(self, app: Application, note_text: str,
                   attachment_pdf: Path) -> WriteBackResult:
        """Note on the candidate + PDF on the application (type "other"). Raises HarvestError."""
        ...


# ── harness (core/harness.py) ─────────────────────────────────────
def run_check(snapshot: Snapshot, claim: Claim, repo_full_name: str, *,
              mode: FetchMode) -> RunTrace:
    """Check out the repo, open a sandbox, baseline, agent, judge; write
    run_traces/{claim_id}.json; return the trace. Raises SandboxError."""
    ...


__all__ = [
    "llm_chat", "extract_claims", "post_card", "update_card", "post_thread", "upload_file",
    "get_permalink", "open_modal", "Sandbox", "open_sandbox", "agent_probe", "judge",
    "read_resume", "link_github", "fetch_evidence", "compute_verdict", "render_placeholder",
    "render_card", "render_run_update", "render_label_change", "render_generated",
    "render_edit_modal", "render_note", "annotate_pdf", "GreenhouseAdapter", "run_check",
]
