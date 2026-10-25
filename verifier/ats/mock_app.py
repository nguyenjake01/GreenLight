"""Mock ATS (design doc J3, J11). Plays an applicant tracking system for the demo.

Fires signed `new_candidate_application` webhooks, serves resume files, implements the three
Harvest v3 routes the verifier uses (token, notes, attachments), and shows one candidate page.
It is labelled "Mock ATS" everywhere and never claims to be a real product.

    uvicorn verifier.ats.mock_app:app --port 8100

State lives in `{RUNS_DIR}/_mock_ats/state.json` (rewritten atomically after every change,
under one lock); stored files in `{RUNS_DIR}/_mock_ats/files/<file_id>/<filename>`.
"""
from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import jinja2
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from markupsafe import Markup, escape
from starlette.concurrency import run_in_threadpool

from verifier.contracts.config import env
from verifier.core import store

HERE = Path(__file__).resolve().parent
FIXTURES = HERE.parents[1] / "fixtures"
TEMPLATES = HERE / "templates"
STATIC = HERE / "static"

VISIBILITIES = ("public", "private", "admin_only")
VISIBILITY_SPELLING = {"public": "publicly_visible", "private": "privately_visible",
                       "admin_only": "admin_only_visible"}
VISIBILITY_LABEL = {"public": "Public", "private": "Private", "admin_only": "Admin only"}
ATTACHMENT_TYPES = ("resume", "cover_letter", "admin_only", "take_home_test", "offer_packet",
                    "offer_letter", "signed_offer_letter", "other")
TOKEN_TTL_S = 3600
_FIXTURE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
MAX_RESUME_BYTES = 5 * 1024 * 1024
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_GITHUB_URL = re.compile(r"^https://github\.com/[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/?$")
_WEBHOOK_STATUS = re.compile(r"^[a-z0-9_]{1,40}$")


def hide_simulate() -> bool:
    """MOCK_ATS_HIDE_SIMULATE=1|true|yes hides the Simulate buttons. Read per request."""
    return os.environ.get("MOCK_ATS_HIDE_SIMULATE", "").strip().lower() in ("1", "true", "yes")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9_]", "", re.sub(r"\s+", "_", text.strip().lower()))


def _secret() -> str:
    """Same fallback as `verifier.demo`, so an unconfigured laptop works end to end."""
    return env("GREENHOUSE_WEBHOOK_SECRET") or "dev-secret"


def _now_ms_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def fixture_names() -> list[str]:
    return sorted(p.stem for p in (FIXTURES / "webhooks").glob("*.json"))


class ValidationFailed(Exception):
    pass


class NotFound(Exception):
    pass


# ── State ─────────────────────────────────────────────────────────────────────

def _empty_state() -> dict:
    return {
        "next_ids": {"application": 1000001, "candidate": 2000001, "note": 1, "attachment": 1,
                     "file": 1},
        "candidates": {},      # id -> greenhouse candidate object, minus attachments
        "applications": {},    # id -> {id, candidate_id, applied_at, status, source, jobs, ...}
        "notes": [],           # oldest first
        "attachments": [],     # oldest first; file_id is None for url-only attachments
        "tokens": {},          # token -> expires_at (epoch seconds)
    }


class MockState:
    """The mock's whole state, behind one lock. Resolves its folder on first use."""

    def __init__(self, root: Path | None = None):
        self._root = Path(root) if root is not None else None
        self.lock = threading.RLock()
        self._data: dict | None = None

    @property
    def root(self) -> Path:
        if self._root is None:
            self._root = Path(env("RUNS_DIR")) / "_mock_ats"
        return self._root

    @property
    def data(self) -> dict:
        with self.lock:
            if self._data is None:
                path = self.root / "state.json"
                loaded = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
                self._data = {**_empty_state(), **loaded}
                self._data["next_ids"] = {**_empty_state()["next_ids"], **self._data["next_ids"]}
            return self._data

    def save(self) -> None:
        """Atomic rewrite (temp file + rename). `store` is write-once, so this can't use it."""
        with self.lock:
            self.root.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".state.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, indent=2, sort_keys=True)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.root / "state.json")
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise

    def next_id(self, kind: str) -> int:
        ids = self.data["next_ids"]
        value = ids[kind]
        ids[kind] = value + 1
        return value

    def snapshot(self) -> dict:
        with self.lock:
            return copy.deepcopy(self.data)

    def file_path(self, file_id: str, filename: str) -> Path:
        return self.root / "files" / file_id / filename


