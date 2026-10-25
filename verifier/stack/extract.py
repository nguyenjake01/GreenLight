"""Claim extraction: one `llm_chat` call plus deterministic post-processing (design doc §8.3, H2).

The LLM returns a flat list of claims (`extraction_output` schema). Code orders them, assigns
ids, normalizes GitHub URLs, maps each claim to its typed assertions and validates it. Linking
the candidate's GitHub login is not done here; `candidate` is copied through unchanged.
"""
from __future__ import annotations

import copy
import json
import logging
import re
from pathlib import Path
from typing import Any

import jsonschema

from verifier.contracts import registry
from verifier.contracts.config import env
from verifier.contracts.errors import LLMSchemaError
from verifier.contracts.types import Candidate, ChatMessage, Claim, ClaimsDoc, ResumeText

log = logging.getLogger(__name__)

PROMPT_VERSION = "extract-v1"
TEMPERATURE = 0
MAX_TOKENS = 4000
TIMEOUT_S = 180.0
MAX_SOURCE_CHARS = 300
RETRY_MESSAGE = "Your reply did not match the schema. Reply with JSON only."

_STACK_DIR = Path(__file__).resolve().parent
_SCHEMAS_DIR = _STACK_DIR.parent / "contracts" / "schemas"
_PROMPT_PATH = _STACK_DIR / "prompts" / f"{PROMPT_VERSION}.md"

_GITHUB_REPO = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9._-]+?)"
    r"(?:\.git)?(?:[/?#].*)?$", re.IGNORECASE)
_OWNER_REPO = re.compile(r"^([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9._-]+)$")


def _load_schema(name: str) -> dict:
    return json.loads((_SCHEMAS_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))


_claim_validator: jsonschema.Draft202012Validator | None = None


def _validate_claim(claim: dict) -> bool:
    global _claim_validator
    if _claim_validator is None:
        defs = _load_schema("claims")["$defs"]
        _claim_validator = jsonschema.Draft202012Validator({"$ref": "#/$defs/claim", "$defs": defs})
    return _claim_validator.is_valid(claim)


# ── Prompt ────────────────────────────────────────────────────────────────────

def build_messages(resume: ResumeText) -> list[ChatMessage]:
    links = "\n".join(f"{link['anchor_text'] or '(no anchor text)'} → {link['uri']}"
                      for link in resume["links"]) or "(none)"
    user = f"RESUME TEXT:\n{resume['text']}\n\nHYPERLINKS (anchor text → URI):\n{links}\n"
    return [{"role": "system", "content": _PROMPT_PATH.read_text(encoding="utf-8")},
            {"role": "user", "content": user}]


# ── Post-processing ───────────────────────────────────────────────────────────

def _norm_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def normalize_github_url(url: str | None) -> str | None:
    """`https://github.com/<owner>/<repo>`, or None if `url` isn't a GitHub repo URL."""
    if not url:
        return None
    m = _GITHUB_REPO.match(url.strip())
    if not m:
        return None
    return f"https://github.com/{m.group(1)}/{m.group(2)}"


def _normalize_contrib_repo(value: str | None) -> str | None:
    """`owner/repo` from an explicit owner/repo or a GitHub URL, else None."""
    if not value:
        return None
    value = value.strip()
    url = normalize_github_url(value)
    if url:
        return url.removeprefix("https://github.com/")
    return value if _OWNER_REPO.match(value) else None


def _assertions(flat: dict[str, Any]) -> dict[str, Any]:
    kind = flat["type"]
    if kind == "project":
        repo_url = normalize_github_url(flat.get("repo_url"))
        repo_name = flat.get("repo_name")
        if repo_url and not repo_name:
            repo_name = repo_url.rsplit("/", 1)[1]
        signal = flat.get("public_signal") or "none"
        if repo_url:
            signal = "link"
        elif signal == "link":  # a "link" with no usable GitHub repo URL isn't one
            signal = "none"
        return {"repo_url": repo_url, "repo_name": repo_name, "role": flat.get("role"),
                "language": flat.get("language"), "min_stars": flat.get("min_stars"),
                "start_year": flat.get("start_year"), "scale": flat.get("scale"),
                "public_signal": signal,
                "behavior": flat.get("behavior")}
    if kind == "skill":
        return {"name": flat.get("skill_name"), "kind": flat.get("skill_kind")}
    if kind == "oss_contribution":
        return {"repo": _normalize_contrib_repo(flat.get("contrib_repo")),
                "role": flat.get("contrib_role"), "min_prs": flat.get("min_prs"),
                "description": flat.get("description")}
    return {"category": flat.get("category")}


def postprocess(flat_claims: list[dict[str, Any]], resume_text: str) -> list[Claim]:
    """Flat LLM claims -> ordered, id-assigned, typed, validated claims (design doc §8.3, §7.3)."""
    kept = [dict(c, source_text=c["source_text"].strip()[:MAX_SOURCE_CHARS])
            for c in flat_claims if c.get("source_text", "").strip()]

    haystack = _norm_ws(resume_text)

    def position(c: dict[str, Any]) -> float:
        pos = haystack.find(_norm_ws(c["source_text"]))
        return float("inf") if pos < 0 else pos

    kept.sort(key=position)  # stable: ties and not-found claims keep model order

    claims: list[Claim] = []
    for i, flat in enumerate(kept, start=1):
        claim: dict[str, Any] = {
            "claim_id": f"c{i}", "type": flat["type"], "section": flat.get("section"),
            "source_text": flat["source_text"], "needs_review": False,
            "assertions": _assertions(flat)}
        if not _validate_claim(claim):
            log.warning("claim c%d failed validation; marking needs_review: %r", i, flat)
            claim.update(type="unchecked", needs_review=True, assertions={"category": "other"})
        claims.append(claim)  # type: ignore[arg-type]
    return claims


# ── Entry point ───────────────────────────────────────────────────────────────

def extract_claims(resume: ResumeText, candidate: Candidate, *, application_id: str) -> ClaimsDoc:
    from verifier.core import store  # lazy: store is lane B's module

    llm = registry.get("llm")
    schema = _load_schema("extraction_output")
    messages = build_messages(resume)
    status = "ok"
    model = ""
    flat_claims: list[dict[str, Any]] = []

    def call(msgs: list[ChatMessage]):
        return llm.llm_chat(msgs, schema, temperature=TEMPERATURE, max_tokens=MAX_TOKENS,
                            reasoning=False, timeout_s=TIMEOUT_S)

    try:
        try:
            response = call(messages)
        except LLMSchemaError as exc:
            log.warning("extraction reply did not match the schema; retrying once: %s", exc)
            status = "retried"
            response = call(messages + [{"role": "user", "content": RETRY_MESSAGE}])
        model = response["model"]
        flat_claims = response["json"]["claims"]
    except LLMSchemaError as exc:
        log.error("extraction failed twice: %s", exc)
        status = "failed"
        model = env("LLM_MODEL")

    claims = postprocess(flat_claims, resume["text"]) if status != "failed" else []
    return {
        "schema_version": "1",
        "application_id": application_id,
        "claims_version": 1,
        "created_at": store.now_iso(),
        "candidate": copy.deepcopy(candidate),
        "extraction": {"model": model, "prompt_version": PROMPT_VERSION,
                       "temperature": TEMPERATURE, "resume_sha256": resume["sha256"],
                       "status": status},
        "edits": [],
        "claims": claims,
    }
