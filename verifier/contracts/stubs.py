"""Working fakes for every interface (design doc §8.1, §8.3).

One namespace per registry name, exposing the same names as the real module (functions;
``GreenhouseAdapter`` for ``ats``; ``open_sandbox`` for ``sandbox``). Deterministic, offline,
under 100 ms. Anything written under ``runs/`` goes through ``store`` (except the Slack stub's
append-only log, which ``store``'s write-once API can't express).
"""
from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

from verifier.contracts.config import env
from verifier.contracts.errors import LLMSchemaError, ResumeError
from verifier.contracts.snapshot import Snapshot
from verifier.contracts.types import (
    AgentResult, AnnotateReport, Application, Candidate, CardOptions, ChatMessage, Claim,
    ClaimEdit, ClaimsDoc, CommandRecord, ExecResult, FetchMode, Generated, JudgeResult, Label,
    LLMResponse, MessageRef, Meta, ResumeText, RunTrace, SlackMessage, UploadRef, Verdict,
    WriteBackResult,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
_DEFAULT_LOGIN = "henrychen4736"


def _store():
    from verifier.core import store  # lazy: store is lane B's module
    return store


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


# ══ llm ═══════════════════════════════════════════════════════════
def _llm_chat(messages: list[ChatMessage], schema: dict | None = None, *,
              temperature: float = 0.0, max_tokens: int = 2048,
              reasoning: bool = False, timeout_s: float = 120.0) -> LLMResponse:
    if schema is None:
        return {"text": "(stub reply)", "json": None, "model": "stub", "duration_s": 0.0,
                "usage": {"prompt_tokens": 0, "completion_tokens": 0}}
    path = FIXTURES / "llm" / f"{schema['title']}.json"
    if not path.exists():
        raise LLMSchemaError(f"stub has no fixture for schema {schema['title']!r}", "")
    data = _load(path)
    return {"text": json.dumps(data, sort_keys=True), "json": data, "model": "stub",
            "duration_s": 0.0, "usage": {"prompt_tokens": 0, "completion_tokens": 0}}


llm = SimpleNamespace(llm_chat=_llm_chat)


# ══ extract ═══════════════════════════════════════════════════════
def _extract_claims(resume: ResumeText, candidate: Candidate, *,
                    application_id: str) -> ClaimsDoc:
    login = candidate.get("github_login")
    path = FIXTURES / "claims" / f"{login}.json"
    if not login or not path.exists():
        path = FIXTURES / "claims" / f"{_DEFAULT_LOGIN}.json"
    doc = _load(path)
    doc["application_id"] = application_id
    doc["candidate"] = copy.deepcopy(candidate)
    return doc


extract = SimpleNamespace(extract_claims=_extract_claims)


# ══ slack ═════════════════════════════════════════════════════════
def _slack_log_path() -> Path:
    return Path(env("RUNS_DIR")) / "_slack_stub.jsonl"


def _next_seq() -> int:
    log = _slack_log_path()
    return (sum(1 for _ in log.open(encoding="utf-8")) if log.exists() else 0) + 1


def _slack_write(call: str, **fields: Any) -> dict:
    log = _slack_log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    seq = _next_seq()
    fields.setdefault("ts", None)
    line = {"seq": seq, "call": call, "channel": fields.get("channel"), "ts": fields["ts"],
            "thread_ts": fields.get("thread_ts"), "text": fields.get("text"),
            "n_blocks": fields.get("n_blocks", 0), "path": fields.get("path")}
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps(line, sort_keys=True, ensure_ascii=False) + "\n")
    text = (line["text"] or "").replace("\n", " ")[:70]
    print(f"[slack stub] #{seq} {call} ts={line['ts']} {text!r}")
    return line


def _save_last_card(msg: SlackMessage) -> None:
    p = Path(env("RUNS_DIR")) / "_slack_last_card.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"blocks": msg["blocks"]}, indent=2, ensure_ascii=False) + "\n",
                 encoding="utf-8")


