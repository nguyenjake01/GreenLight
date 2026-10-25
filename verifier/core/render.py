"""Slack Block Kit card, thread lines, edit modal and Greenhouse note (design doc §8.3, §8.5, J6).

Everything here is pure: dicts in, dicts or strings out. No LLM, no Slack calls, no I/O, no clock.
Labels and tallies come from the verdict as-is; nothing here computes or changes a label.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Literal

from verifier.contracts.types import (
    Application, CardOptions, Claim, ClaimEdit, ClaimsDoc, Generated, Label, Meta, RunTrace,
    SlackMessage, Verdict, VerdictClaim,
)

# ── Action ids (§8.5) ─────────────────────────────────────────────
A_RECHECK = "openclaw:rv:recheck"
A_OPEN_ATS = "openclaw:rv:open_ats"
A_OPEN_PDF = "openclaw:rv:open_pdf"
A_CLAIM_MENU = "openclaw:rv:claim_menu"
A_TRANSCRIPT = "openclaw:rv:transcript"
CB_EDIT_CLAIM = "openclaw:rv:edit_claim"

EMOJI: dict[str, str] = {"supported": "🟢", "partial": "🟡", "contradicted": "🔴", "no_evidence": "⚪"}
MENU_LABELS: dict[str, str] = {
    "more_context": "More context",
    "suggest_questions": "Suggest questions",
    "edit_claim": "Edit claim",
}
STAGE_LINES: dict[str, str] = {
    "resume": "Couldn't read this resume.",
    "extract": "Couldn't read claims from this resume.",
    "fetch": "GitHub evidence unavailable; affected checks show ⚪.",
    "annotate": "Annotated resume unavailable.",
    "writeback": "Greenhouse note not written.",
}
MAX_BLOCKS = 50
MAX_SECTION = 3000
MAX_HEADER = 150
MAX_REASON = 300
RUBRIC_DEFAULT = "rubric-v1"
GROUPS: tuple[tuple[str, str], ...] = (
    ("projects", "PROJECTS"),
    ("contributions", "CONTRIBUTIONS"),
    ("skills", "SKILLS"),
    ("needs_review", "NEEDS REVIEW"),
    ("unchecked", "NOT CHECKED"),
)


# ── Small helpers ─────────────────────────────────────────────────
def _esc(s: str) -> str:
    """Escape the three characters Slack treats specially in mrkdwn."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _cut(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _plain(text: str, limit: int = 75) -> dict[str, Any]:
    return {"type": "plain_text", "text": _cut(text, limit), "emoji": True}


def _mrkdwn(text: str) -> dict[str, Any]:
    return {"type": "mrkdwn", "text": _cut(text, MAX_SECTION)}


def _section(text: str, block_id: str, accessory: dict | None = None) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "section", "block_id": block_id, "text": _mrkdwn(text)}
    if accessory:
        block["accessory"] = accessory
    return block


def _context(lines: list[str], block_id: str) -> dict[str, Any]:
    return {"type": "context", "block_id": block_id, "elements": [_mrkdwn(t) for t in lines]}


def _header(text: str) -> dict[str, Any]:
    return {"type": "header", "block_id": "header", "text": _plain(text, MAX_HEADER)}


def _context_blocks(lines: list[str]) -> list[dict[str, Any]]:
    """Context blocks hold at most 10 elements; extra lines go into `account_2` (then clipped)."""
    out: list[dict[str, Any]] = []
    if lines:
        out.append(_context(lines[:10], "account"))
    if len(lines) > 10:
        out.append(_context(lines[10:20], "account_2"))
    return out


def _title(app: Application) -> str:
    name = app["candidate_name"]
    job = app.get("job_title")
    return f"New application: {name} · {job}" if job else f"New application: {name}"


def _tally_text(verdict: Verdict) -> str:
    t = verdict.get("tally", {})
    return (f"🟢 {t.get('supported', 0)}   🟡 {t.get('partial', 0)}   "
            f"🔴 {t.get('contradicted', 0)}   ⚪ {t.get('no_evidence', 0)}")


