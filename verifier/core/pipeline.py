"""Orchestrator (design doc §5, §8.3, J7): runs the pipeline for one application at a time.

Flows: ``run`` (new application), ``recheck``, ``edit_claim``; side flows: ``generate``,
``show_transcript``; read helpers for the API: ``get_state``, ``find_applications``,
``find_application``, ``find_claim``.

Rules kept here:
- Modules are reached only through ``registry.get`` (looked up on every use, so tests can swap
  them); ``runs/`` is written only through ``store``.
- A per-application lock covers a whole flow (stages, run checks, write-back). A re-check or
  edit that arrives mid-flow waits; ``is_busy`` tells the API to answer ``queued``.
- Every stage is wrapped. A failure goes into ``meta.stage_errors`` and the flow continues
  where it can; nothing is raised out of a flow (§5.6).
- No labels or reasons are computed here, and there is no Block Kit beyond one-line replies.
"""
from __future__ import annotations

import copy
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from verifier.contracts import registry
from verifier.contracts.config import env
from verifier.contracts.errors import LLMError, ResumeError, WriteOnceError
from verifier.contracts.snapshot import Snapshot, cache_key
from verifier.contracts.types import (
    Application, CardOptions, Claim, ClaimsDoc, Meta, MessageRef, RunTrace, SlackMessage, Verdict,
)
from verifier.core import store

log = logging.getLogger(__name__)

# ── module-level state ────────────────────────────────────────────────────────

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
_SANDBOX_SEM = threading.BoundedSemaphore(2)     # two sandboxes at once across applications
_SLACK_LOCK = threading.RLock()                  # keeps Slack calls from interleaving
_APPS: dict[str, Application] = {}               # applications seen by `run`, for later flows

LABEL_EMOJI = {"supported": "🟢", "partial": "🟡", "contradicted": "🔴", "no_evidence": "⚪"}
GENERATE_KINDS = {"more_context": "More context", "suggest_questions": "Suggested questions"}


def _lock(application_id: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(str(application_id), threading.Lock())


def is_busy(application_id: str) -> bool:
    """True while a flow (run, recheck, edit) holds the application's lock: the API reports
    a new re-check or edit as `queued`."""
    return _lock(application_id).locked()


def _m(name: str):
    return registry.get(name)


# ── small helpers ─────────────────────────────────────────────────────────────

def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "_", name.lower())