def _post_card(channel: str, msg: SlackMessage) -> MessageRef:
    channel = channel or "C0STUB"  # SLACK_CHANNEL_ID is blank in a fresh .env
    seq = _next_seq()
    ts = f"stub.{seq}"
    _slack_write("post_card", channel=channel, ts=ts, text=msg["text"],
                 n_blocks=len(msg["blocks"]))
    _save_last_card(msg)
    return {"channel": channel, "ts": ts}


def _update_card(ref: MessageRef, msg: SlackMessage) -> None:
    _slack_write("update_card", channel=ref["channel"], ts=ref["ts"], text=msg["text"],
                 n_blocks=len(msg["blocks"]))
    _save_last_card(msg)


def _post_thread(parent: MessageRef, msg: SlackMessage) -> MessageRef:
    ts = f"stub.{_next_seq()}"
    _slack_write("post_thread", channel=parent["channel"], ts=ts, thread_ts=parent["ts"],
                 text=msg["text"], n_blocks=len(msg["blocks"]))
    return {"channel": parent["channel"], "ts": ts}


def _upload_file(parent: MessageRef, path: Path, *, title: str,
                 comment: str | None = None) -> UploadRef:
    seq = _next_seq()
    _slack_write("upload_file", channel=parent["channel"], ts=None, thread_ts=parent["ts"],
                 text=comment or title, n_blocks=0, path=str(path))
    return {"file_id": f"stub-file-{seq}", "permalink": f"file://{path}"}


def _get_permalink(ref: MessageRef) -> str:
    _slack_write("get_permalink", channel=ref["channel"], ts=ref["ts"], text=None)
    return f"https://stub.slack.local/archives/{ref['channel']}/p{ref['ts']}"


def _open_modal(trigger_id: str, view: dict) -> None:
    _slack_write("open_modal", channel=None, ts=None, text=view.get("callback_id"),
                 n_blocks=len(view.get("blocks", [])))


slack = SimpleNamespace(post_card=_post_card, update_card=_update_card,
                        post_thread=_post_thread, upload_file=_upload_file,
                        get_permalink=_get_permalink, open_modal=_open_modal)


# ══ sandbox / agent / judge ═══════════════════════════════════════
class _StubSandbox:
    backend = "stub"

    def exec(self, cmd: str, timeout_s: float) -> ExecResult:
        return {"exit_code": 0, "stdout": f"(stub) {cmd}\n".encode(), "stderr": b"",
                "duration_s": 0.01, "timed_out": False}


@contextmanager
def _open_sandbox(repo_dir: Path) -> Iterator[_StubSandbox]:
    yield _StubSandbox()


sandbox = SimpleNamespace(open_sandbox=_open_sandbox)


def _agent_probe(sb: Any, claim: Claim, readme: str | None, *,
                 max_commands: int = 15, timeout_s: float = 300.0) -> AgentResult:
    sb.exec("ls -la", 10.0)
    sb.exec("echo stub-run", 10.0)
    return {"model": "stub", "prompt_version": "run-v1", "stopped": "done",
            "final_note": "(stub) ran two commands"}


agent = SimpleNamespace(agent_probe=_agent_probe)


def _judge(claim: Claim, transcript: list[CommandRecord]) -> JudgeResult:
    return {"model": "stub", "prompt_version": "judge-v1", "outcome": "works_as_claimed",
            "rationale": "(stub) not judged"}


judge = SimpleNamespace(judge=_judge)


# ══ resume ════════════════════════════════════════════════════════
def _read_resume(pdf_path: Path) -> ResumeText:
    sha = hashlib.sha256(Path(pdf_path).read_bytes()).hexdigest()
    chosen = None
    for p in sorted((FIXTURES / "resumes").glob("*.resume.json")):
        data = _load(p)
        if data.get("sha256") == sha:
            chosen = data
            break
    if chosen is None:
        fallback = FIXTURES / "resumes" / "henry.resume.json"
        if not fallback.exists():
            raise ResumeError("stub resume fixtures are missing")
        chosen = _load(fallback)
    return copy.deepcopy(chosen)