def _fallback_text(app: Application, verdict: Verdict) -> str:
    t = verdict.get("tally", {})
    return (f"New application: {app['candidate_name']}: 🟢 {t.get('supported', 0)} "
            f"🟡 {t.get('partial', 0)} 🔴 {t.get('contradicted', 0)} ⚪ {t.get('no_evidence', 0)}")


def _month_year(iso_date: str | None) -> str | None:
    if not iso_date:
        return None
    try:
        return datetime.strptime(iso_date[:10], "%Y-%m-%d").strftime("%b %Y")
    except ValueError:
        return None


def _matched_repo_name(vclaim: VerdictClaim | None) -> str | None:
    """The repo name from the verdict claim's `repo` check facts ("owner/repo" → "repo")."""
    for chk in (vclaim or {}).get("checks", []):
        if chk.get("check") == "repo":
            repo = (chk.get("facts") or {}).get("repo")
            if isinstance(repo, str) and repo:
                return repo.split("/", 1)[-1]
    return None


def _claim_name(claim: Claim | None, vclaim: VerdictClaim | None = None, fallback: str = "") -> str:
    """Project: repo_name, else matched repo, else first 40 chars of source_text. Skill: name.
    Contribution: repo."""
    if claim is None:
        return _matched_repo_name(vclaim) or fallback
    a = claim.get("assertions", {})
    t = claim.get("type")
    if t == "skill":
        return a.get("name") or _cut(claim["source_text"], 40)
    if t == "oss_contribution":
        return a.get("repo") or _cut(claim["source_text"], 40)
    if t == "project":
        return a.get("repo_name") or _matched_repo_name(vclaim) or claim["source_text"][:40]
    return claim["source_text"][:40]


def _pretty_category(cat: str) -> str:
    return cat.replace("_", " ").lower()


# ── Card ──────────────────────────────────────────────────────────
def _stage_error_lines(meta: Meta, claims_by_id: dict[str, Claim],
                       skip_stages: tuple[str, ...] = ()) -> list[str]:
    lines: list[str] = []
    for err in meta.get("stage_errors", []) or []:
        stage = err.get("stage")
        if stage in skip_stages:
            continue
        if stage == "run_check":
            cid = err.get("claim_id")
            claim = claims_by_id.get(cid) if cid else None
            line = f"Functional run check unavailable for {_esc(_claim_name(claim, None, cid or 'a project'))}."
        else:
            line = STAGE_LINES.get(stage or "")
        if line and line not in lines:
            lines.append(line)
    return lines


def _account_line(verdict: Verdict) -> str:
    acct = verdict.get("account")
    if not acct:
        return "No GitHub account found on the resume or application."
    parts = [f"GitHub @{acct['login']}"]
    n = acct.get("public_repos")
    if n is not None:
        parts.append(f"{n} public repo{'' if n == 1 else 's'}")
    since = _month_year(acct.get("created_at"))
    if since:
        parts.append(f"account since {since}")
    return " · ".join(parts)


def _actions_block(app: Application, opts: CardOptions, *, recheck: bool = True,
                   pdf: bool = True) -> dict[str, Any] | None:
    elements: list[dict[str, Any]] = []
    if recheck and opts.get("action_buttons"):
        elements.append({"type": "button", "action_id": A_RECHECK, "text": _plain("Re-check now"),
                         "value": app["application_id"]})
    if opts.get("profile_url"):
        elements.append({"type": "button", "action_id": A_OPEN_ATS, "text": _plain("Open in Greenhouse"),
                         "url": opts["profile_url"]})
    if pdf and opts.get("annotated_pdf_url"):
        elements.append({"type": "button", "action_id": A_OPEN_PDF, "text": _plain("Open annotated resume"),
                         "url": opts["annotated_pdf_url"]})
    if not elements:
        return None
    return {"type": "actions", "block_id": "actions", "elements": elements}