# ── The app ───────────────────────────────────────────────────────────────────

def create_app(*, state_root: Path | None = None,
               webhook_client: httpx.Client | None = None) -> FastAPI:
    """`webhook_client` is optional: tests pass a client whose transport is a MockTransport or
    a TestClient of the verifier service, to intercept the outgoing webhook."""
    state = MockState(state_root)
    app = FastAPI(title="Mock ATS", docs_url="/docs", redoc_url=None)
    app.state.mock = state
    jenv = jinja2.Environment(loader=jinja2.FileSystemLoader(str(TEMPLATES)), autoescape=True)
    jenv.filters["nl2br"] = _nl2br
    jenv.filters["when"] = _when
    jenv.globals["slack_live"] = _slack_live
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    def public_url() -> str:
        return env("ATS_PUBLIC_URL").rstrip("/")

    def file_url(file_id: str, filename: str, *, absolute: bool) -> str:
        rel = f"/files/{file_id}/{quote(filename)}"
        return public_url() + rel if absolute else rel

    # ── error shapes ──────────────────────────────────────────────────────────

    def error(status: int, code: str, message: str = "") -> JSONResponse:
        body = {"error": code}
        if message:
            body["message"] = message
        return JSONResponse(body, status_code=status)

    # ── apply (simulate a new application) ────────────────────────────────────

    def do_apply(fixture: str) -> dict:
        if not _FIXTURE_NAME.match(fixture or ""):
            raise NotFound(f"unknown fixture {fixture!r}")
        path = FIXTURES / "webhooks" / f"{fixture}.json"
        if not path.is_file():
            raise NotFound(f"unknown fixture {fixture!r}")
        body = json.loads(path.read_text(encoding="utf-8"))
        resume_att = next((a for a in (body["payload"]["application"]["candidate"].get(
            "attachments") or []) if a.get("type") == "resume"), None)
        resume_bytes = resume_name = None
        if resume_att is not None:
            resume_name = Path(resume_att["filename"]).name
            src = FIXTURES / "resumes" / resume_name
            if not src.is_file():
                raise NotFound(f"fixture resume {resume_name!r} not found")
            resume_bytes = src.read_bytes()
        return submit_application(body, resume_name, resume_bytes)

    def submit_application(body: dict, resume_name: str | None,
                           resume_bytes: bytes | None) -> dict:
        """Shared by the fixture path and the upload page: allocate ids, store the resume,
        record the application, then sign and post the webhook."""
        application = body["payload"]["application"]
        candidate = application["candidate"]
        attachments = candidate.get("attachments") or []
        resume_att = next((a for a in attachments if a.get("type") == "resume"), None)
        if resume_att is None and resume_bytes is not None:
            resume_att = {"filename": resume_name, "type": "resume", "url": ""}
            candidate["attachments"] = attachments + [resume_att]

        event_id = secrets.token_hex(16)
        with state.lock:
            runs = Path(env("RUNS_DIR"))
            app_id = state.next_id("application")
            while (runs / str(app_id)).exists() or str(app_id) in state.data["applications"]:
                app_id = state.next_id("application")  # never collide with an old run folder
            cand_id = state.next_id("candidate")
            applied_at = _now_ms_iso()
            application["id"], candidate["id"] = app_id, cand_id
            application["applied_at"] = applied_at
            application["url"] = f"{public_url()}/candidates/{cand_id}?application_id={app_id}"

            file_id = None
            if resume_att is not None and resume_bytes is not None:
                file_id = f"f{state.next_id('file')}"
                store.write_bytes(state.file_path(file_id, resume_name), resume_bytes)
                resume_att["url"] = file_url(file_id, resume_name, absolute=True)
                att_id = state.next_id("attachment")
                state.data["attachments"].append({
                    "id": att_id, "application_id": app_id, "candidate_id": cand_id,
                    "filename": resume_name, "type": "resume", "file_id": file_id,
                    "url": None, "visibility": "public", "created_at": store.now_iso()})

            state.data["candidates"][str(cand_id)] = {
                k: v for k, v in candidate.items() if k != "attachments"}
            state.data["applications"][str(app_id)] = {
                "id": app_id, "candidate_id": cand_id, "applied_at": applied_at,
                "status": application.get("status", "active"),
                "source": application.get("source"), "jobs": application.get("jobs") or [],
                "current_stage": application.get("current_stage"), "event_id": event_id,
                "url": application["url"]}
            state.save()

        # ASCII-escaped like Greenhouse; signed over exactly these bytes.
        raw = json.dumps(body).encode("utf-8")
        headers = {"Signature": "sha256 " + hmac.new(_secret().encode(), raw,
                                                     hashlib.sha256).hexdigest(),
                   "Greenhouse-Event-ID": event_id,
                   "Content-Type": "application/json"}
        result: dict[str, Any] = {"application_id": app_id, "candidate_id": cand_id,
                                  "event_id": event_id, "webhook_status": None}
        try:
            client = webhook_client or httpx.Client()
            try:
                resp = client.post(env("VERIFIER_WEBHOOK_URL"), content=raw, headers=headers,
                                   timeout=10.0)
            finally:
                if webhook_client is None:
                    client.close()
            try:
                result["webhook_status"] = resp.json().get("status")
            except (ValueError, AttributeError):
                pass
            if result["webhook_status"] is None:
                result["webhook_status"] = f"http_{resp.status_code}"
            result["webhook_http_status"] = resp.status_code
        except httpx.HTTPError as e:
            result["webhook_status"] = "unreachable"
            result["webhook_error"] = str(e) or e.__class__.__name__
        return result

    @app.post("/_mock/apply")
    async def mock_apply(request: Request):
        ctype = request.headers.get("content-type", "")
        is_form = "form" in ctype
        if is_form:
            fixture = str((await request.form()).get("fixture") or "")
        else:
            try:
                payload = await request.json()
            except ValueError:
                payload = None
            fixture = str(payload.get("fixture") or "") if isinstance(payload, dict) else ""
        try:
            result = await run_in_threadpool(do_apply, fixture)
        except NotFound as e:
            return error(404, "unknown_fixture", str(e))
        if is_form:
            return RedirectResponse("/", status_code=303)
        return result

    # ── candidate-facing careers page: apply with an uploaded resume ──────────

    def careers_job() -> dict:
        """The one open role: job, stage and source taken from the first fixture."""
        names = fixture_names()
        if not names:
            return {"jobs": [], "current_stage": None, "source": None}
        first = json.loads((FIXTURES / "webhooks" / f"{names[0]}.json").read_text("utf-8"))
        app_ = first["payload"]["application"]
        return {"jobs": copy.deepcopy(app_.get("jobs") or []),
                "current_stage": copy.deepcopy(app_.get("current_stage")),
                "source": copy.deepcopy(app_.get("source"))}

    def job_name(job: dict) -> str:
        return job["jobs"][0].get("name") if job["jobs"] else "Open role"

    @app.get("/apply", response_class=HTMLResponse)
    def apply_form():
        return render("apply.html", nav="apply", job_name=job_name(careers_job()),
                      values={}, errors={})

    @app.post("/apply")
    async def apply_submit(request: Request):
        job = careers_job()
        form = await request.form()
        values = {k: str(form.get(k) or "").strip()
                  for k in ("first_name", "last_name", "email", "github_url")}
        errors: dict[str, str] = {}
        for key, label in (("first_name", "first name"), ("last_name", "last name")):
            if not values[key]:
                errors[key] = f"Enter your {label}."
            elif len(values[key]) > 80:
                errors[key] = f"Your {label} is too long (80 characters at most)."
        if not values["email"]:
            errors["email"] = "Enter your email address."
        elif len(values["email"]) > 200 or not _EMAIL.match(values["email"]):
            errors["email"] = "Enter a valid email address, like name@example.com."
        if values["github_url"] and not _GITHUB_URL.match(values["github_url"]):
            errors["github_url"] = "Use a profile link like https://github.com/your-username."
        upload = form.get("resume")
        data = b""
        if upload is None or isinstance(upload, str) or not getattr(upload, "filename", ""):
            errors["resume"] = "Attach your resume as a PDF."
        else:
            data = await upload.read(MAX_RESUME_BYTES + 1)
            if not data:
                errors["resume"] = "That file is empty. Attach your resume as a PDF."
            elif len(data) > MAX_RESUME_BYTES:
                errors["resume"] = "That file is over 5 MB. Attach a PDF under 5 MB."
            elif not data.startswith(b"%PDF"):
                errors["resume"] = "That file is not a PDF. Attach your resume as a PDF."
        if errors:
            return render("apply.html", 422, nav="apply", job_name=job_name(job),
                          values=values, errors=errors)

        websites = []
        if values["github_url"]:
            websites.append({"type": "other", "value": values["github_url"].rstrip("/")})
        filename = f"{_slug(values['first_name']) or 'candidate'}_" \
                   f"{_slug(values['last_name']) or 'applicant'}_resume.pdf"
        body = {"action": "new_candidate_application", "payload": {"application": {
            "candidate": {
                "first_name": values["first_name"], "last_name": values["last_name"],
                "email_addresses": [{"type": "personal", "value": values["email"]}],
                "website_addresses": websites, "phone_numbers": [],
                "social_media_addresses": [], "educations": [], "employments": [],
                "tags": [], "custom_fields": {},
                "attachments": [{"filename": filename, "type": "resume", "url": ""}]},
            "jobs": job["jobs"], "current_stage": job["current_stage"],
            "source": job["source"], "status": "active", "custom_fields": {}}}}
        result = await run_in_threadpool(submit_application, body, filename, data)
        wh = quote(str(result.get("webhook_status") or ""))
        return RedirectResponse(
            f"/apply/thanks?application_id={result['application_id']}&webhook={wh}",
            status_code=303)

    @app.get("/apply/thanks", response_class=HTMLResponse)
    def apply_thanks(application_id: str = "", webhook: str = ""):
        snap = state.snapshot()
        app_rec = snap["applications"].get(application_id)
        if app_rec is None:
            return RedirectResponse("/apply", status_code=303)
        cand = snap["candidates"].get(str(app_rec["candidate_id"]), {})
        jobs = app_rec.get("jobs") or []
        name = f"{cand.get('first_name', '')} {cand.get('last_name', '')}".strip()
        return render("apply.html", nav="apply", done=True, name=name, values={}, errors={},
                      job_name=jobs[0].get("name") if jobs else "Open role",
                      application_id=application_id,
                      webhook_status=webhook if _WEBHOOK_STATUS.match(webhook) else "")

    # ── files ─────────────────────────────────────────────────────────────────

    @app.get("/files/{file_id}/{filename}")
    def get_file(file_id: str, filename: str):
        known = any(a["file_id"] == file_id and a["filename"] == filename
                    for a in state.snapshot()["attachments"])
        path = state.file_path(file_id, filename)
        if not known or not path.is_file():
            return error(404, "not_found")
        media = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        return FileResponse(path, media_type=media)

    # ── Harvest v3 auth ───────────────────────────────────────────────────────

    @app.post("/token")
    async def token(request: Request):
        cid, csecret = _basic_credentials(request.headers.get("authorization", ""))
        ok = (cid is not None
              and hmac.compare_digest(cid.encode(), env("HARVEST_CLIENT_ID").encode())
              and hmac.compare_digest(csecret.encode(), env("HARVEST_CLIENT_SECRET").encode()))
        if not ok:
            return JSONResponse({"error": "invalid_client"}, status_code=401,
                                headers={"WWW-Authenticate": 'Basic realm="mock-ats"'})
        form = await request.form()
        if form.get("grant_type") != "client_credentials":
            return error(400, "unsupported_grant_type",
                         "grant_type must be client_credentials")
        value = secrets.token_urlsafe(32)
        with state.lock:
            now = time.time()
            state.data["tokens"] = {t: exp for t, exp in state.data["tokens"].items() if exp > now}
            state.data["tokens"][value] = now + TOKEN_TTL_S
            state.save()
        return {"token_type": "Bearer", "access_token": value, "expires_in": TOKEN_TTL_S}

    def authorized(request: Request) -> bool:
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            return False
        with state.lock:
            expires = state.data["tokens"].get(auth[7:].strip())
        return expires is not None and expires > time.time()

    # ── Harvest v3: notes ─────────────────────────────────────────────────────

    @app.post("/v3/notes")
    async def create_note(request: Request):
        if not authorized(request):
            return error(401, "unauthorized", "missing or unknown token")
        try:
            payload = await _json_object(request)
            return JSONResponse(await run_in_threadpool(_add_note, state, payload),
                                status_code=201)
        except ValidationFailed as e:
            return error(422, "validation_failed", str(e))
        except NotFound as e:
            return error(404, "not_found", str(e))

    # ── Harvest v3: attachments ───────────────────────────────────────────────

    @app.post("/v3/attachments")
    async def create_attachment(request: Request):
        if not authorized(request):
            return error(401, "unauthorized", "missing or unknown token")
        try:
            payload = await _json_object(request)
            result = await run_in_threadpool(_add_attachment, state, payload, public_url())
            return JSONResponse(result, status_code=201)
        except ValidationFailed as e:
            return error(422, "validation_failed", str(e))
        except NotFound as e:
            return error(404, "not_found", str(e))

    # ── Pages (J11) ───────────────────────────────────────────────────────────

    def render(template: str, status: int = 200, **ctx: Any) -> HTMLResponse:
        return HTMLResponse(jenv.get_template(template).render(**ctx), status_code=status)

    @app.get("/_mock/status")
    def status_endpoint(ids: str = ""):
        wanted: list[str] = []
        for part in ids.split(","):
            part = part.strip()
            if _DIGITS.match(part) and part not in wanted:
                wanted.append(part)
            if len(wanted) >= 50:
                break
        return JSONResponse({i: processing_status(i) for i in wanted},
                            headers={"Cache-Control": "no-store"})

    @app.get("/", response_class=HTMLResponse)
    def index():
        snap = state.snapshot()
        rows = []
        for app_rec in sorted(snap["applications"].values(), key=lambda a: a["id"], reverse=True):
            cand = snap["candidates"].get(str(app_rec["candidate_id"]), {})
            jobs = app_rec.get("jobs") or []
            rows.append({
                "application_id": app_rec["id"], "candidate_id": app_rec["candidate_id"],
                "name": f"{cand.get('first_name', '')} {cand.get('last_name', '')}".strip()
                        or f"Candidate {app_rec['candidate_id']}",
                "job": jobs[0].get("name") if jobs else None,
                "applied_at": app_rec.get("applied_at"),
                "status": processing_status(app_rec["id"]),
            })
        return render("index.html", applications=rows, fixtures=fixture_names(),
                      hide_simulate=hide_simulate())

    @app.get("/candidates/{candidate_id}", response_class=HTMLResponse)
    def candidate_page(candidate_id: str, application_id: str | None = None):
        snap = state.snapshot()
        cand = snap["candidates"].get(candidate_id)
        if cand is None:
            return render("candidate.html", status=404, missing=True, candidate_id=candidate_id)
        apps = sorted((a for a in snap["applications"].values()
                       if str(a["candidate_id"]) == candidate_id),
                      key=lambda a: a["id"], reverse=True)
        if application_id is not None:
            chosen = next((a for a in apps if str(a["id"]) == application_id), None)
            if chosen is None:
                return render("candidate.html", status=404, missing=True,
                              candidate_id=candidate_id)
        else:
            chosen = apps[0] if apps else None

        atts = sorted((a for a in snap["attachments"]
                       if chosen and a["application_id"] == chosen["id"]),
                      key=lambda a: a["id"], reverse=True)
        stored = [a for a in atts if a["file_id"]]
        annotated = next((a for a in stored
                          if a["type"] == "other" and "_resume_verified_" in a["filename"]), None)
        original = next((a for a in stored if a["type"] == "resume"), None) \
            or (stored[-1] if stored else None)
        viewing = annotated or original

        def link(a: dict) -> str:
            return file_url(a["file_id"], a["filename"], absolute=False) if a["file_id"] \
                else (a["url"] or "")

        viewer_url = None
        if viewing is not None:
            inner = quote(file_url(viewing["file_id"], viewing["filename"], absolute=False),
                          safe="")
            viewer_url = f"/static/pdfjs/web/viewer.html?file={inner}#zoom=page-width"

        listing = [a for a in atts if a["type"] == "resume"] + \
                  [a for a in atts if a["type"] != "resume"]
        notes = sorted((n for n in snap["notes"] if str(n["candidate_id"]) == candidate_id),
                       key=lambda n: n["id"], reverse=True)
        jobs = (chosen or {}).get("jobs") or []
        source = (chosen or {}).get("source") or {}
        return render(
            "candidate.html", missing=False, candidate_id=candidate_id,
            name=f"{cand.get('first_name', '')} {cand.get('last_name', '')}".strip(),
            application={
                "id": chosen["id"] if chosen else None,
                "job": jobs[0].get("name") if jobs else None,
                "applied_at": (chosen or {}).get("applied_at"),
                "source": source.get("public_name") if isinstance(source, dict) else None,
            },
            viewer_url=viewer_url,
            proc=processing_status(chosen["id"]) if chosen else None,
            viewing=viewing["filename"] if viewing else None,
            viewing_annotated=annotated is not None,
            attachments=[{"filename": a["filename"], "type": a["type"],
                          "created_at": a["created_at"], "href": link(a)} for a in listing],
            notes=[{"id": n["id"], "body": n["body"], "created_at": n["created_at"],
                    "badge": VISIBILITY_LABEL[n["visibility"]], "visibility": n["visibility"],
                    "author": "Resume Verifier (integration)"} for n in notes],
        )

    return app


