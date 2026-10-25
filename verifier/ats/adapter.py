"""Greenhouse adapter: signed webhooks in, Harvest v3 write-back out (design doc 8.3, J3).

Real Greenhouse formats, pointed at the mock ATS by default (config.py). Nothing here knows
whether the other end is the mock or the real thing.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal

import httpx

from verifier.contracts.config import env
from verifier.contracts.errors import HarvestError, ResumeError, SignatureError
from verifier.contracts.types import Application, WriteBackResult
from verifier.core import store

log = logging.getLogger("verifier.ats.adapter")

NEW_APPLICATION = "new_candidate_application"
TOKEN_REFRESH_MARGIN_S = 60
_SAFE_EVENT_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


# ── Normalization (design doc 8.2) ────────────────────────────────────────────

def _values(candidate: dict, key: str) -> list[str]:
    return [str(x["value"]) for x in (candidate.get(key) or []) if x.get("value")]


def normalize_application(body: dict, event_id: str) -> Application:
    """Greenhouse `new_candidate_application` body -> the normalized `Application`.

    Raises ValueError when the body has no `payload.application.id`.
    """
    try:
        app = body["payload"]["application"]
        app_id = app["id"]
    except (KeyError, TypeError) as e:
        raise ValueError("webhook body has no payload.application.id") from e
    cand = app.get("candidate") or {}
    resume = next(
        ({"filename": a.get("filename", ""), "url": a.get("url", ""), "type": a["type"]}
         for a in (cand.get("attachments") or []) if a.get("type") == "resume"),
        None,
    )
    jobs = app.get("jobs") or []
    name = f"{cand.get('first_name') or ''} {cand.get('last_name') or ''}".strip()
    return {
        "application_id": str(app_id),
        "candidate_id": str(cand["id"]) if cand.get("id") is not None else "",
        "candidate_name": name,
        "emails": _values(cand, "email_addresses"),
        "website_addresses": _values(cand, "website_addresses"),
        "social_media_addresses": _values(cand, "social_media_addresses"),
        "job_title": (jobs[0].get("name") if jobs else None),
        "applied_at": app.get("applied_at") or "",
        "profile_url": app.get("url") or "",
        "resume": resume,
        "event_id": event_id,
    }


def sign(secret: str, raw_body: bytes) -> str:
    """The `Signature` header value for `raw_body`: `sha256 <hex hmac>`."""
    return "sha256 " + hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


# ── Adapter ───────────────────────────────────────────────────────────────────

class GreenhouseAdapter:
    def __init__(self, *, webhook_secret: str, harvest_auth_url: str, harvest_base_url: str,
                 client_id: str, client_secret: str, note_visibility: str = "private",
                 client: httpx.Client | None = None):
        """`client` is an optional httpx.Client (tests inject a MockTransport or a TestClient)."""
        self._secret = webhook_secret
        self._auth_url = harvest_auth_url
        self._base_url = harvest_base_url.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self._note_visibility = note_visibility
        self._client = client
        self._callbacks: list[Callable[[Application], None]] = []
        self._token: str | None = None
        self._token_expires_at = 0.0  # time.monotonic()
        self._token_lock = threading.Lock()

    # ── webhook in ────────────────────────────────────────────────────────────

    def on_new_application(self, callback: Callable[[Application], None]) -> None:
        self._callbacks.append(callback)

    def _verify(self, raw_body: bytes, lowered: Mapping[str, str]) -> None:
        header = (lowered.get("signature") or "").strip()
        parts = header.split()
        if len(parts) != 2 or parts[0].lower() != "sha256":
            raise SignatureError("missing or malformed Signature header")
        expected = hmac.new(self._secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(parts[1].lower().encode("utf-8"), expected.encode("utf-8")):
            raise SignatureError("Signature does not match the body")

    def handle_webhook(self, raw_body: bytes,
                       headers: Mapping[str, str]) -> Literal["accepted", "duplicate", "ignored"]:
        """Verify, filter, dedupe, normalize, save, notify. Raises SignatureError; ValueError
        for a correctly signed body that isn't a usable JSON webhook."""
        lowered = {str(k).lower(): v for k, v in headers.items()}
        self._verify(raw_body, lowered)  # over the raw bytes, never a re-serialization

        try:
            body = json.loads(raw_body)
        except ValueError as e:
            raise ValueError(f"webhook body is not valid JSON: {e}") from e
        if not isinstance(body, dict) or body.get("action") != NEW_APPLICATION:
            return "ignored"  # includes `ping`

        event_id = lowered.get("greenhouse-event-id") or hashlib.sha256(raw_body).hexdigest()
        app = normalize_application(body, event_id)  # pure: a bad body leaves no marker behind

        if not self._claim_event(event_id):
            return "duplicate"

        rd = store.run_dir(app["application_id"])
        store.write_bytes(rd / "webhook.json", raw_body)
        store.write_json(rd / "webhook_headers.json",
                         {"event_id": event_id, "signature": lowered.get("signature", "")})
        for cb in self._callbacks:
            cb(app)
        return "accepted"

    @staticmethod
    def _claim_event(event_id: str) -> bool:
        """Exclusive-create `{RUNS_DIR}/_events/<event_id>`; False if it already exists.

        `store` has no exclusive-create, but its write is write-once: a second writer's bytes
        differ (random nonce), so it raises WriteOnceError, which means "already claimed".
        """
        from verifier.contracts.errors import WriteOnceError

        name = event_id if _SAFE_EVENT_NAME.match(event_id) and event_id not in (".", "..") \
            else hashlib.sha256(event_id.encode("utf-8")).hexdigest()
        marker = Path(env("RUNS_DIR")) / "_events" / name
        try:
            store.write_bytes(marker, f"{store.now_iso()} {uuid.uuid4().hex}\n".encode("utf-8"))
        except WriteOnceError:
            return False
        return True

    # ── reads ─────────────────────────────────────────────────────────────────

    def get_application(self, application_id: str) -> Application:
        """Rebuilt from the saved webhook (production would call Harvest: known gap)."""
        rd = store.run_dir(application_id)
        body = json.loads((rd / "webhook.json").read_bytes())
        headers = store.read_json(rd / "webhook_headers.json")
        return normalize_application(body, headers["event_id"])

    def fetch_resume(self, app: Application, dest: Path) -> Path:
        ref = app.get("resume")
        if not ref or not ref.get("url"):
            raise ResumeError("application has no resume")
        try:
            resp = self._send("GET", ref["url"], timeout=30.0, follow_redirects=True)
        except httpx.HTTPError as e:
            raise ResumeError(f"resume download failed: {e}") from e
        if resp.status_code != 200:
            raise ResumeError(f"resume download failed: HTTP {resp.status_code}")
        dest = Path(dest)
        store.write_bytes(dest, resp.content)
        return dest

    # ── write-back (Harvest v3) ───────────────────────────────────────────────

    def write_back(self, app: Application, note_text: str,
                   attachment_pdf: Path) -> WriteBackResult:
        """Note on the candidate, then the PDF on the application as type "other" (D20).

        Never retried as a whole (a second note would be a duplicate); only a 401 retries the
        one call that got it, with a fresh token.
        """
        candidate_id, application_id = int(app["candidate_id"]), int(app["application_id"])
        pdf = Path(attachment_pdf)

        note = self._v3("/notes", {
            "candidate_id": candidate_id,
            "application_id": application_id,
            "body": note_text,
            "note_type": "NOTE",
            "visibility": self._note_visibility,
        })
        attachment = self._v3("/attachments", {
            "application_id": application_id,
            "filename": pdf.name,
            "type": "other",
            "content": base64.b64encode(pdf.read_bytes()).decode("ascii"),
        })
        return {
            "note_id": str(note["id"]),
            "attachment_id": str(attachment["id"]),
            "attachment_url": str(attachment.get("url", "")),
        }

    def _v3(self, path: str, payload: dict[str, Any]) -> dict:
        """POST a v3 route with a bearer token; one retry after a 401; 201 or HarvestError."""
        for attempt in (1, 2):
            token = self._get_token(force=attempt == 2)
            try:
                resp = self._send("POST", f"{self._base_url}{path}", json=payload,
                                  headers={"Authorization": f"Bearer {token}"}, timeout=30.0)
            except httpx.HTTPError as e:
                raise HarvestError(f"POST {path} failed: {e}") from e
            if resp.status_code == 401 and attempt == 1:
                self._drop_token(token)
                continue
            break
        if resp.status_code != 201:
            raise HarvestError(f"POST {path} -> HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            return resp.json()
        except ValueError as e:
            raise HarvestError(f"POST {path} -> 201 but the body is not JSON") from e

    def _get_token(self, *, force: bool = False) -> str:
        with self._token_lock:
            if (not force and self._token
                    and time.monotonic() < self._token_expires_at - TOKEN_REFRESH_MARGIN_S):
                return self._token
            try:
                resp = self._send("POST", self._auth_url, timeout=30.0,
                                  auth=(self._client_id, self._client_secret),
                                  data={"grant_type": "client_credentials"})
            except httpx.HTTPError as e:
                raise HarvestError(f"token request failed: {e}") from e
            if resp.status_code != 200:
                raise HarvestError(f"token request -> HTTP {resp.status_code}: {resp.text[:300]}")
            try:
                data = resp.json()
                token, expires_in = data["access_token"], float(data.get("expires_in", 3600))
            except (ValueError, KeyError, TypeError) as e:
                raise HarvestError("token response has no access_token") from e
            self._token = token
            self._token_expires_at = time.monotonic() + expires_in
            return token

    def _drop_token(self, token: str) -> None:
        with self._token_lock:
            if self._token == token:
                self._token = None

    # ── HTTP with logging ─────────────────────────────────────────────────────

    def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """One outgoing call, logged as method, path, status, milliseconds."""
        started = time.monotonic()
        path = httpx.URL(url).path
        status: Any = "error"
        try:
            if self._client is not None:
                resp = self._client.request(method, url, **kwargs)
            else:
                with httpx.Client() as client:
                    resp = client.request(method, url, **kwargs)
            status = resp.status_code
            return resp
        finally:
            log.info("%s %s -> %s (%d ms)", method, path, status,
                     round((time.monotonic() - started) * 1000))