def _menu_accessory(app_id: str, claim_id: str, opts: CardOptions) -> dict[str, Any] | None:
    kinds = [k for k in opts.get("menu", []) if k in MENU_LABELS]
    if not opts.get("action_buttons") or not kinds:
        return None
    kinds = list(dict.fromkeys(kinds))[:5]
    if len(kinds) == 1:     # Slack's overflow needs 2-5 options; one option becomes a small button
        k = kinds[0]
        return {"type": "button", "action_id": A_CLAIM_MENU, "text": _plain(MENU_LABELS[k]),
                "value": f"{k}|{app_id}|{claim_id}"}
    return {"type": "overflow", "action_id": A_CLAIM_MENU,
            "options": [{"text": _plain(MENU_LABELS[k]), "value": f"{k}|{app_id}|{claim_id}"}
                        for k in kinds]}


class _Row:
    """One claim row, before block building."""
    __slots__ = ("block_id", "text", "notes", "menu_claim_id")

    def __init__(self, block_id: str, text: str, notes: list[str] | None = None,
                 menu_claim_id: str | None = None):
        self.block_id, self.text, self.notes, self.menu_claim_id = block_id, text, notes or [], menu_claim_id


def _plan_groups(claims: ClaimsDoc | None, verdict: Verdict) -> list[tuple[str, str, list[_Row]]]:
    """Group rows in order PROJECTS, CONTRIBUTIONS, SKILLS, NEEDS REVIEW, NOT CHECKED.

    Returns (group key, title, rows) for non-empty groups. The SKILLS group's trailing
    framework/tool row has no menu_claim_id."""
    claims_by_id = {c["claim_id"]: c for c in (claims or {}).get("claims", [])}
    buckets: dict[str, list[_Row]] = {k: [] for k, _ in GROUPS}
    frameworks: list[str] = []
    unchecked_cats: list[str] = []
    n_unchecked = 0
    for vc in verdict.get("claims", []):
        claim = claims_by_id.get(vc["claim_id"])
        ctype = (claim or vc).get("type", vc["type"])
        name = _esc(_claim_name(claim, vc, vc["claim_id"]))
        if ctype == "unchecked":
            if claim and claim.get("needs_review"):
                buckets["needs_review"].append(_Row(
                    f"claim_{vc['claim_id']}",
                    f"⚪ {_esc(_cut(claim['source_text'], 60))}: couldn't classify this claim; "
                    f"check the resume."))
            else:
                n_unchecked += 1
                cat = _pretty_category((claim or {}).get("assertions", {}).get("category") or "other")
                if cat not in unchecked_cats:
                    unchecked_cats.append(cat)
            continue
        if ctype == "skill" and claim and claim["assertions"].get("kind") == "framework_tool":
            frameworks.append(name)
            continue
        emoji = EMOJI.get(vc["label"], "⚪")
        warn = "⚠️ " if vc.get("flags") else ""
        reason = _esc(_cut(vc.get("top_reason", ""), MAX_REASON))
        row = _Row(f"claim_{vc['claim_id']}", f"{warn}{emoji} *{name}*: {reason}",
                   [_esc(n) for n in vc.get("notes", [])], vc["claim_id"])
        key = {"project": "projects", "oss_contribution": "contributions", "skill": "skills"}.get(ctype)
        if key:
            buckets[key].append(row)
    if frameworks:
        buckets["skills"].append(_Row(
            "frameworks", f"⚪ {', '.join(frameworks)}: frameworks and tools aren't checked in v1."))
    if n_unchecked:
        cats = ", ".join(unchecked_cats)
        buckets["unchecked"].append(_Row(
            "unchecked", f"⚪ {cats[:1].upper()}{cats[1:]}. Not verifiable from public code."))
    out = []
    for key, title in GROUPS:
        if buckets[key]:
            if key == "unchecked":
                title = f"NOT CHECKED ({n_unchecked})"
            out.append((key, title, buckets[key]))
    return out