def _text_msg(text: str) -> SlackMessage:
    """A one-line thread reply."""
    return {"text": text, "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]}


def _first_line(exc: BaseException | str) -> str:
    text = str(exc).strip() if not isinstance(exc, str) else exc.strip()
    line = text.splitlines()[0] if text else ""
    if not line and not isinstance(exc, str):
        line = type(exc).__name__
    return line[:200]


def _claim_name(claim: Claim) -> str:
    a = claim["assertions"]
    if claim["type"] == "project":
        name = a.get("repo_name")
        if not name and a.get("repo_url"):
            name = a["repo_url"].rstrip("/").rsplit("/", 1)[-1]
        return name or claim["claim_id"]
    if claim["type"] == "skill":
        return a.get("name") or claim["claim_id"]
    if claim["type"] == "oss_contribution":
        return a.get("repo") or claim["claim_id"]
    return (claim.get("source_text") or claim["claim_id"])[:40]


def _passing_repos(verdict: Verdict) -> list[tuple[str, str]]:
    """(claim_id, "owner/repo") for each project claim whose `repo` check passed, in claim order."""
    out: list[tuple[str, str]] = []
    for vc in verdict["claims"]:
        if vc["type"] != "project":
            continue
        for chk in vc["checks"]:
            repo = (chk.get("facts") or {}).get("repo")
            if chk["check"] == "repo" and chk["result"] == "pass" and repo:
                out.append((vc["claim_id"], repo))
                break
    return out


def _adapter():
    return _m("ats").GreenhouseAdapter(
        webhook_secret=env("GREENHOUSE_WEBHOOK_SECRET"),
        harvest_auth_url=env("HARVEST_AUTH_URL"),
        harvest_base_url=env("HARVEST_BASE_URL"),
        client_id=env("HARVEST_CLIENT_ID"),
        client_secret=env("HARVEST_CLIENT_SECRET"),
        note_visibility=env("NOTE_VISIBILITY"),
    )


def card_options(app: Application, meta: Meta) -> CardOptions:
    """Card buttons and overflow menu from SLACK_ACTIONS and CUT (§6.2, J7 table)."""
    mode = (env("SLACK_ACTIONS") or "plugin").strip().lower()
    if mode == "off":
        buttons, menu = False, []
    elif mode == "agent":
        buttons, menu = True, ["more_context", "suggest_questions"]
    else:
        buttons, menu = True, ["more_context", "suggest_questions", "edit_claim"]
    cut = {c.strip() for c in env("CUT").split(",") if c.strip()}
    if "generate" in cut:
        menu = [o for o in menu if o not in ("more_context", "suggest_questions")]
    if "edit" in cut:
        menu = [o for o in menu if o != "edit_claim"]
    return {
        "profile_url": app["profile_url"],
        "annotated_pdf_url": (meta.get("slack") or {}).get("pdf_permalink"),
        "action_buttons": buttons,
        "menu": menu,  # type: ignore[typeddict-item]
    }


def _pushed_at(snapshot: Snapshot, repo: str) -> str | None:
    for name in (repo, repo.lower()):
        got = snapshot.get("GET", f"https://api.github.com/repos/{name}")
        if got and got[0] == 200 and isinstance(got[1], dict) and got[1].get("pushed_at"):
            return got[1]["pushed_at"]
    return None


def _latest_doc(rd: Path, pattern: str, number: int | None) -> Any:
    if number is not None:
        path = rd / pattern.format(number)
        if path.is_file():
            return store.read_json(path)
    return None


def _highest(folder: Path, regex: str) -> int | None:
    best = None
    if folder.is_dir():
        for p in folder.iterdir():
            m = re.match(regex, p.name)
            if m and (best is None or int(m.group(1)) > best):
                best = int(m.group(1))
    return best


def _load_claims(rd: Path, meta: Meta) -> ClaimsDoc | None:
    n = meta["latest"].get("claims_version")
    if n is None:
        n = _highest(rd, r"^claims\.v(\d+)\.json$")
    return _latest_doc(rd, "claims.v{}.json", n)


def _load_verdict(rd: Path, meta: Meta) -> Verdict | None:
    n = meta["latest"].get("verdict_n")
    if n is None:
        n = _highest(rd / "verdicts", r"^verdict_(\d+)\.json$")
    return _latest_doc(rd, "verdicts/verdict_{}.json", n)


def _extraction_failed(rd: Path, meta: Meta) -> bool:
    claims = _load_claims(rd, meta)
    return claims is None or claims["extraction"]["status"] == "failed"


# ── the flow object ───────────────────────────────────────────────────────────

class _Flow:
    """State and stage helpers for one flow on one application. The lock is held by the caller."""

    def __init__(self, app: Application):
        self.app = app
        self.app_id = app["application_id"]
        self.rd = store.run_dir(self.app_id)
        self.mode = env("GITHUB_FETCH_MODE")
        self.card: MessageRef | None = None
        self.claims: ClaimsDoc | None = None
        self.snapshot: Snapshot | None = None
        self.verdict: Verdict | None = None
        self.old_verdict: Verdict | None = None
        self.pdf_path: Path | None = None
        self.pdf_n = 0                  # verdict number of the latest PDF
        self.uploaded_n = 0             # verdict number of the PDF last uploaded to the thread
        self.state_lock = threading.RLock()
        self._wb_guard = threading.Lock()
        self.wb_done = False
        self.timer: threading.Timer | None = None
        self._seen: set[tuple] = set()
        self._adapter_obj = None

    # -- plumbing -------------------------------------------------------------

    @property
    def adapter(self):
        if self._adapter_obj is None:
            self._adapter_obj = _adapter()
        return self._adapter_obj

    def err(self, stage: str, exc: BaseException | str, claim_id: str | None = None) -> None:
        """Record a stage error (first line, ≤ 200 chars) once per flow."""
        error = _first_line(exc)
        key = (stage, claim_id, error)
        if key in self._seen:
            return
        self._seen.add(key)
        entry: dict[str, Any] = {"stage": stage, "error": error, "at": store.now_iso()}
        if claim_id:
            entry["claim_id"] = claim_id
        log.warning("%s: stage %s failed: %s", self.app_id, stage, error)
        try:
            store.update_meta(self.app_id, lambda m: m["stage_errors"].append(entry))
        except Exception:  # noqa: BLE001 - recording must never break a flow
            log.exception("%s: could not record stage error", self.app_id)

    def meta(self) -> Meta:
        return store.read_meta(self.app_id)

    def set_meta(self, fn) -> Meta:
        return store.update_meta(self.app_id, fn)

    def slack(self, fn_name: str, *args, **kwargs):
        """One Slack call; a failure is a stage error and returns None (the run continues)."""
        try:
            with _SLACK_LOCK:
                return getattr(_m("slack"), fn_name)(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            self.err("slack", e)
            return None

    def buttons(self) -> bool:
        return card_options(self.app, self.meta())["action_buttons"]

    def reply(self, text: str) -> MessageRef | None:
        if self.card is None:
            return None
        return self.slack("post_thread", self.card, _text_msg(text))

    # -- card -----------------------------------------------------------------

    def refresh_card(self) -> None:
        if self.card is None:
            return
        try:
            meta = self.meta()
            msg = _m("render").render_card(self.app, self.claims, self.verdict, meta,
                                           card_options(self.app, meta))
        except Exception as e:  # noqa: BLE001
            self.err("render", e)
            return
        self.slack("update_card", self.card, msg)

    def set_card(self, ref: MessageRef) -> None:
        self.card = ref
        permalink = self.slack("get_permalink", ref)

        def apply(m):
            m["slack"].update({"channel": ref["channel"], "card_ts": ref["ts"],
                               "card_permalink": permalink})
        self.set_meta(apply)

    def post_placeholder(self) -> None:
        msg = _m("render").render_placeholder(self.app)
        ref = self.slack("post_card", env("SLACK_CHANNEL_ID"), msg)
        if ref is not None:
            self.set_card(ref)

    def show_unreadable(self) -> None:
        """The "Could not read claims" card: stop here, no verdict, no write-back."""
        try:
            meta = self.meta()
            msg = _m("render").render_card(self.app, None, None, meta, card_options(self.app, meta))
        except Exception as e:  # noqa: BLE001
            self.err("render", e)
            return
        if self.card is not None:
            self.slack("update_card", self.card, msg)
        else:
            ref = self.slack("post_card", env("SLACK_CHANNEL_ID"), msg)
            if ref is not None:
                self.set_card(ref)

    # -- stages ---------------------------------------------------------------

    def stage_resume(self):
        """Step 1. Returns (resume, candidate) or None after a stage error."""
        try:
            if self.app.get("resume") is None:
                raise ResumeError("application has no resume")
            pdf = self.adapter.fetch_resume(self.app, self.rd / "resume.pdf")
            resume = _m("resume").read_resume(pdf)
            store.write_text(self.rd / "resume.txt", resume["text"])
            store.write_json(self.rd / "resume_links.json", resume["links"])
            return resume, _m("resume").link_github(resume, self.app)
        except Exception as e:  # noqa: BLE001
            self.err("resume", e)
            return None

    def reread_resume(self):
        """The saved resume again (for a re-check that retries extraction)."""
        pdf = self.rd / "resume.pdf"
        if not pdf.is_file():
            return self.stage_resume()
        try:
            resume = _m("resume").read_resume(pdf)
            return resume, _m("resume").link_github(resume, self.app)
        except Exception as e:  # noqa: BLE001
            self.err("resume", e)
            return None

    def stage_extract(self, resume, candidate) -> bool:
        """Step 3. False (after a stage error) means: could-not-read card, stop."""
        try:
            doc = _m("extract").extract_claims(resume, candidate, application_id=self.app_id)
            doc["claims_version"] = store.next_claims_version(self.rd)
            store.write_json(self.rd / f"claims.v{doc['claims_version']}.json", doc)
        except Exception as e:  # noqa: BLE001 - LLMError, or a bad doc
            self.err("extract", e)
            return False
        self.claims = doc
        self.set_meta(lambda m: m["latest"].update({"claims_version": doc["claims_version"]}))
        if doc["extraction"]["status"] == "failed":
            self.err("extract", "extraction failed: could not read claims from the resume")
            return False
        return True

    def empty_snapshot(self) -> Snapshot:
        """The snapshot the pipeline writes itself when the fetch fails: every check reads
        "evidence unavailable"."""
        assert self.claims is not None
        login = self.claims["candidate"].get("github_login")
        stamp = int(datetime.now(timezone.utc).timestamp())
        while True:
            snapshot_id = "snap_" + datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            path = self.rd / "snapshots" / snapshot_id
            if not path.exists():
                break
            stamp += 1
        evidence = {
            "schema_version": "1", "snapshot_id": snapshot_id, "application_id": self.app_id,
            "github_login": login, "fetch_mode": self.mode, "created_at": store.now_iso(),
            "responses": [],
            "misses": [cache_key("GET", f"https://api.github.com/users/{login}")] if login else [],
            "decisions": [],
        }
        store.write_json(path / "evidence.json", evidence)
        return Snapshot.load(path)

    def stage_fetch(self) -> Snapshot | None:
        """Step 4. A FetchError continues on an empty snapshot."""
        assert self.claims is not None
        try:
            snap = _m("fetch").fetch_evidence(self.claims, run_dir=self.rd, mode=self.mode)
        except Exception as e:  # noqa: BLE001
            self.err("fetch", e)
            try:
                snap = self.empty_snapshot()
            except Exception as e2:  # noqa: BLE001
                self.err("fetch", e2)
                return None
        self.snapshot = snap
        self.set_meta(lambda m: m["latest"].update({"snapshot_id": snap.snapshot_id}))
        return snap

    # -- verdict and PDF ------------------------------------------------------

    def snapshot_traces(self) -> list[RunTrace]:
        """Every run trace in the snapshot, in claim order."""
        assert self.snapshot is not None and self.claims is not None
        folder = self.snapshot.traces_dir()
        out: list[RunTrace] = []
        for c in self.claims["claims"]:
            p = folder / f"{c['claim_id']}.json"
            if p.is_file():
                out.append(store.read_json(p))
        return out

    def make_verdict(self) -> Verdict | None:
        """Compute and store the next verdict from every trace in the snapshot, then
        regenerate the annotated PDF (not uploaded). Both are published under one lock so a
        write-back never pairs a verdict with an older PDF."""
        assert self.claims is not None and self.snapshot is not None
        with self.state_lock:
            try:
                n = store.next_verdict_n(self.rd)
                v = _m("rubric").compute_verdict(self.claims, self.snapshot, self.snapshot_traces(),
                                                 verdict_n=n, computed_at=store.now_iso())
                store.write_json(self.rd / "verdicts" / f"verdict_{n}.json", v)
            except Exception as e:  # noqa: BLE001
                self.err("verdict", e)
                return None
            self.set_meta(lambda m: m["latest"].update({"verdict_n": n}))
            self.verdict = v
            out = (self.rd / "annotated"
                   / f"{_slug(self.app['candidate_name'])}_resume_verified_{n}.pdf")
            try:
                _m("annotate").annotate_pdf(self.rd / "resume.pdf", self.claims, v, out)
                self.pdf_path, self.pdf_n = out, n
            except Exception as e:  # noqa: BLE001 - AnnotateError
                self.err("annotate", e)
            return v

    def upload_pdf_if_newer(self) -> None:
        """Upload the latest PDF into the thread unless the thread already has it."""
        with self.state_lock:
            if self.card is None or self.pdf_path is None or self.pdf_n <= self.uploaded_n:
                return
            path, n = self.pdf_path, self.pdf_n
            up = self.slack("upload_file", self.card, path, title=path.name)
            if up is None:
                return
            self.uploaded_n = n
            self.set_meta(lambda m: m["slack"].update({"pdf_permalink": up["permalink"]}))
        self.refresh_card()

    def mark_runs(self) -> None:
        """Mark projects queued (or skipped with RUN_CHECK=off, or done when the snapshot
        already holds a trace for the matched repo)."""
        assert self.claims is not None and self.verdict is not None
        runnable = dict(_passing_repos(self.verdict))
        off = env("RUN_CHECK") == "off"
        runs: dict[str, str] = {}
        for c in self.claims["claims"]:
            if c["type"] != "project":
                continue
            cid = c["claim_id"]
            if cid in runnable and self.has_trace(cid, runnable[cid]):
                runs[cid] = "done"
            elif off:
                runs[cid] = "skipped"
            elif cid in runnable:
                runs[cid] = "queued"
        self.set_meta(lambda m: m.update({"runs": runs}))

    def has_trace(self, claim_id: str, repo: str) -> bool:
        assert self.snapshot is not None
        p = self.snapshot.traces_dir() / f"{claim_id}.json"
        if not p.is_file():
            return False
        try:
            return store.read_json(p)["repo"].lower() == repo.lower()
        except Exception:  # noqa: BLE001
            return False

    # -- run checks -----------------------------------------------------------

    def run_checks(self) -> None:
        """Step 6: one project at a time, in claim order, for every queued claim."""
        assert self.claims is not None and self.snapshot is not None
        if self.verdict is None:
            return
        by_id = {c["claim_id"]: c for c in self.claims["claims"]}
        queued = [(cid, repo) for cid, repo in _passing_repos(self.verdict)
                  if self.meta()["runs"].get(cid) == "queued"]
        if not queued:
            return
        self.start_timer()
        for cid, repo in queued:
            claim = by_id[cid]
            buttons = self.buttons()
            self.set_meta(lambda m, cid=cid: m["runs"].__setitem__(cid, "running"))
            line = None
            if self.card is not None:
                line = self.slack("post_thread", self.card, _m("render").render_run_update(
                    self.app_id, claim, "started", None, buttons=buttons))
            try:
                with _SANDBOX_SEM:
                    trace = _m("harness").run_check(self.snapshot, claim, repo, mode=self.mode)
                path = self.snapshot.traces_dir() / f"{cid}.json"
                if not path.is_file():
                    store.write_json(path, trace)
            except Exception as e:  # noqa: BLE001 - SandboxError or anything else: no flag
                self.set_meta(lambda m, cid=cid: m["runs"].__setitem__(cid, "error"))
                self.err("run_check", e, cid)
                if line is not None:
                    self.slack("update_card", line, _m("render").render_run_update(
                        self.app_id, claim, "error", None, buttons=buttons))
                self.refresh_card()
                continue
            self.set_meta(lambda m, cid=cid: m["runs"].__setitem__(cid, "done"))
            if line is not None:
                self.slack("update_card", line, _m("render").render_run_update(
                    self.app_id, claim, "done", trace, buttons=buttons))
            self.make_verdict()
            self.refresh_card()
        self.cancel_timer()

    # -- write-back -----------------------------------------------------------

    def start_timer(self) -> None:
        try:
            cap = float(env("WRITEBACK_CAP_S"))
        except ValueError:
            cap = 600.0
        self.timer = threading.Timer(cap, self.writeback)
        self.timer.daemon = True
        self.timer.start()

    def cancel_timer(self) -> None:
        if self.timer is not None:
            self.timer.cancel()

    def writeback(self) -> None:
        """Step 7, at most once per flow (the guard also serves the cap timer)."""
        with self._wb_guard:
            if self.wb_done:
                return
            self.wb_done = True
        try:
            with self.state_lock:
                verdict, pdf = self.verdict, self.pdf_path
            if verdict is None or pdf is None:
                log.info("%s: write-back skipped (no verdict or no annotated PDF)", self.app_id)
                return
            self.upload_pdf_if_newer()
            meta = self.meta()
            note = _m("render").render_note(self.app, self.claims, verdict,
                                            meta["slack"].get("card_permalink"))
            result = self.adapter.write_back(self.app, note, pdf)
            entry = {"verdict_n": int(verdict["verdict_id"].split("_")[1]),
                     "note_id": result["note_id"], "attachment_id": result["attachment_id"],
                     "at": store.now_iso()}
            self.set_meta(lambda m: m["writebacks"].append(entry))
        except Exception as e:  # noqa: BLE001 - HarvestError; never retried
            self.err("write_back", e)

    def finish(self) -> None:
        """End of a flow: cancel the cap timer, write back (if not yet), upload a newer PDF."""
        self.cancel_timer()
        self.writeback()
        self.upload_pdf_if_newer()

    # -- steps 5 to 7, shared by run, recheck and edit ------------------------

    def verdict_and_runs(self, *, upload_first: bool) -> bool:
        """Step 5 (verdict, queued runs, PDF, card) and step 6. False if no verdict."""
        if self.make_verdict() is None:
            self.show_unreadable()
            return False
        self.mark_runs()
        self.refresh_card()
        if upload_first:
            self.upload_pdf_if_newer()
        self.run_checks()
        return True


# ── carry-forward of run traces ───────────────────────────────────────────────

def _copy_trace(old: Snapshot, new: Snapshot, claim_id: str) -> bool:
    src = old.traces_dir() / f"{claim_id}.json"
    trace = store.read_json(src)
    paths = [c.get("output_path") for c in ((trace.get("agent") or {}).get("commands") or [])]
    paths.append((trace.get("baseline") or {}).get("output_path"))
    for rel in filter(None, paths):
        f = old.path / rel
        if f.is_file():
            store.write_bytes(new.path / rel, f.read_bytes())
    store.write_json(new.traces_dir() / f"{claim_id}.json", trace)
    return True


def _carry_forward(flow: _Flow, old: Snapshot | None, skip: set[str]) -> list[str]:
    """Copy run traces from `old` into the new snapshot for each project whose matched repo has
    the same `pushed_at`. Returns the claim ids carried."""
    if old is None or flow.snapshot is None or flow.claims is None:
        return []
    if not old.traces_dir().is_dir():
        return []
    try:  # matched repos come from a provisional verdict (never stored)
        prov = _m("rubric").compute_verdict(flow.claims, flow.snapshot, [], verdict_n=0,
                                            computed_at=store.now_iso())
        matched = dict(_passing_repos(prov))
    except Exception as e:  # noqa: BLE001
        flow.err("carry_forward", e)
        return []
    carried: list[str] = []
    for p in sorted(old.traces_dir().glob("*.json")):
        cid = p.stem
        if cid in skip or cid not in matched:
            continue
        try:
            trace = store.read_json(p)
            repo = matched[cid]
            if trace["repo"].lower() != repo.lower():
                continue
            before, after = _pushed_at(old, trace["repo"]), _pushed_at(flow.snapshot, repo)
            if before is None or before != after:
                continue
            _copy_trace(old, flow.snapshot, cid)
            carried.append(cid)
        except Exception as e:  # noqa: BLE001
            flow.err("carry_forward", e, cid)
    return carried


# ── run ───────────────────────────────────────────────────────────────────────

def _initial_meta(app: Application) -> Meta:
    return {
        "application_id": app["application_id"], "candidate_id": app["candidate_id"],
        "candidate_name": app["candidate_name"], "job_title": app["job_title"],
        "event_id": app["event_id"], "applied_at": app["applied_at"],
        "latest": {"claims_version": None, "snapshot_id": None, "verdict_n": None},
        "slack": {"channel": env("SLACK_CHANNEL_ID"), "card_ts": None,
                  "card_permalink": None, "pdf_permalink": None},
        "runs": {}, "stage_errors": [], "writebacks": [],
    }


def _run_flow(app: Application) -> None:
    flow = _Flow(app)
    try:
        _, created = store.init_meta(flow.app_id, _initial_meta(app))
        if not created:
            log.info("%s: already processed; run() ignored", flow.app_id)
            return
        # 1. Resume.
        got = flow.stage_resume()
        if got is None:
            flow.show_unreadable()
            return
        resume, candidate = got
        # 2. Placeholder card (a Slack failure is a stage error; the run continues).
        flow.post_placeholder()
        # 3. Claims.
        if not flow.stage_extract(resume, candidate):
            flow.show_unreadable()
            return
        # 4. Evidence.
        if flow.stage_fetch() is None:
            flow.show_unreadable()
            return
        # 5 to 7.
        if not flow.verdict_and_runs(upload_first=True):
            return
        flow.finish()
    except Exception as e:  # noqa: BLE001 - last resort: a flow never raises
        flow.err("pipeline", e)
    finally:
        flow.cancel_timer()


def run(app: Application, *, wait: bool = False) -> None:
    """Run every stage for one application. The per-application lock is taken here, in the
    caller's thread, so a re-check that follows always queues behind this run. With
    ``wait=True`` this returns only after the run checks and write-back; otherwise the flow
    continues in a daemon thread."""
    _APPS[app["application_id"]] = app
    lock = _lock(app["application_id"])
    lock.acquire()

    def work() -> None:
        try:
            _run_flow(app)
        finally:
            lock.release()

    if wait:
        work()
    else:
        threading.Thread(target=work, name=f"run-{app['application_id']}", daemon=True).start()


# ── loading an existing application for later flows ───────────────────────────

def _application(app_id: str, meta: Meta) -> Application:
    if app_id in _APPS:
        return _APPS[app_id]
    try:
        app = _adapter().get_application(app_id)
    except Exception:  # noqa: BLE001
        app = {
            "application_id": app_id, "candidate_id": meta["candidate_id"],
            "candidate_name": meta["candidate_name"], "emails": [], "website_addresses": [],
            "social_media_addresses": [], "job_title": meta["job_title"],
            "applied_at": meta["applied_at"], "profile_url": "", "resume": None,
            "event_id": meta["event_id"],
        }
    _APPS[app_id] = app
    return app


def _load_flow(app_id: str) -> _Flow | None:
    try:
        meta = store.read_meta(app_id)
    except FileNotFoundError:
        log.warning("%s: no such application", app_id)
        return None
    flow = _Flow(_application(app_id, meta))
    flow.claims = _load_claims(flow.rd, meta)
    flow.verdict = flow.old_verdict = _load_verdict(flow.rd, meta)
    sid = meta["latest"].get("snapshot_id")
    if sid and (flow.rd / "snapshots" / sid / "evidence.json").is_file():
        flow.snapshot = Snapshot.load(flow.rd / "snapshots" / sid)
    n = meta["latest"].get("verdict_n")
    flow.uploaded_n = n or 0       # the previous flow ended with its latest PDF in the thread
    if meta["slack"].get("card_ts"):
        flow.card = {"channel": meta["slack"]["channel"], "ts": meta["slack"]["card_ts"]}
    return flow


def _label_changes(flow: _Flow) -> list[str]:
    old = {vc["claim_id"]: vc["label"] for vc in (flow.old_verdict or {"claims": []})["claims"]}
    if flow.verdict is None or flow.claims is None or not old:
        return []
    names = {c["claim_id"]: _claim_name(c) for c in flow.claims["claims"]}
    out = []
    for vc in flow.verdict["claims"]:
        before = old.get(vc["claim_id"])
        if before is not None and before != vc["label"]:
            out.append(f"{names.get(vc['claim_id'], vc['claim_id'])} "
                       f"{LABEL_EMOJI[before]} → {LABEL_EMOJI[vc['label']]}")
    return out


# ── recheck ───────────────────────────────────────────────────────────────────

def recheck(application_id: str, by: str) -> None:
    """Re-check: new snapshot and verdict; traces carry forward when `pushed_at` is unchanged."""
    with _lock(application_id):
        flow = _load_flow(application_id)
        if flow is None:
            return
        try:
            _recheck_flow(flow, by)
        except Exception as e:  # noqa: BLE001
            flow.err("pipeline", e)
        finally:
            flow.cancel_timer()


def _recheck_flow(flow: _Flow, by: str) -> None:
    if flow.card is None:
        flow.post_placeholder()
    flow.reply(f"↻ Re-check started by {by}.")
    # Only a failed extraction is retried; otherwise the claims stay as they are.
    if _extraction_failed(flow.rd, flow.meta()):
        got = flow.reread_resume()
        if got is None or not flow.stage_extract(*got):
            flow.show_unreadable()
            flow.reply("↻ Re-check done: could not read claims from this resume.")
            return
    old_snapshot = flow.snapshot
    if flow.stage_fetch() is None:
        flow.reply("↻ Re-check done: evidence could not be fetched.")
        return
    _carry_forward(flow, old_snapshot, skip=set())
    if not flow.verdict_and_runs(upload_first=False):
        return
    flow.finish()
    changes = _label_changes(flow)
    if flow.old_verdict is None:
        flow.reply("↻ Re-check done: claims read.")
    elif changes:
        flow.reply("↻ Re-check done: " + ", ".join(changes) + ".")
    else:
        flow.reply("↻ Re-check done: no label changes.")


# ── show_transcript ───────────────────────────────────────────────────────────

def _transcript_text(claim: Claim, trace: RunTrace, snapshot_id: str) -> str:
    out = [f"Run transcript: {_claim_name(claim)} ({claim['claim_id']})",
           f"Repo: {trace['repo']} @ {trace['commit_sha']}",
           f"Snapshot: {snapshot_id}   Sandbox: {trace['sandbox_backend']}", ""]
    b = trace["baseline"]
    out += ["== Baseline ==",
            f"step: {b['step']}   command: {b.get('command') or '-'}",
            f"exit code: {b['exit_code']}   duration: {b['duration_s']}s"
            f"   timed out: {b['timed_out']}   failure kind: {b.get('failure_kind') or '-'}"]
    if b.get("output_excerpt"):
        out += ["output:", b["output_excerpt"].rstrip()]
    out.append("")
    agent = trace.get("agent")
    out.append("== Agent ==")
    if agent is None:
        out.append("(no agent run)")
    else:
        out.append(f"model: {agent['model']}   stopped: {agent['stopped']}   "
                   f"limits: {agent['limits']}")
        for i, c in enumerate(agent["commands"], 1):
            out += ["", f"[{i}] $ {c['cmd']}",
                    f"    exit code: {c['exit_code']}   duration: {c['duration_s']}s"
                    f"   timed out: {c['timed_out']}"]
            if c.get("output_excerpt"):
                out += ["    output:"] + ["    " + ln for ln in c["output_excerpt"].rstrip().splitlines()]
    out += ["", "== Judge =="]
    j = trace.get("judge")
    if j is None:
        out.append("(not judged)")
    else:
        out += [f"outcome: {j['outcome']}", f"rationale: {j['rationale']}"]
    return "\n".join(out) + "\n"


def show_transcript(application_id: str, claim_id: str) -> None:
    """Upload the claim's run transcript into the thread, or say there is none yet.
    Read-only on finished files, so it does not wait for the application lock."""
    flow = _load_flow(application_id)
    if flow is None:
        return
    try:
        claim = next((c for c in (flow.claims or {"claims": []})["claims"]
                      if c["claim_id"] == claim_id), None)
        name = _claim_name(claim) if claim else claim_id
        trace_path = (flow.snapshot.traces_dir() / f"{claim_id}.json") if flow.snapshot else None
        if claim is None or trace_path is None or not trace_path.is_file():
            flow.reply(f"No run transcript for {name} yet.")
            return
        text = _transcript_text(claim, store.read_json(trace_path), flow.snapshot.snapshot_id)
        path = flow.rd / "transcripts" / f"{claim_id}-{flow.snapshot.snapshot_id}.txt"
        try:
            store.write_text(path, text)
        except WriteOnceError:
            pass  # an earlier transcript of the same immutable trace
        if flow.card is not None:
            flow.slack("upload_file", flow.card, path, title=f"{name}: run transcript")
    except Exception as e:  # noqa: BLE001
        flow.err("transcript", e)


# ── edit_claim and validate_edit ──────────────────────────────────────────────

_ENUMS = {
    ("project", "role"): ("creator", "co-creator", "maintainer"),
    ("project", "scale"): ("small", "substantial"),
    ("project", "public_signal"): ("link", "open_source_wording", "none"),
    ("skill", "kind"): ("language", "framework_tool"),
    ("oss_contribution", "role"): ("contributor", "maintainer"),
}
_FIELDS = {
    "project": {"repo_url": "text", "repo_name": "text", "language": "text", "role": "enum",
                "min_stars": "int", "start_year": "year", "scale": "enum",
                "public_signal": "enum", "behavior": "text"},
    "skill": {"name": "text", "kind": "enum"},
    "oss_contribution": {"repo": "text", "role": "enum", "min_prs": "int", "description": "text"},
}
_REQUIRED = {("project", "public_signal"), ("skill", "name"), ("skill", "kind"),
             ("oss_contribution", "repo"), ("oss_contribution", "role")}
REPO_FIELDS = {"project": ("repo_url", "repo_name"), "oss_contribution": ("repo",)}
_GITHUB_URL = re.compile(r"^https?://(?:www\.)?github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?/?(?:[?#].*)?$",
                         re.IGNORECASE)
_OWNER_REPO = re.compile(r"^[\w.-]+/[\w.-]+$")


def _normalize(claim: Claim, values: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """Clean the submitted `values` for this claim type: (field -> new value, field -> error)."""
    ctype = claim["type"]
    spec = _FIELDS.get(ctype)
    if spec is None:
        return {}, {"_claim": "This claim can't be edited."}
    clean: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for field, raw in values.items():
        kind = spec.get(field)
        if kind is None:
            errors[field] = "Unknown field."
            continue
        if isinstance(raw, str):
            raw = raw.strip()
        if raw in ("", None, "none_stated", "not stated"):
            raw = None
        if raw is None:
            if (ctype, field) in _REQUIRED:
                errors[field] = "Required."
            else:
                clean[field] = None
            continue
        if kind == "enum":
            if raw not in _ENUMS[(ctype, field)]:
                errors[field] = "Choose one of: " + ", ".join(_ENUMS[(ctype, field)]) + "."
            else:
                clean[field] = raw
        elif kind in ("int", "year"):
            try:
                if isinstance(raw, bool):
                    raise ValueError
                num = int(raw) if not isinstance(raw, float) or raw == int(raw) else None
                if num is None or num < 0:
                    raise ValueError
            except (TypeError, ValueError):
                errors[field] = "Enter a whole number, 0 or more."
                continue
            if kind == "year" and not 1990 <= num <= datetime.now(timezone.utc).year + 1:
                errors[field] = f"Enter a year between 1990 and {datetime.now(timezone.utc).year + 1}."
            else:
                clean[field] = num
        else:  # text
            text = str(raw)
            if field == "repo_url":
                m = _GITHUB_URL.match(text)
                if not m:
                    errors[field] = "Enter a GitHub repository URL."
                    continue
                text = f"https://github.com/{m.group(1)}/{m.group(2)}"
            elif ctype == "oss_contribution" and field == "repo" and not _OWNER_REPO.match(text):
                errors[field] = "Use the form owner/repo."
                continue
            clean[field] = text
    if clean.get("repo_url"):
        clean["public_signal"] = "link"  # a repo link makes the signal a link (H2's rule)
        errors.pop("public_signal", None)
    return clean, errors


def _find_claim_obj(claims: ClaimsDoc | None, claim_id: str) -> Claim | None:
    return next((c for c in (claims or {"claims": []})["claims"] if c["claim_id"] == claim_id), None)


def validate_edit(application_id: str, claim_id: str, values: dict[str, Any]) -> dict[str, str]:
    """Field errors for the Edit modal (block_id -> message); {} when everything is valid."""
    try:
        meta = store.read_meta(application_id)
        claim = _find_claim_obj(_load_claims(store.run_dir(application_id), meta), claim_id)
    except FileNotFoundError:
        return {"_claim": "Application not found."}
    if claim is None:
        return {"_claim": "Claim not found."}
    return _normalize(claim, values)[1]


def edit_claim(application_id: str, claim_id: str, changes: dict[str, Any], by: str) -> None:
    """New claims version, recomputed on the same snapshot (a new one when a repo field
    changed), label change in the thread, new Greenhouse note and PDF."""
    lock = _lock(application_id)
    queued = lock.locked()
    if queued:
        flow0 = _load_flow(application_id)
        if flow0 is not None:
            flow0.reply("Queued: this edit applies after the current checks finish.")
    with lock:
        flow = _load_flow(application_id)
        if flow is None:
            return
        try:
            _edit_flow(flow, claim_id, changes, by)
        except Exception as e:  # noqa: BLE001
            flow.err("edit_claim", e, claim_id)
        finally:
            flow.cancel_timer()


def _edit_flow(flow: _Flow, claim_id: str, changes: dict[str, Any], by: str) -> None:
    claim = _find_claim_obj(flow.claims, claim_id)
    if claim is None or flow.claims is None:
        flow.reply(f"Couldn't edit: claim {claim_id} not found.")
        return
    name = _claim_name(claim)
    clean, errors = _normalize(claim, changes)
    if errors:
        flow.reply(f"Couldn't edit {name}: " + "; ".join(f"{k}: {v}" for k, v in errors.items()))
        return
    current = claim["assertions"]
    changed = {f: v for f, v in clean.items() if current.get(f) != v}
    if not changed:
        flow.reply(f"No changes to {name}.")
        return
    # New claims version: assertions updated, one edit entry per changed field.
    version = store.next_claims_version(flow.rd)
    at = store.now_iso()
    doc = copy.deepcopy(flow.claims)
    doc["claims_version"], doc["created_at"] = version, at
    target = _find_claim_obj(doc, claim_id)
    new_edits = [{"claims_version": version, "claim_id": claim_id, "field": f,
                  "from": current.get(f), "to": v, "by": by, "at": at} for f, v in changed.items()]
    target["assertions"].update(changed)  # type: ignore[union-attr]
    doc["edits"] = list(doc.get("edits", [])) + new_edits
    store.write_json(flow.rd / f"claims.v{version}.json", doc)
    flow.claims = doc
    flow.set_meta(lambda m: m["latest"].update({"claims_version": version}))
    # Same snapshot, unless the edit changes which repo is checked.
    if any(f in REPO_FIELDS.get(claim["type"], ()) for f in changed):
        old_snapshot = flow.snapshot
        if flow.stage_fetch() is None:
            flow.reply(f"{by} edited {name}, but evidence could not be fetched.")
            return
        _carry_forward(flow, old_snapshot, skip={claim_id})
    if flow.snapshot is None:
        flow.stage_fetch()
        if flow.snapshot is None:
            return
    if not flow.verdict_and_runs(upload_first=False):
        return
    old_label = next((vc["label"] for vc in (flow.old_verdict or {"claims": []})["claims"]
                      if vc["claim_id"] == claim_id), "no_evidence")
    new_label = next((vc["label"] for vc in (flow.verdict or {"claims": []})["claims"]
                      if vc["claim_id"] == claim_id), "no_evidence")
    if flow.card is not None:
        flow.slack("post_thread", flow.card, _m("render").render_label_change(
            _find_claim_obj(doc, claim_id), new_edits, old_label, new_label, by))
    flow.finish()


# ── generate ──────────────────────────────────────────────────────────────────

def generate(application_id: str, claim_id: str, kind: str, by: str) -> None:
    """More context / Suggest questions. Takes no lock: it only reads finished files and
    writes a new one."""
    flow = _load_flow(application_id)
    if flow is None:
        return
    try:
        cut = {c.strip() for c in env("CUT").split(",") if c.strip()}
        if "generate" in cut or kind not in GENERATE_KINDS:
            flow.reply("Not available.")
            return
        claim = _find_claim_obj(flow.claims, claim_id)
        vclaim = next((vc for vc in (flow.verdict or {"claims": []})["claims"]
                       if vc["claim_id"] == claim_id), None)
        if claim is None or vclaim is None or flow.verdict is None:
            flow.reply("Not available.")
            return
        try:
            from verifier.core import generate as gen_mod
        except ImportError as e:
            flow.err("generate", f"generate module missing: {e}", claim_id)
            flow.reply("Not available yet.")
            return
        name = _claim_name(claim)
        ref = flow.reply(f"⏳ {by} asked for {GENERATE_KINDS[kind]} on {name}…")
        try:
            gen = gen_mod.generate_text(kind, claim, vclaim, flow.verdict)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            store.write_json(flow.rd / "generated" / f"{claim_id}-{kind}-{stamp}.json", gen)
            msg = _m("render").render_generated(claim, gen)
        except LLMError:
            msg = _text_msg("Couldn't generate this right now.")
        if ref is not None:
            flow.slack("update_card", ref, msg)
    except Exception as e:  # noqa: BLE001
        flow.err("generate", e, claim_id)


# ── read helpers (for the API) ────────────────────────────────────────────────

def get_state(application_id: str) -> dict:
    """`{meta, claims, verdict}`, the latest of each. Raises KeyError for an unknown id."""
    try:
        meta = store.read_meta(application_id)
    except FileNotFoundError:
        raise KeyError(application_id) from None
    rd = store.run_dir(application_id)
    return {"meta": meta, "claims": _load_claims(rd, meta), "verdict": _load_verdict(rd, meta)}


def find_applications(query: str) -> list[str]:
    """An exact application id, else every application whose candidate name contains the
    query (case-insensitive), newest `applied_at` first."""
    query = (query or "").strip()
    if not query:
        return []
    metas = store.list_metas()
    for m in metas:
        if m["application_id"] == query:
            return [query]
    needle = query.lower()
    hits = [m for m in metas if needle in m["candidate_name"].lower()]
    hits.sort(key=lambda m: (m["applied_at"], m["application_id"]), reverse=True)
    return [m["application_id"] for m in hits]


def find_application(query: str) -> str | None:
    found = find_applications(query)
    return found[0] if found else None


def find_claim(application_id: str, query: str) -> str | None:
    """Claim id or text -> claim id: exact id, else a case-insensitive substring of the repo
    name, skill name, contribution repo or source text; first in resume order wins."""
    try:
        claims = get_state(application_id)["claims"]
    except KeyError:
        return None
    query = (query or "").strip()
    if not claims or not query:
        return None
    for c in claims["claims"]:
        if c["claim_id"] == query:
            return c["claim_id"]
    needle = query.lower()
    for c in claims["claims"]:
        a = c["assertions"]
        haystacks = [a.get("repo_name"), a.get("name"), a.get("repo"), c.get("source_text")]
        if any(h and needle in str(h).lower() for h in haystacks):
            return c["claim_id"]
    return None
