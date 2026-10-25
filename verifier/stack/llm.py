"""`llm_chat`: OpenAI-compatible chat client for the local model (design doc §8.3, H2).

One module-level ``httpx.Client`` shared across threads. No retries here; callers decide.
TLS verification is never disabled: a CA bundle from the usual environment variables is used
when present (the sandbox provides one for ``inference.local``), else the system defaults.
"""
from __future__ import annotations

import json
import logging
import os
import re
import ssl
import time
from typing import Any

import httpx
import jsonschema

from verifier.contracts.config import env
from verifier.contracts.errors import LLMError, LLMSchemaError
from verifier.contracts.types import ChatMessage, LLMResponse

log = logging.getLogger(__name__)

_CA_ENV_VARS = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)


def _verify() -> ssl.SSLContext | bool:
    """The CA bundle to verify with: the first usable one named in the environment, else defaults."""
    for var in _CA_ENV_VARS:
        path = os.environ.get(var)
        if path and os.path.isfile(path):
            return ssl.create_default_context(cafile=path)
    return True


_client = httpx.Client(verify=_verify())


def _merge(base: dict[str, Any], extra: dict[str, Any]) -> None:
    """Merge `extra` into `base` in place, recursing into nested objects."""
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value


def _strip_think(text: str) -> str:
    """Drop a `<think>…</think>` block (or a dangling `…</think>` prefix) if one slipped through."""
    text = _THINK_BLOCK.sub("", text)
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1]
    return text.strip()


def llm_chat(messages: list[ChatMessage], schema: dict | None = None, *,
             temperature: float = 0.0, max_tokens: int = 2048,
             reasoning: bool = False, timeout_s: float = 120.0) -> LLMResponse:
    configured_model = env("LLM_MODEL")
    body: dict[str, Any] = {
        "model": configured_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if schema is not None:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": schema["title"], "schema": schema, "strict": True},
        }
    if not reasoning:
        try:
            extra = json.loads(env("LLM_NO_THINK_EXTRA"))
        except json.JSONDecodeError as exc:
            raise LLMError(f"LLM_NO_THINK_EXTRA is not valid JSON: {exc}") from exc
        if isinstance(extra, dict):
            _merge(body, extra)

    url = env("LLM_BASE_URL").rstrip("/") + "/chat/completions"
    start = time.monotonic()
    try:
        resp = _client.post(url, json=body, timeout=timeout_s)
    except httpx.TimeoutException as exc:
        raise LLMError(f"LLM request timed out after {timeout_s}s: {url}") from exc
    except httpx.HTTPError as exc:
        raise LLMError(f"LLM request failed: {exc!r}") from exc
    duration = time.monotonic() - start

    if resp.status_code >= 400:
        raise LLMError(f"LLM HTTP {resp.status_code} from {url}: {resp.text[:300]}")
    try:
        data = resp.json()
        content = data["choices"][0]["message"].get("content")
    except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
        raise LLMError(f"LLM reply is not a chat completion: {resp.text[:300]}") from exc

    text = _strip_think(content or "")
    usage_raw = data.get("usage") or {}
    usage = {"prompt_tokens": int(usage_raw.get("prompt_tokens") or 0),
             "completion_tokens": int(usage_raw.get("completion_tokens") or 0)}
    model = data.get("model") or configured_model
    log.info("llm_chat model=%s duration_s=%.2f prompt_tokens=%d completion_tokens=%d",
             model, duration, usage["prompt_tokens"], usage["completion_tokens"])

    parsed: Any | None = None
    if schema is not None:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMSchemaError(f"LLM reply is not valid JSON: {exc}", text) from exc
        try:
            jsonschema.Draft202012Validator(schema).validate(parsed)
        except jsonschema.ValidationError as exc:
            raise LLMSchemaError(f"LLM reply does not match schema {schema['title']!r}: "
                                 f"{exc.message[:200]}", text) from exc

    return {"text": text, "json": parsed, "model": model,
            "duration_s": round(duration, 3), "usage": usage}