def _build_claim_blocks(app_id: str, groups: list[tuple[str, str, list[_Row]]], opts: CardOptions,
                        *, fold_notes: bool, collapse_skills: bool,
                        keep: int | None, verdict: Verdict, claims: ClaimsDoc | None) -> list[dict]:
    blocks: list[dict] = []
    hidden = 0
    for key, title, rows in groups:
        if collapse_skills and key == "skills":
            blocks.append(_section(f"*{title}*", f"group_{key}"))
            claims_by_id = {c["claim_id"]: c for c in (claims or {}).get("claims", [])}
            parts = []
            vmap = {vc["claim_id"]: vc for vc in verdict["claims"]}
            for r in rows:
                if r.menu_claim_id:
                    vc = vmap[r.menu_claim_id]
                    nm = _esc(_claim_name(claims_by_id.get(r.menu_claim_id), vc, r.menu_claim_id))
                    parts.append(f"{EMOJI.get(vc['label'], '⚪')} {nm}")
                else:
                    parts.append(r.text.split(":")[0])
            blocks.append(_section(" · ".join(parts), "skills_collapsed"))
            continue
        shown = rows if keep is None else rows[:keep]
        hidden += len(rows) - len(shown)
        if not shown:
            continue
        blocks.append(_section(f"*{title}*", f"group_{key}"))
        for r in shown:
            text = r.text
            if fold_notes and r.notes:
                text += "\n↳ " + " · ".join(r.notes)
            accessory = _menu_accessory(app_id, r.menu_claim_id, opts) if r.menu_claim_id else None
            blocks.append(_section(text, r.block_id, accessory))
            if r.notes and not fold_notes:
                blocks.append(_context(["↳ " + " · ".join(r.notes)], f"notes_{r.block_id}"))
    if hidden:
        blocks.append(_context([f"+ {hidden} more claims in the annotated resume"], "more"))
    return blocks


def render_placeholder(app: Application) -> SlackMessage:
    return {
        "text": f"New application: {app['candidate_name']}: verifying…",
        "blocks": [_header(_title(app)),
                   _context(["Verifying public evidence… this takes about a minute."], "status")],
    }


def _footer(rubric_version: str, snapshot_id: str | None) -> dict[str, Any]:
    parts = ["Evidence summary, not a hiring decision", rubric_version]
    if snapshot_id:
        parts.append(snapshot_id)
    return _context([" · ".join(parts)], "footer")


def _render_unreadable(app: Application, claims: ClaimsDoc | None, meta: Meta,
                       opts: CardOptions) -> SlackMessage:
    lines = ["Couldn't read claims from this resume. Re-check retries reading it."]
    claims_by_id = {c["claim_id"]: c for c in (claims or {}).get("claims", [])}
    lines += _stage_error_lines(meta, claims_by_id, skip_stages=("extract",))
    blocks = [_header(_title(app)), *_context_blocks(lines)]
    actions = _actions_block(app, opts, pdf=False)
    if actions:
        blocks.append(actions)
    blocks.append(_footer(RUBRIC_DEFAULT, None))
    return {"text": f"New application: {app['candidate_name']}: couldn't read claims", "blocks": blocks}


def render_card(app: Application, claims: ClaimsDoc | None, verdict: Verdict | None,
                meta: Meta, opts: CardOptions) -> SlackMessage:
    if verdict is None:
        return _render_unreadable(app, claims, meta, opts)

    claims_by_id = {c["claim_id"]: c for c in (claims or {}).get("claims", [])}
    ctx = [_account_line(verdict)]
    ctx += [_esc(c) for c in verdict.get("context", []) if not c.startswith("Account created")]
    ctx += _stage_error_lines(meta, claims_by_id)
    runs = meta.get("runs", {}) or {}
    if any(s in ("queued", "running") for s in runs.values()):
        done = sum(1 for s in runs.values() if s in ("done", "skipped", "error"))
        ctx.append(f"Run checks: {done} of {len(runs)} done")

    head = [_header(_title(app)), *_context_blocks(ctx), _section(_tally_text(verdict), "tally")]
    tail: list[dict] = []
    if not opts.get("action_buttons"):
        tail.append(_context(["Mention me to re-check or ask about a claim."], "hint"))
    actions = _actions_block(app, opts)
    if actions:
        tail.append(actions)
    tail.append(_footer(verdict.get("rubric_version", RUBRIC_DEFAULT),
                        (verdict.get("inputs") or {}).get("snapshot_id")))

    groups = _plan_groups(claims, verdict)
    app_id = app["application_id"]

    def build(fold: bool, collapse: bool, keep: int | None) -> list[dict]:
        mid = _build_claim_blocks(app_id, groups, opts, fold_notes=fold, collapse_skills=collapse,
                                  keep=keep, verdict=verdict, claims=claims)
        return head + mid + tail

    blocks = build(False, False, None)
    if len(blocks) > MAX_BLOCKS:
        blocks = build(True, False, None)
    if len(blocks) > MAX_BLOCKS:
        blocks = build(True, True, None)
    if len(blocks) > MAX_BLOCKS:
        biggest = max((len(rows) for _, _, rows in groups), default=0)
        for keep in range(biggest - 1, -1, -1):
            blocks = build(True, True, keep)
            if len(blocks) <= MAX_BLOCKS:
                break
    return {"text": _fallback_text(app, verdict), "blocks": blocks[:MAX_BLOCKS]}