# ── Processing status (read-only view of the verifier's run folder) ──────────

STATUS_STEPS = ("received", "reading resume", "finding claims", "checking github",
                "weighing evidence", "running code", "writing back", "done")
STALL_AFTER_S = 15 * 60
_DIGITS = re.compile(r"^[0-9]{1,20}$")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _status_dict(state_: str, idx: int, detail: str | None, progress: float) -> dict:
    steps = []
    for i, label in enumerate(STATUS_STEPS):
        if state_ == "done" or i < idx:
            st = "done"
        elif i == idx:
            st = "current"
        else:
            st = "todo"
        steps.append({"label": label, "status": st})
    return {"state": state_, "step": STATUS_STEPS[idx], "detail": detail,
            "progress": round(max(0.0, min(1.0, progress)), 3), "steps": steps}


def processing_status(application_id: Any) -> dict:
    """Where the verifier is with one application. Reads only RUNS_DIR/<id>/; never writes,
    never raises: a missing, partial or corrupt file just means an earlier stage."""
    try:
        return _processing_status(str(application_id))
    except Exception:  # noqa: BLE001 - a status read must never break a page
        return _status_dict("queued", 0, None, 0.0)


def _processing_status(app_id: str) -> dict:
    queued = _status_dict("queued", 0, None, 0.0)
    if not _DIGITS.match(app_id):
        return queued
    folder = Path(env("RUNS_DIR")) / app_id
    meta_path = folder / "meta.json"
    meta = _read_json(meta_path)
    if not isinstance(meta, dict):
        return queued

    last = len(STATUS_STEPS) - 1
    latest = meta.get("latest") if isinstance(meta.get("latest"), dict) else {}
    runs = meta.get("runs") if isinstance(meta.get("runs"), dict) else {}
    errors = [e for e in (meta.get("stage_errors") or []) if isinstance(e, dict)]
    writebacks = meta.get("writebacks") or []

    claims_files = sorted(folder.glob("claims.v*.json"))
    claims_doc = _read_json(claims_files[-1]) if claims_files else None
    extraction = claims_doc.get("extraction") if isinstance(claims_doc, dict) else None
    extraction_failed = isinstance(extraction, dict) and extraction.get("status") == "failed"
    fatal = any(e.get("stage") in ("resume", "extract") for e in errors)

    has_resume = (folder / "resume.txt").exists()
    has_claims = bool(claims_files)
    has_snapshot = bool(latest.get("snapshot_id"))
    verdicts = folder / "verdicts"
    has_verdict = latest.get("verdict_n") is not None or (
        verdicts.is_dir() and any(verdicts.iterdir()))

    if fatal or extraction_failed:
        idx = 1 if not has_resume else 2
        out = _status_dict("failed", idx, "couldn't read the resume", idx / last)
        return out
    if writebacks:
        return _status_dict("done", last, None, 1.0)

    detail = None
    if not has_resume:
        idx = 1
    elif not has_claims:
        idx = 2
    elif not has_snapshot:
        idx = 3
    elif not has_verdict:
        idx = 4
    else:
        values = [str(v) for v in runs.values()]
        pending = [v for v in values if v in ("queued", "running")]
        if pending:
            idx = 5
            detail = f"{len(values) - len(pending)} of {len(values)} projects"
        else:
            idx = 6
    progress = idx / last
    if idx == 5 and runs:
        progress = (5 + (len(runs) - len(pending)) / len(runs)) / last * 0.98
    out = _status_dict("working", idx, detail, progress)

    try:
        if time.time() - meta_path.stat().st_mtime > STALL_AFTER_S:
            note = "stalled"
            if errors:
                note += f" · {errors[-1].get('stage', 'stage')}: {errors[-1].get('error', '')}"
            out["detail"] = note
    except OSError:
        pass
    return out


