"""Verifier service (design doc 8.4): the webhook route, health and the `/internal/*` API (J9).

    uvicorn verifier.core.service:app --port 8200

The webhook route never waits for the pipeline: the adapter's callback starts it in a daemon
thread and the response goes out first.
"""
from __future__ import annotations

import hmac
import json
import logging
import re
import threading
from typing import Any
from urllib.parse import parse_qs

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from verifier.contracts import registry
from verifier.contracts.config import env
from verifier.contracts.errors import SignatureError
from verifier.contracts.types import Application
from verifier.core import pipeline, render, store

log = logging.getLogger("verifier.service")


def webhook_secret() -> str:
    """Shared with the mock ATS; falls back like `verifier.demo` so an unconfigured laptop works."""
    return env("GREENHOUSE_WEBHOOK_SECRET") or "dev-secret"


def build_adapter():
    return registry.get("ats").GreenhouseAdapter(
        webhook_secret=webhook_secret(),
        harvest_auth_url=env("HARVEST_AUTH_URL"),
        harvest_base_url=env("HARVEST_BASE_URL"),
        client_id=env("HARVEST_CLIENT_ID"),
        client_secret=env("HARVEST_CLIENT_SECRET"),
        note_visibility=env("NOTE_VISIBILITY"),
    )


def start_pipeline(application: Application) -> threading.Thread:
    """Run `pipeline.run(application)` in a daemon thread; returns at once."""

    def target() -> None:
        try:
            pipeline.run(application)  # looked up at call time, so J7's version is picked up
        except Exception:  # a failed run must never take the service down
            log.exception("pipeline failed for application %s", application["application_id"])

    thread = threading.Thread(target=target, name=f"pipeline-{application['application_id']}",
                              daemon=True)
    thread.start()
    return thread


def require_internal_token(x_verifier_token: str | None = Header(default=None)) -> None:
    """401 `{"error": "unauthorized"}` unless `X-Verifier-Token` equals INTERNAL_TOKEN.

    An unset INTERNAL_TOKEN rejects everything (fail closed): set it in `.env`.
    """
    expected = env("INTERNAL_TOKEN")
    if not expected or not x_verifier_token or not hmac.compare_digest(
            x_verifier_token.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=401, detail="unauthorized")


def create_app() -> FastAPI:
    app = FastAPI(title="Resume Verifier service")
    adapter = build_adapter()
    adapter.on_new_application(start_pipeline)
    app.state.adapter = adapter

    @app.exception_handler(HTTPException)
    async def _http_error(_request: Request, exc: HTTPException):
        if exc.status_code == 401 and exc.detail == "unauthorized":
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                            headers=getattr(exc, "headers", None))

    @app.post("/webhooks/greenhouse")
    async def greenhouse_webhook(request: Request):
        raw = await request.body()  # raw bytes: the signature covers exactly these
        try:
            status = adapter.handle_webhook(raw, request.headers)
        except SignatureError:
            return JSONResponse({"error": "bad_signature"}, status_code=401)
        except ValueError as e:  # correctly signed, but not a usable webhook body
            return JSONResponse({"error": "bad_request", "message": str(e)}, status_code=400)
        return {"status": status}

    @app.get("/internal/health", dependencies=[Depends(require_internal_token)])
    def health():
        return {"status": "ok", "real_modules": registry.real_modules(),
                "fetch_mode": env("GITHUB_FETCH_MODE")}

    _add_internal_routes(app)
    return app


# ── /internal/* (J9, design doc 8.4 and 8.5) ──────────────────────────────────

PREFIX = "openclaw:rv:"
CB_EDIT_CLAIM = PREFIX + "edit_claim"
STALE_CLAIMS = "The claims changed since you opened this; reopen Edit claim."
NO_EDIT = "Edit claim isn't available in this mode."


def _not_found() -> JSONResponse:
    return JSONResponse({"error": "not_found"}, status_code=404)


def _bad_request(message: str) -> JSONResponse:
    return JSONResponse({"error": "bad_request", "message": message}, status_code=400)


def _background(fn, *args) -> threading.Thread:
    """Run `fn(*args)` in a daemon thread. `fn` is looked up by the caller at call time."""

    def target() -> None:
        try:
            fn(*args)
        except Exception:  # background work must never take the service down
            log.exception("background task %s failed", getattr(fn, "__name__", fn))

    thread = threading.Thread(target=target, name=f"api-{getattr(fn, '__name__', 'task')}",
                              daemon=True)
    thread.start()
    return thread