# ── Run lines (thread) ────────────────────────────────────────────
def _baseline_text(b: dict[str, Any]) -> str:
    if b.get("timed_out"):
        return "timed out"
    fk = b.get("failure_kind")
    if fk == "deps_missing":
        return "missing dependencies"
    if fk == "needs_network":
        return "needs network"
    step = b.get("step")
    ok = b.get("exit_code") == 0
    secs = f"{b.get('duration_s', 0):.1f}s"
    if step == "tests":
        return f"tests passed ({secs})" if ok else "tests failed"
    if step == "build":
        return f"built ({secs})" if ok else "build failed"
    if step == "syntax":
        return "syntax OK" if ok else "syntax errors"
    return "nothing to build"


_JUDGE_TEXT = {
    "works_as_claimed": "works as claimed",
    "runs_but_mismatch": "output didn't match the claim",
    "failed_to_run": "failed to run",
    "could_not_attempt": "couldn't run here",
}


def render_run_update(application_id: str, claim: Claim,
                      status: Literal["started", "done", "skipped", "error"],
                      trace: RunTrace | None, *, buttons: bool = True) -> SlackMessage:
    name = (claim.get("assertions", {}).get("repo_name")
            or (trace["repo"].split("/", 1)[-1] if trace else None)
            or claim["source_text"][:40])
    name = _esc(name)
    block_id = f"run_{claim['claim_id']}"
    accessory = None
    if status == "started":
        text = f"▶ {name}: running…"
    elif status == "skipped":
        text = f"▶ {name}: skipped"
    elif status == "error" or trace is None:
        text = f"▶ {name}: run check unavailable"
    else:
        parts = [_baseline_text(trace["baseline"])]
        agent = trace.get("agent")
        if agent is not None:
            n = len(agent.get("commands", []))
            parts.append(f"ran {n} command{'' if n == 1 else 's'}")
        judge = trace.get("judge")
        if judge is not None:
            parts.append(_JUDGE_TEXT.get(judge["outcome"], judge["outcome"]))
        text = f"▶ {name}: " + " → ".join(parts)
        if buttons:
            accessory = {"type": "button", "action_id": A_TRANSCRIPT, "text": _plain("Show transcript"),
                         "value": f"{application_id}|{claim['claim_id']}"}
    return {"text": text, "blocks": [_section(text, block_id, accessory)]}


# ── Label change, generated text, edit modal ──────────────────────
def _fmt_val(v: Any) -> str:
    if v is None:
        return "not stated"
    return _cut(" ".join(str(v).split()), 80)


def render_label_change(claim: Claim, edits: list[ClaimEdit], old: Label, new: Label,
                        by: str) -> SlackMessage:
    mine = [e for e in edits if e.get("claim_id") == claim["claim_id"]] or list(edits)
    name = _claim_name(claim)
    changes = "; ".join(f"{e['field']} {_fmt_val(e.get('from'))} → {_fmt_val(e.get('to'))}"
                        for e in mine)
    if old == new:
        verdict_part = f"Label unchanged ({EMOJI.get(new, '⚪')})."
    else:
        verdict_part = f"{EMOJI.get(old, '⚪')} → {EMOJI.get(new, '⚪')}."
    head = f"{by} edited {name}: {changes}." if changes else f"{by} edited {name}."
    text = f"{head} {verdict_part}"
    # A Slack mention ("<@U123>") from service._user stays unescaped so Slack shows the name.
    mention = re.fullmatch(r"<@[UW][A-Z0-9]+>", by)
    body = by + _esc(text[len(by):]) if mention else _esc(text)
    return {"text": text, "blocks": [_section(body, f"label_{claim['claim_id']}")]}


