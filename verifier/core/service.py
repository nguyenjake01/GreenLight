"""Verifier service (design doc 8.4): the webhook route and health now; J9 adds `/internal/*`.

    uvicorn verifier.core.service:app --port 8200

The webhook route never waits for the pipeline: the adapter's callback starts it in a daemon
thread and the response goes out first.
"""
from __future__ import annotations

import hmac
import logging
import threading

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from verifier.contracts import registry
from verifier.contracts.config import env
from verifier.contracts.errors import SignatureError
from verifier.contracts.types import Application
from verifier.core import pipeline

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

    return app


def _default_app() -> FastAPI:
    load_dotenv()
    return create_app()


app = _default_app()
