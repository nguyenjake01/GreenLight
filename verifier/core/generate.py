"""J14: More context / Suggest questions text (design doc §11 J14, §7.7).

`generate_text` makes one `llm_chat` call that sees only the claim, its verdict claim, and the
verdict's account and context (never raw API bodies or READMEs), checks the reply for words that
would read as a judgment, and returns a `Generated`. It never changes a label. The caller
(`pipeline.generate`) saves the result; it runs only on a click (D6, D8).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from verifier.contracts import registry
from verifier.contracts.types import Claim, Generated, Verdict, VerdictClaim
from verifier.core import store

PROMPTS_DIR = Path(__file__).parent / "prompts"
PROMPT_VERSIONS = {"more_context": "context-v1", "suggest_questions": "questions-v1"}
MAX_TEXT = 2500
MAX_TOKENS = 500
TIMEOUT_S = 60.0
FALLBACK = "Couldn't generate a neutral summary; see the checks on the card."

# Words that would read as a judgment of the candidate. Inflections are listed so that
# "hired" or "rejecting" count, while a repo name like "fakeapp" does not.
BANNED_WORDS = ("fraud", "fraudulent", "fake", "fakes", "faked", "liar", "liars", "lying",
                "dishonest", "dishonesty", "hire", "hires", "hired", "hiring",
                "reject", "rejects", "rejected", "rejecting", "rejection")
_BANNED = re.compile(r"(?<![\w])(" + "|".join(BANNED_WORDS) + r")(?![\w])", re.IGNORECASE)
_URL = re.compile(r"https?://\S+")

_REMINDER = ("Your last reply used wording the rules forbid ({words}). Write it again using "
             "only the facts in the JSON, with neutral wording: no judgment about honesty, no "
             "hiring advice. Do not use those words.")


def _prompt(kind: str) -> str:
    return (PROMPTS_DIR / f"{PROMPT_VERSIONS[kind]}.md").read_text(encoding="utf-8")


def build_input(claim: Claim, vclaim: VerdictClaim, verdict: Verdict) -> dict[str, Any]:
    """The only facts the model sees (§11 J14): claim type, source text and assertions; the
    verdict claim's label, top reason, checks, notes, flags and evidence links; the verdict's
    account and context."""
    return {
        "claim": {"type": claim["type"], "source_text": claim["source_text"],
                  "assertions": claim.get("assertions", {})},
        "verdict_claim": {
            "label": vclaim["label"],
            "top_reason": vclaim["top_reason"],
            "checks": [{k: c[k] for k in ("check", "result", "reason", "facts") if k in c}
                       for c in vclaim["checks"]],
            "notes": vclaim["notes"],
            "flags": vclaim["flags"],
            "evidence_links": vclaim["evidence_links"],
        },
        "account": verdict.get("account"),
        "context": verdict.get("context", []),
    }


def banned_words(text: str, source_text: str) -> list[str]:
    """Banned words in `text` that the claim's own `source_text` doesn't contain. URLs are
    ignored (a repo called "fake-news-detector" is a name, not a judgment)."""
    allowed = {m.group(1).lower() for m in _BANNED.finditer(source_text)}
    found = {m.group(1).lower() for m in _BANNED.finditer(_URL.sub(" ", text))}
    return sorted(found - allowed)


def _cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit)
    return text[: cut if cut >= limit // 2 else limit].rstrip()


def _finish(kind: str, text: str, links: list[str]) -> str:
    """Cap the text at 2,500 characters. A context note always ends with its evidence links, so
    any link the model left out is appended, and the cap keeps room for them."""
    text = text.strip()
    if kind != "more_context":
        return _cap(text, MAX_TEXT)
    missing = [u for u in links if u not in text]
    if not missing:
        return _cap(text, MAX_TEXT)
    tail = "\n" + "\n".join(missing)
    if "Evidence" not in text:
        tail = "\nEvidence:" + tail
    return _cap(text, MAX_TEXT - len(tail)) + tail


def generate_text(kind: str, claim: Claim, vclaim: VerdictClaim, verdict: Verdict) -> Generated:
    """One `llm_chat` call (no schema, temperature 0, reasoning off, 500 tokens, 60 s), one
    retry if the guard trips, then the neutral fallback. Raises `LLMError` from `llm_chat`."""
    if kind not in PROMPT_VERSIONS:
        raise ValueError(f"unknown kind {kind!r}")
    llm = registry.get("llm")
    messages = [
        {"role": "system", "content": _prompt(kind)},
        {"role": "user", "content": json.dumps(build_input(claim, vclaim, verdict),
                                               indent=2, ensure_ascii=False)},
    ]

    def ask(msgs):
        return llm.llm_chat(msgs, None, temperature=0.0, max_tokens=MAX_TOKENS,
                            reasoning=False, timeout_s=TIMEOUT_S)

    resp = ask(messages)
    text = (resp["text"] or "").strip()
    model = resp["model"]
    bad = banned_words(text, claim["source_text"])
    if bad or not text:
        retry = ask(messages + [
            {"role": "assistant", "content": text},
            {"role": "user", "content": _REMINDER.format(words=", ".join(bad) or "none")}])
        text = (retry["text"] or "").strip()
        model = retry["model"]
        if not text or banned_words(text, claim["source_text"]):
            text = FALLBACK
    if text != FALLBACK:
        text = _finish(kind, text, vclaim["evidence_links"])
    return {"claim_id": claim["claim_id"], "kind": kind, "verdict_id": verdict["verdict_id"],
            "model": model, "prompt_version": PROMPT_VERSIONS[kind],
            "created_at": store.now_iso(), "text": text}