def _slack_format(text: str) -> str:
    """Markdown to Slack mrkdwn: "- " bullets become "• ", `**bold**` becomes `*bold*`,
    `#` headings become bold lines."""
    out = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("- ", "* ")):
            line = line[: len(line) - len(stripped)] + "• " + stripped[2:]
        elif stripped.startswith("#"):
            heading = stripped.lstrip("#")
            if heading.startswith(" ") and heading.strip():
                line = f"**{heading.strip()}**"
        out.append(line)
    return "\n".join(out).replace("**", "*")


def _split_text(body: str, limit: int) -> list[str]:
    """Pieces of at most `limit` characters, cut at a line break when one is available."""
    chunks: list[str] = []
    while len(body) > limit:
        cut = body.rfind("\n", 0, limit + 1)
        if cut < limit // 2:
            cut = limit
        chunks.append(body[:cut].rstrip("\n"))
        body = body[cut:].lstrip("\n")
    chunks.append(body)
    return chunks


def render_generated(claim: Claim, gen: Generated) -> SlackMessage:
    kind_title = "More context" if gen["kind"] == "more_context" else "Suggested questions"
    name = _claim_name(claim)
    body = _esc(_slack_format(gen["text"]))
    title = f"{kind_title}: {name}"
    chunks = _split_text(body, 2900)
    blocks = [_section(f"*{_esc(title)}*\n{chunks[0]}", f"gen_{claim['claim_id']}_{gen['kind']}")]
    for i, chunk in enumerate(chunks[1:], start=2):
        blocks.append(_section(chunk, f"gen_{claim['claim_id']}_{gen['kind']}_{i}"))
    blocks.append(_context([f"Generated locally by {gen['model']} · {gen['prompt_version']} · "
                            f"based on {gen['verdict_id']}"], "gen_footer"))
    return {"text": title, "blocks": blocks}


# Edit modal field layout per claim type (§8.5, J12): (field, label, kind, required, options).
# kinds: text, multiline, number, select. A select that is not required gets a "not stated" option.
NOT_STATED = "none_stated"
_MODAL_FIELDS: dict[str, tuple[tuple[str, str, str, bool, tuple[str, ...]], ...]] = {
    "project": (
        ("repo_url", "Repository URL", "text", False, ()),
        ("repo_name", "Repository name", "text", False, ()),
        ("language", "Language", "text", False, ()),
        ("role", "Role", "select", False, ("creator", "co-creator", "maintainer")),
        ("min_stars", "Minimum stars", "number", False, ()),
        ("start_year", "Start year", "number", False, ()),
        ("scale", "Scale", "select", False, ("small", "substantial")),
        ("public_signal", "Public signal", "select", True, ("link", "open_source_wording", "none")),
        ("behavior", "Behavior", "multiline", False, ()),
    ),
    "skill": (
        ("name", "Skill", "text", True, ()),
        ("kind", "Kind", "select", True, ("language", "framework_tool")),
    ),
    "oss_contribution": (
        ("repo", "Repository (owner/repo)", "text", True, ()),
        ("role", "Role", "select", True, ("contributor", "maintainer")),
        ("min_prs", "Minimum merged PRs", "number", False, ()),
        ("description", "Description", "multiline", False, ()),
    ),
}
_PLACEHOLDERS = {"repo_url": "https://github.com/owner/repo", "repo": "owner/repo"}


def _option(value: str, text: str | None = None) -> dict[str, Any]:
    return {"text": _plain(text or value), "value": value}