def _link_github(resume: ResumeText, app: Application) -> Candidate:
    login: str | None = None
    filename = (app.get("resume") or {}).get("filename")
    candidates = FIXTURES / "candidates.json"
    if filename and candidates.exists():
        for entry in _load(candidates).values():
            if entry.get("resume") == filename:
                login = entry.get("github_login")
                break
    return {"name": app["candidate_name"], "emails": list(app["emails"]),
            "github_login": login,
            "github_login_source": "resume_link" if login else "none",
            "github_login_conflict": None}


resume = SimpleNamespace(read_resume=_read_resume, link_github=_link_github)


# ══ fetch ═════════════════════════════════════════════════════════
def _new_snapshot_dir(run_dir: Path) -> tuple[str, Path]:
    when = datetime.now(timezone.utc).replace(microsecond=0)
    while True:
        snapshot_id = "snap_" + when.strftime("%Y%m%dT%H%M%SZ")
        path = Path(run_dir) / "snapshots" / snapshot_id
        if not path.exists():
            return snapshot_id, path
        when += timedelta(seconds=1)


def _fetch_evidence(claims: ClaimsDoc, *, run_dir: Path, mode: FetchMode) -> Snapshot:
    store = _store()
    src = FIXTURES / "snapshots" / "sample"
    snapshot_id, dest = _new_snapshot_dir(run_dir)
    evidence = _load(src / "evidence.json")
    evidence["snapshot_id"] = snapshot_id
    evidence["application_id"] = claims["application_id"]
    raw_src = src / "raw"
    if raw_src.is_dir():
        (dest / "raw").mkdir(parents=True, exist_ok=True)
        for f in sorted(raw_src.iterdir()):
            if f.is_file():
                store.write_bytes(dest / "raw" / f.name, f.read_bytes())
    dest.mkdir(parents=True, exist_ok=True)
    store.write_json(dest / "evidence.json", evidence)
    return Snapshot.load(dest)


fetch = SimpleNamespace(fetch_evidence=_fetch_evidence)


# ══ rubric ════════════════════════════════════════════════════════
def _compute_verdict(claims: ClaimsDoc, snapshot: Snapshot, run_traces: list[RunTrace], *,
                     verdict_n: int, computed_at: str) -> Verdict:
    store = _store()
    login = claims["candidate"].get("github_login")
    verdict: Verdict | None = None
    if login:
        path = FIXTURES / "verdicts" / f"{login}.json"
        if path.exists():
            fixture = _load(path)
            if [c["claim_id"] for c in fixture["claims"]] == [c["claim_id"] for c in claims["claims"]]:
                verdict = fixture
    if verdict is None:
        labels = [{"claim_id": c["claim_id"], "type": c["type"], "label": "no_evidence",
                   "flags": [], "top_reason": "(stub) not checked", "checks": [], "notes": [],
                   "evidence_links": []} for c in claims["claims"]]
        verdict = {
            "schema_version": "1", "verdict_id": "", "application_id": "", "computed_at": "",
            "rubric_version": "rubric-v1", "inputs": {}, "account": None,
            "tally": {"supported": 0, "partial": 0, "contradicted": 0,
                      "no_evidence": len(labels)},
            "context": [], "claims": labels,
        }
    verdict["verdict_id"] = f"verdict_{verdict_n}"
    verdict["application_id"] = claims["application_id"]
    verdict["computed_at"] = computed_at
    verdict["inputs"] = {
        "claims_version": claims["claims_version"],
        "claims_sha256": store.sha256_json(claims),
        "snapshot_id": snapshot.snapshot_id,
        "evidence_sha256": store.sha256_json(snapshot.evidence),
        "run_traces": [{"claim_id": t["claim_id"], "sha256": store.sha256_json(t)}
                       for t in run_traces],
    }
    return verdict


rubric = SimpleNamespace(compute_verdict=_compute_verdict)


# ══ render ════════════════════════════════════════════════════════
def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _msg(text: str) -> SlackMessage:
    return {"text": text, "blocks": [_section(text)]}