def _parse_body(raw: bytes, content_type: str) -> dict[str, Any]:
    """A Slack payload: JSON, or Slack's form encoding (a `payload` field holding JSON)."""
    text = raw.decode("utf-8")
    if "application/x-www-form-urlencoded" in (content_type or "").lower():
        fields = parse_qs(text, keep_blank_values=True)
        if "payload" not in fields:
            raise ValueError("form body has no `payload` field")
        text = fields["payload"][0]
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("payload must be a JSON object")
    return data


def _user(payload: dict[str, Any]) -> str:
    """Name, else username, else a Slack mention of the id (Slack shows it as the name)."""
    user = payload.get("user") or {}
    name = user.get("name") or user.get("username")
    if name:
        return str(name)
    uid = str(user.get("id") or "")
    if re.fullmatch(r"[UW][A-Z0-9]+", uid):
        return f"<@{uid}>"
    return uid or "someone"


def _action_value(action: dict[str, Any]) -> str:
    """Buttons carry `value`; overflow menus carry `selected_option.value`."""
    if action.get("selected_option"):
        return str(action["selected_option"].get("value") or "")
    return str(action.get("value") or "")


def _reply_in_thread(application_id: str, text: str) -> None:
    """One-line reply under the application's card (read-only use of meta + Slack's post_thread)."""
    try:
        slack_meta = store.read_meta(application_id).get("slack") or {}
    except FileNotFoundError:
        return
    if not slack_meta.get("card_ts") or not slack_meta.get("channel"):
        log.info("%s: no card to reply under: %s", application_id, text)
        return
    msg = {"text": text, "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]}
    try:
        registry.get("slack").post_thread(
            {"channel": slack_meta["channel"], "ts": slack_meta["card_ts"]}, msg)
    except Exception:
        log.exception("%s: thread reply failed", application_id)


def _open_edit_modal(trigger_id: str, application_id: str, claim_id: str) -> None:
    """Open the Edit claim modal inline: the trigger expires 3 s after the click."""
    try:
        state = pipeline.get_state(application_id)
    except KeyError:
        log.warning("edit_claim: unknown application %s", application_id)
        return
    claims = state["claims"]
    claim = next((c for c in (claims or {"claims": []})["claims"] if c["claim_id"] == claim_id),
                 None)
    if claims is None or claim is None:
        log.warning("edit_claim: unknown claim %s/%s", application_id, claim_id)
        return
    try:
        # Real render, not registry.get("render"): the service parses the real modal's
        # callback_id suffix (<app>:<claim>:<version>), which the stub modal lacks.
        view = render.render_edit_modal(application_id, claims["claims_version"], claim)
        registry.get("slack").open_modal(trigger_id, view)
    except Exception:
        log.exception("edit_claim: could not open modal for %s/%s", application_id, claim_id)


def _block_actions(payload: dict[str, Any]) -> None:
    actions = payload.get("actions") or []
    if not actions:
        log.info("block_actions without actions")
        return
    action = actions[0]
    action_id = str(action.get("action_id") or "")
    value = _action_value(action)
    by = _user(payload)

    if action_id == PREFIX + "recheck":
        _background(pipeline.recheck, value, by)
    elif action_id == PREFIX + "claim_menu":
        parts = value.split("|")
        if len(parts) != 3:
            log.warning("claim_menu: bad value %r", value)
            return
        kind, app_id, claim_id = parts
        if kind in ("more_context", "suggest_questions"):
            _background(pipeline.generate, app_id, claim_id, kind, by)
        elif kind == "edit_claim":
            trigger_id = payload.get("trigger_id")
            if trigger_id:
                _open_edit_modal(str(trigger_id), app_id, claim_id)
            else:
                _background(_reply_in_thread, app_id, NO_EDIT)
        else:
            log.info("claim_menu: unknown option %r", kind)
    elif action_id == PREFIX + "transcript":
        parts = value.split("|")
        if len(parts) != 2:
            log.warning("transcript: bad value %r", value)
            return
        _background(pipeline.show_transcript, parts[0], parts[1])
    else:  # open_ats, open_pdf (URL buttons) and anything unknown: ack only
        log.info("action %r: nothing to do", action_id)


def _input_value(entry: dict[str, Any]) -> Any:
    """The value of one modal input (`view.state.values[block]["v"]`)."""
    if "selected_option" in entry:
        option = entry["selected_option"]
        return option.get("value") if option else None
    return entry.get("value")


def _edit_ids(view: dict[str, Any]) -> tuple[str, str, Any] | None:
    """(application_id, claim_id, claims_version) of an Edit claim submission.

    From `private_metadata` when it parses (direct Slack), else from the suffixed callback_id
    `openclaw:rv:edit_claim:<application_id>:<claim_id>:<claims_version>` (OpenClaw plugin mode,
    where the handler never sees private_metadata)."""
    try:
        meta = json.loads(view.get("private_metadata") or "{}")
        return str(meta["application_id"]), str(meta["claim_id"]), meta.get("claims_version")
    except (ValueError, KeyError, TypeError):
        pass
    parts = str(view.get("callback_id") or "")[len(CB_EDIT_CLAIM) + 1:].split(":")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return None
    version: Any = None
    if len(parts) > 2 and parts[2]:
        try:
            version = int(parts[2])
        except ValueError:
            return None
    return parts[0], parts[1], version


def _report_edit_errors(app_id: str, claim_id: str, errors: dict[str, str]) -> None:
    """Thread reply for an invalid edit when the caller could not deliver the modal errors."""
    name = claim_id
    try:
        claims = pipeline.get_state(app_id)["claims"]
        claim = next((c for c in (claims or {"claims": []})["claims"]
                      if c["claim_id"] == claim_id), None)
        if claim is not None:
            name = render._claim_name(claim) or claim_id
    except Exception:  # the name is only decoration
        log.debug("edit error report: no claim name for %s/%s", app_id, claim_id)
    detail = "; ".join(m if k.startswith("_") or m == STALE_CLAIMS else f"{k}: {m}"
                       for k, m in errors.items())
    _reply_in_thread(app_id, f"Couldn't save the edit to {name}: {detail}")


def _view_submission(payload: dict[str, Any], ack_none: bool = False) -> dict[str, Any] | None:
    """`ack_payload` for an Edit claim submission: field errors, or None to close the modal.

    `ack_none`: the caller already acked Slack and cannot deliver an ack body, so invalid or
    stale edits are also reported in the application's thread."""
    view = payload.get("view") or {}
    callback_id = str(view.get("callback_id") or "")
    if callback_id != CB_EDIT_CLAIM and not callback_id.startswith(CB_EDIT_CLAIM + ":"):
        log.info("view_submission %r: nothing to do", view.get("callback_id"))
        return None
    ids = _edit_ids(view)
    if ids is None:
        log.warning("edit_claim submission without usable ids")
        return None
    app_id, claim_id, version = ids
    state = (view.get("state") or {}).get("values") or {}
    values = {block: _input_value(inputs["v"]) for block, inputs in state.items()
              if isinstance(inputs, dict) and isinstance(inputs.get("v"), dict)}
    first_block = next(iter(values), "behavior")

    def errors_ack(errors: dict[str, str]) -> dict[str, Any]:
        if ack_none:
            _background(_report_edit_errors, app_id, claim_id, errors)
        # Slack rejects error keys that are not input blocks of the view.
        fixed = {(k if k in values else first_block): v for k, v in errors.items()}
        return {"response_action": "errors", "errors": fixed}

    try:
        latest = pipeline.get_state(app_id)["claims"]
    except KeyError:
        return errors_ack({first_block: "Application not found."})
    if latest is not None and version is not None and latest["claims_version"] != version:
        return errors_ack({first_block: STALE_CLAIMS})
    errors = pipeline.validate_edit(app_id, claim_id, values)
    if errors:
        return errors_ack(errors)
    _background(pipeline.edit_claim, app_id, claim_id, values, _user(payload))
    return None


def handle_action(payload: dict[str, Any], ack_none: bool = False) -> dict[str, Any]:
    """Dispatch one Slack interaction; always `{"ack_payload": ...}`."""
    ack: dict[str, Any] | None = None
    kind = payload.get("type")
    try:
        if kind == "block_actions":
            _block_actions(payload)
        elif kind == "view_submission":
            ack = _view_submission(payload, ack_none)
        else:
            log.info("interaction type %r ignored", kind)
    except Exception:  # an odd payload must still be acked
        log.exception("internal action failed")
    return {"ack_payload": ack}


def _add_internal_routes(app: FastAPI) -> None:
    auth = [Depends(require_internal_token)]

    @app.post("/internal/actions", dependencies=auth)
    async def actions(request: Request):
        try:
            payload = _parse_body(await request.body(), request.headers.get("content-type", ""))
        except (ValueError, UnicodeDecodeError) as e:
            return _bad_request(str(e))
        # `X-Verifier-Ack: none`: the caller already acked Slack and can't deliver an ack body.
        ack_none = request.headers.get("x-verifier-ack", "").strip().lower() == "none"
        return await run_in_threadpool(handle_action, payload, ack_none)

    @app.get("/internal/verdict", dependencies=auth)
    def verdict(candidate: str = ""):
        matches = pipeline.find_applications(candidate)
        if not matches:
            return _not_found()
        state = pipeline.get_state(matches[0])
        meta = state["meta"]
        others = []
        for app_id in matches[1:]:
            try:
                others.append({"application_id": app_id,
                               "candidate_name": pipeline.get_state(app_id)["meta"]["candidate_name"]})
            except KeyError:
                continue
        return {
            "application_id": meta["application_id"],
            "candidate_name": meta["candidate_name"],
            "job_title": meta["job_title"],
            "card_permalink": (meta.get("slack") or {}).get("card_permalink"),
            "runs": meta.get("runs", {}),
            "claims": (state["claims"] or {}).get("claims", []),
            "verdict": state["verdict"],
            "other_matches": others,
        }

    @app.post("/internal/recheck", dependencies=auth)
    async def recheck(request: Request):
        try:
            body = json.loads((await request.body()) or b"{}")
            candidate, by = str(body["candidate"]), str(body.get("by") or "someone")
        except (ValueError, KeyError, TypeError):
            return _bad_request('expected JSON {"candidate": "...", "by": "..."}')
        app_id = pipeline.find_application(candidate)
        if app_id is None:
            return _not_found()
        status = "queued" if pipeline.is_busy(app_id) else "started"
        _background(pipeline.recheck, app_id, by)
        return JSONResponse({"application_id": app_id, "status": status}, status_code=202)

    @app.get("/internal/explain", dependencies=auth)
    def explain(candidate: str = "", claim: str = ""):
        app_id = pipeline.find_application(candidate)
        if app_id is None:
            return _not_found()
        claim_id = pipeline.find_claim(app_id, claim)
        if claim_id is None:
            return _not_found()
        state = pipeline.get_state(app_id)
        claim_obj = next(c for c in state["claims"]["claims"] if c["claim_id"] == claim_id)
        vclaim = next((vc for vc in (state["verdict"] or {"claims": []})["claims"]
                       if vc["claim_id"] == claim_id), None)
        if vclaim is None:
            return _not_found()
        return {
            "claim_id": claim_id,
            "type": claim_obj["type"],
            "source_text": claim_obj["source_text"],
            "label": vclaim["label"],
            "top_reason": vclaim["top_reason"],
            "checks": vclaim["checks"],
            "notes": vclaim["notes"],
            "flags": vclaim["flags"],
            "evidence_links": vclaim["evidence_links"],
            "run": _run_block(app_id, state["meta"], claim_id),
        }


def _run_block(app_id: str, meta: dict[str, Any], claim_id: str) -> dict[str, Any] | None:
    """The claim's run result from the latest snapshot's trace, or None."""
    snapshot_id = (meta.get("latest") or {}).get("snapshot_id")
    if not snapshot_id:
        return None
    path = store.run_dir(app_id) / "snapshots" / snapshot_id / "run_traces" / f"{claim_id}.json"
    try:
        trace = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    judge = trace.get("judge") or {}
    baseline = trace.get("baseline") or {}
    return {"outcome": judge.get("outcome"), "rationale": judge.get("rationale"),
            "baseline_step": baseline.get("step"), "baseline_exit_code": baseline.get("exit_code")}


def _default_app() -> FastAPI:
    load_dotenv()
    return create_app()


app = _default_app()