def _modal_input(field: str, label: str, kind: str, required: bool, options: tuple[str, ...],
                 current: Any) -> dict[str, Any]:
    if kind == "select":
        opts = [_option(o) for o in options]
        initial = None
        if current in options:
            initial = _option(current)
        elif current is None and not required:
            initial = _option(NOT_STATED, "not stated")
        if not required:
            opts.append(_option(NOT_STATED, "not stated"))
        element: dict[str, Any] = {"type": "static_select", "action_id": "v", "options": opts}
        if initial is not None:
            element["initial_option"] = initial
    elif kind == "number":
        element = {"type": "number_input", "action_id": "v", "is_decimal_allowed": False}
        if field != "start_year":
            element["min_value"] = "0"
        if isinstance(current, int) and not isinstance(current, bool):
            element["initial_value"] = str(current)
    else:
        element = {"type": "plain_text_input", "action_id": "v"}
        if kind == "multiline":
            element["multiline"] = True
        if current not in (None, ""):
            element["initial_value"] = str(current)[:3000]
        if field in _PLACEHOLDERS:
            element["placeholder"] = _plain(_PLACEHOLDERS[field], 150)
    return {"type": "input", "block_id": field, "optional": not required,
            "label": _plain(label, 2000), "element": element}


def render_edit_modal(application_id: str, claims_version: int, claim: Claim) -> dict:
    """The Edit claim modal: the claim's source text, then one input per assertion field of its
    type (`block_id` = field name, element `action_id` = `v`). Claims that can't be edited
    (unchecked, needs_review) get a modal with a note and no inputs."""
    fields = _MODAL_FIELDS.get(claim.get("type", ""))
    blocks: list[dict[str, Any]] = [
        _context([_esc(_cut(claim.get("source_text", ""), 300))], "source_text")]
    if fields is None or claim.get("needs_review"):
        blocks.append(_section("This claim can't be edited.", "no_fields"))
    else:
        assertions = claim.get("assertions", {})
        blocks += [_modal_input(f, label, kind, req, opts, assertions.get(f))
                   for f, label, kind, req, opts in fields]
    return {
        "type": "modal",
        "callback_id": f"{CB_EDIT_CLAIM}:{application_id}:{claim['claim_id']}:{claims_version}",
        "private_metadata": json.dumps({"application_id": application_id,
                                        "claim_id": claim["claim_id"],
                                        "claims_version": claims_version}),
        "title": _plain("Edit claim", 24),
        "submit": _plain("Save", 24),
        "close": _plain("Cancel", 24),
        "blocks": blocks,
    }


# ── Greenhouse note ───────────────────────────────────────────────
def _note_date(computed_at: str) -> str:
    try:
        dt = datetime.fromisoformat(computed_at.replace("Z", "+00:00"))
    except ValueError:
        return computed_at[:10]
    return f"{dt.strftime('%b')} {dt.day}, {dt.year}"


def render_note(app: Application, claims: ClaimsDoc, verdict: Verdict,
                permalink: str | None) -> str:
    t = verdict.get("tally", {})
    claims_by_id = {c["claim_id"]: c for c in (claims or {}).get("claims", [])}
    lines = [
        f"Resume Verifier · {verdict.get('rubric_version', RUBRIC_DEFAULT)} · "
        f"{_note_date(verdict.get('computed_at', ''))}",
        f"{t.get('supported', 0)} supported · {t.get('partial', 0)} partially supported · "
        f"{t.get('contradicted', 0)} contradicted · {t.get('no_evidence', 0)} no public evidence",
    ]
    detail = []
    for label, prefix in (("contradicted", "Contradicted"), ("partial", "Partial")):
        for vc in verdict.get("claims", []):
            if vc["label"] == label:
                name = _claim_name(claims_by_id.get(vc["claim_id"]), vc, vc["claim_id"])
                detail.append(f"{prefix}: {name}: {vc.get('top_reason', '')}")
    lines += detail[:8]
    if len(detail) > 8:
        lines.append(f"+ {len(detail) - 8} more in the full report")
    if permalink:
        lines.append(f"Full report: {permalink}")
    lines.append("Evidence summary only; not a hiring recommendation.")
    return "\n".join(lines)