def _tally_line(verdict: Verdict) -> str:
    t = verdict["tally"]
    return (f"{t.get('supported', 0)} supported · {t.get('partial', 0)} partial · "
            f"{t.get('contradicted', 0)} contradicted · {t.get('no_evidence', 0)} no evidence")


def _render_placeholder(app: Application) -> SlackMessage:
    return _msg(f"New application: {app['candidate_name']} — verifying…")


def _render_card(app: Application, claims: ClaimsDoc | None, verdict: Verdict | None,
                 meta: Meta, opts: CardOptions) -> SlackMessage:
    title = f"{app['candidate_name']} — {app.get('job_title') or 'application'}"
    header = {"type": "header", "text": {"type": "plain_text", "text": title[:150]}}
    if verdict is None:
        text = f"Could not read claims for {app['candidate_name']}"
        return {"text": text, "blocks": [header, _section("Could not read claims")]}
    line = _tally_line(verdict)
    return {"text": f"{title}: {line}", "blocks": [header, _section(line)]}


def _render_run_update(application_id: str, claim: Claim,
                       status: Literal["started", "done", "skipped", "error"],
                       trace: RunTrace | None, *, buttons: bool = True) -> SlackMessage:
    label = claim["assertions"].get("repo_name") or claim["claim_id"]
    return _msg(f"Run check {status}: {label}")


def _render_label_change(claim: Claim, edits: list[ClaimEdit], old: Label, new: Label,
                         by: str) -> SlackMessage:
    return _msg(f"{claim['claim_id']}: {old} -> {new} (edited by {by})")


def _render_generated(claim: Claim, gen: Generated) -> SlackMessage:
    return _msg(gen["text"])