# ── Helpers ───────────────────────────────────────────────────────────────────

def _basic_credentials(header: str) -> tuple[str | None, str]:
    if not header.lower().startswith("basic "):
        return None, ""
    try:
        decoded = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None, ""
    cid, sep, csecret = decoded.partition(":")
    return (cid, csecret) if sep else (None, "")


async def _json_object(request: Request) -> dict:
    try:
        payload = await request.json()
    except ValueError as e:
        raise ValidationFailed("request body must be JSON") from e
    if not isinstance(payload, dict):
        raise ValidationFailed("request body must be a JSON object")
    return payload


def _need_int(payload: dict, key: str, *, required: bool = True) -> int | None:
    value = payload.get(key)
    if value is None:
        if required:
            raise ValidationFailed(f"{key} is required")
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationFailed(f"{key} must be an integer")
    return value


def _need_str(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{key} is required and must be a non-empty string")
    return value


def _optional_visibility(payload: dict, *, default: str | None) -> str | None:
    value = payload.get("visibility", default)
    if value is None:
        return None
    if value not in VISIBILITIES:
        raise ValidationFailed(f"visibility must be one of {', '.join(VISIBILITIES)}")
    return value


def _add_note(state: MockState, payload: dict) -> dict:
    candidate_id = _need_int(payload, "candidate_id")
    application_id = _need_int(payload, "application_id", required=False)
    body = _need_str(payload, "body")
    if payload.get("note_type") != "NOTE":
        raise ValidationFailed("note_type is required and must be NOTE")
    visibility = _optional_visibility({"visibility": payload.get("visibility")}, default=None)
    if visibility is None:
        raise ValidationFailed(f"visibility is required: one of {', '.join(VISIBILITIES)}")
    user_id = _need_int(payload, "user_id", required=False)
    with state.lock:
        if str(candidate_id) not in state.data["candidates"]:
            raise NotFound(f"candidate {candidate_id} not found")
        if application_id is not None:
            app_rec = state.data["applications"].get(str(application_id))
            if app_rec is None:
                raise NotFound(f"application {application_id} not found")
            if app_rec["candidate_id"] != candidate_id:
                raise ValidationFailed("application_id does not belong to candidate_id")
        note = {"id": state.next_id("note"), "candidate_id": candidate_id,
                "application_id": application_id, "body": body, "note_type": "NOTE",
                "visibility": visibility, "user_id": user_id, "created_at": store.now_iso()}
        state.data["notes"].append(note)
        state.save()
    return {"id": note["id"], "type": "note", "body": body, "candidate_id": candidate_id,
            "application_id": application_id, "user_id": user_id,
            "visibility": VISIBILITY_SPELLING[visibility], "created_at": note["created_at"]}


def _add_attachment(state: MockState, payload: dict, public_url: str) -> dict:
    application_id = _need_int(payload, "application_id")
    filename = _need_str(payload, "filename")
    if filename != Path(filename).name or filename in (".", "..") or "\\" in filename \
            or "\x00" in filename:
        raise ValidationFailed("filename must be a plain file name")
    kind = _need_str(payload, "type")
    if kind not in ATTACHMENT_TYPES:
        raise ValidationFailed(f"type must be one of {', '.join(ATTACHMENT_TYPES)}")
    has_content, has_url = payload.get("content") is not None, payload.get("url") is not None
    if has_content == has_url:
        raise ValidationFailed("provide exactly one of content or url")
    visibility = _optional_visibility(payload, default="public")
    content = url = None
    if has_content:
        if not isinstance(payload["content"], str):
            raise ValidationFailed("content must be a base64 string")
        try:
            content = base64.b64decode(payload["content"], validate=True)
        except (binascii.Error, ValueError) as e:
            raise ValidationFailed("content is not valid base64") from e
    else:
        url = payload["url"]
        if not isinstance(url, str) or not re.match(r"^https?://", url):
            raise ValidationFailed("url must be an http(s) URL")
    with state.lock:
        app_rec = state.data["applications"].get(str(application_id))
        if app_rec is None:
            raise NotFound(f"application {application_id} not found")
        file_id = None
        if content is not None:
            file_id = f"f{state.next_id('file')}"
            store.write_bytes(state.file_path(file_id, filename), content)
        if kind == "resume":  # real Greenhouse demotes the current resume; so does the mock
            for existing in state.data["attachments"]:
                if existing["application_id"] == application_id and existing["type"] == "resume":
                    existing["type"] = "other"
        att = {"id": state.next_id("attachment"), "application_id": application_id,
               "candidate_id": app_rec["candidate_id"], "filename": filename, "type": kind,
               "file_id": file_id, "url": url, "visibility": visibility,
               "created_at": store.now_iso()}
        state.data["attachments"].append(att)
        state.save()
    out_url = url if url else f"{public_url}/files/{file_id}/{quote(filename)}"
    return {"id": att["id"], "application_id": application_id,
            "candidate_id": att["candidate_id"], "type": kind, "filename": filename,
            "url": out_url, "visibility": visibility}


def _nl2br(text: Any) -> Markup:
    """Escape, then turn newlines into <br>, so a note keeps its line breaks and stays safe."""
    escaped = str(escape("" if text is None else str(text)))
    return Markup(escaped.replace("\r\n", "\n").replace("\n", "<br>\n"))


def _when(iso: str | None) -> str:
    """`2026-10-25T14:02:11Z` -> `Oct 25, 2026 14:02 UTC` (anything unparseable is returned as is)."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    return dt.astimezone(timezone.utc).strftime("%b %d, %Y %H:%M UTC")


def _slack_live() -> bool:
    """True when this stack posts real Slack cards: demo_up.sh puts `slack` in VERIFIER_REAL only
    with SLACK=real. Read per request; the page warns when cards only go to the stub log."""
    return "slack" in [m.strip() for m in os.environ.get("VERIFIER_REAL", "").split(",")]


def _default_app() -> FastAPI:
    load_dotenv()
    return create_app()


app = _default_app()