def _render_edit_modal(application_id: str, claims_version: int, claim: Claim) -> dict:
    return {
        "type": "modal", "callback_id": "openclaw:rv:edit_claim",
        "private_metadata": json.dumps({"application_id": application_id,
                                        "claim_id": claim["claim_id"],
                                        "claims_version": claims_version}),
        "title": {"type": "plain_text", "text": "Edit claim"},
        "submit": {"type": "plain_text", "text": "Save"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [_section("(stub) no editable fields")],
    }


def _render_note(app: Application, claims: ClaimsDoc, verdict: Verdict,
                 permalink: str | None) -> str:
    return _tally_line(verdict)


render = SimpleNamespace(
    render_placeholder=_render_placeholder, render_card=_render_card,
    render_run_update=_render_run_update, render_label_change=_render_label_change,
    render_generated=_render_generated, render_edit_modal=_render_edit_modal,
    render_note=_render_note)


# ══ annotate ══════════════════════════════════════════════════════
def _annotate_pdf(resume_pdf: Path, claims: ClaimsDoc, verdict: Verdict,
                  out_path: Path) -> AnnotateReport:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _store().write_bytes(out_path, Path(resume_pdf).read_bytes())
    return {"out_path": str(out_path), "located": [],
            "not_located": [c["claim_id"] for c in claims["claims"]]}


annotate = SimpleNamespace(annotate_pdf=_annotate_pdf)


# ══ ats ═══════════════════════════════════════════════════════════
def _normalize_application(body: dict, event_id: str) -> Application:
    app = body["payload"]["application"]
    cand = app.get("candidate") or {}
    values = lambda key: [x["value"] for x in (cand.get(key) or [])]  # noqa: E731
    resume = next(({"filename": a["filename"], "url": a["url"], "type": a["type"]}
                   for a in (cand.get("attachments") or []) if a.get("type") == "resume"), None)
    jobs = app.get("jobs") or []
    return {
        "application_id": str(app["id"]),
        "candidate_id": str(cand.get("id", "")),
        "candidate_name": f"{cand.get('first_name', '')} {cand.get('last_name', '')}".strip(),
        "emails": values("email_addresses"),
        "website_addresses": values("website_addresses"),
        "social_media_addresses": values("social_media_addresses"),
        "job_title": jobs[0].get("name") if jobs else None,
        "applied_at": app.get("applied_at", ""),
        "profile_url": app.get("url", ""),
        "resume": resume,
        "event_id": event_id,
    }


class _StubGreenhouseAdapter:
    """Stub adapter: no signature check, no network, in-process duplicate detection."""

    def __init__(self, *, webhook_secret: str = "", harvest_auth_url: str = "",
                 harvest_base_url: str = "", client_id: str = "", client_secret: str = "",
                 note_visibility: str = "private"):
        self._callbacks: list[Callable[[Application], None]] = []
        self._seen: set[str] = set()

    def on_new_application(self, callback: Callable[[Application], None]) -> None:
        self._callbacks.append(callback)

    def handle_webhook(self, raw_body: bytes,
                       headers: Mapping[str, str]) -> Literal["accepted", "duplicate", "ignored"]:
        lowered = {k.lower(): v for k, v in headers.items()}
        body = json.loads(raw_body)
        if body.get("action") != "new_candidate_application":
            return "ignored"
        event_id = lowered.get("greenhouse-event-id") or hashlib.sha256(raw_body).hexdigest()
        if event_id in self._seen:
            return "duplicate"
        self._seen.add(event_id)
        app = _normalize_application(body, event_id)
        store = _store()
        rd = store.run_dir(app["application_id"])
        store.write_bytes(rd / "webhook.json", raw_body)
        store.write_json(rd / "webhook_headers.json",
                         {"event_id": event_id, "signature": lowered.get("signature", "")})
        for cb in self._callbacks:
            cb(app)
        return "accepted"

    def get_application(self, application_id: str) -> Application:
        store = _store()
        rd = store.run_dir(application_id)
        body = store.read_json(rd / "webhook.json")
        headers = store.read_json(rd / "webhook_headers.json")
        return _normalize_application(body, headers["event_id"])

    def fetch_resume(self, app: Application, dest: Path) -> Path:
        ref = app.get("resume")
        if not ref:
            raise ResumeError("application has no resume")
        src = FIXTURES / "resumes" / Path(ref["filename"]).name
        if not src.exists():
            raise ResumeError(f"stub has no resume fixture {ref['filename']!r}")
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        _store().write_bytes(dest, src.read_bytes())
        return dest

    def write_back(self, app: Application, note_text: str,
                   attachment_pdf: Path) -> WriteBackResult:
        store = _store()
        base = store.run_dir(app["application_id"]) / "writeback_stub"
        n = 1
        while (base / str(n)).exists():
            n += 1
        out = base / str(n)
        out.mkdir(parents=True, exist_ok=True)
        store.write_text(out / "note.txt", note_text)
        pdf = Path(attachment_pdf)
        store.write_bytes(out / pdf.name, pdf.read_bytes())
        return {"note_id": f"stub-note-{n}", "attachment_id": f"stub-att-{n}",
                "attachment_url": f"file://{out / pdf.name}"}


ats = SimpleNamespace(GreenhouseAdapter=_StubGreenhouseAdapter)


# ══ harness ═══════════════════════════════════════════════════════
def _run_check(snapshot: Snapshot, claim: Claim, repo_full_name: str, *,
               mode: FetchMode) -> RunTrace:
    fixture = FIXTURES / "run_traces" / f"{claim['claim_id']}.json"
    if fixture.exists():
        trace = _load(fixture)
    else:
        trace = {
            "schema_version": "1", "claim_id": claim["claim_id"], "repo": repo_full_name,
            "commit_sha": "0" * 40, "sandbox_backend": "stub",
            "baseline": {"step": "skip", "command": None, "exit_code": None, "duration_s": 0.0,
                         "timed_out": False, "failure_kind": None, "output_excerpt": "",
                         "output_path": None},
            "agent": None, "judge": None,
        }
    traces = snapshot.traces_dir()
    traces.mkdir(parents=True, exist_ok=True)
    _store().write_json(traces / f"{claim['claim_id']}.json", trace)
    return trace


harness = SimpleNamespace(run_check=_run_check)
