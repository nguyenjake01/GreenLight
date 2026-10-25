"""J8: annotated resume PDF (design doc 8.3 `annotate`, D4, D20).

Opens the original from its bytes, marks every checked claim, appends a verification notes
page, and saves the result through `store.write_bytes` (write-once). The original is never
touched. No LLM, no emoji in PDF text: legend and notes use Helvetica with ASCII only.

Marks (drawn into the page so every viewer shows them, plus one native annotation each so
viewers with pop-ups, such as Firefox's pdf.js, show the label, reason and evidence link):
- project / oss_contribution: a coloured dot in the left margin level with the claim's first
  line, and a thin bar of the same colour beside the claim's lines.
- skill: a pastel highlight on the skill word, drawn under the text.
- unchecked: no marks.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any

import pymupdf

from verifier.contracts.errors import AnnotateError
from verifier.contracts.types import AnnotateReport, ClaimsDoc, Verdict
from verifier.core import store

AUTHOR = "Resume Verifier"

LABEL_WORDS = {
    "supported": "Supported",
    "partial": "Partially supported",
    "contradicted": "Contradicted",
    "no_evidence": "No public evidence",
}
FLAG_WORDS = {"run_failed": "run failed", "run_timeout": "run timed out"}

# Dot / bar colours (strong) and highlight colours (pastel), RGB 0..1.
STRONG = {
    "supported": (0.13, 0.65, 0.29),
    "partial": (0.96, 0.76, 0.05),
    "contradicted": (0.86, 0.15, 0.15),
    "no_evidence": (0.55, 0.55, 0.58),
}
PASTEL = {
    "supported": (0.70, 0.93, 0.72),
    "partial": (1.00, 0.94, 0.55),
    "contradicted": (1.00, 0.74, 0.74),
    "no_evidence": (0.86, 0.86, 0.88),
}
FAINT_GREY = (0.94, 0.94, 0.95)  # frameworks and tools: not checked in v1
RING = (0.10, 0.10, 0.12)
BLACK = (0, 0, 0)
GREY_TEXT = (0.35, 0.35, 0.38)

DOT_R = 3.6
RING_R = 5.6
BAR_W = 2.0
_EDGE_PUNCT = ",;:.()[]{}\"'"


# ── text helpers ──────────────────────────────────────────────────────────────

_CHAR_MAP = {
    "‘": "'", "’": "'", "‚": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", "−": "-", "•": "*", "…": "...",
    " ": " ",
}


def _norm(text: str) -> str:
    """Normalize for matching: NFKC, straight quotes and dashes, lower case."""
    text = unicodedata.normalize("NFKC", text)
    text = "".join(_CHAR_MAP.get(ch, ch) for ch in text)
    return text.casefold()


def _key(token: str) -> str:
    stripped = token.strip(_EDGE_PUNCT)
    return stripped or token


def _tokens(text: str) -> list[str]:
    return [_key(t) for t in _norm(text).split()]


def _ascii(text: str) -> str:
    """ASCII-only text for PDF fonts: map typographic characters, drop the rest (emoji)."""
    text = "".join(_CHAR_MAP.get(ch, ch) for ch in str(text))
    text = unicodedata.normalize("NFKD", text)
    return text.encode("ascii", "ignore").decode("ascii")


def _wrap(text: str, font: str, size: float, width: float) -> list[str]:
    """Greedy word wrap; words longer than the width (URLs) are cut by characters."""
    lines: list[str] = []
    cur = ""
    for word in text.split():
        while pymupdf.get_text_length(word, fontname=font, fontsize=size) > width:
            room = cur + (" " if cur else "")
            n = len(word)
            while n > 1 and pymupdf.get_text_length(room + word[:n], fontname=font, fontsize=size) > width:
                n -= 1
            if not cur and n <= 1:
                n = 1
            if cur and n <= 1:
                lines.append(cur)
                cur = ""
                continue
            lines.append(room + word[:n])
            cur, word = "", word[n:]
        trial = (cur + " " + word) if cur else word
        if cur and pymupdf.get_text_length(trial, fontname=font, fontsize=size) > width:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines or [""]


# ── locating claims ───────────────────────────────────────────────────────────

class _Word:
    __slots__ = ("rect", "tight", "key", "line_id")

    def __init__(self, rect: pymupdf.Rect, key: str, line_id: tuple[int, int],
                 tight: pymupdf.Rect | None = None):
        self.rect, self.key, self.line_id = rect, key, line_id
        self.tight = tight or rect  # rect without edge punctuation, for highlights


def _page_words(page: pymupdf.Page) -> list[_Word]:
    out = []
    for x0, y0, x1, y1, text, block, line, _n in page.get_text("words"):
        rect = pymupdf.Rect(x0, y0, x1, y1)
        parts = _norm(text).split()
        tight = rect
        if len(parts) == 1 and text and len(text.strip(_EDGE_PUNCT)) > 0:
            per_char = (x1 - x0) / len(text)
            lead = len(text) - len(text.lstrip(_EDGE_PUNCT))
            trail = len(text) - len(text.rstrip(_EDGE_PUNCT))
            tight = pymupdf.Rect(x0 + lead * per_char, y0, x1 - trail * per_char, y1)
        for part in parts:
            out.append(_Word(rect, _key(part), (block, line), tight))
    return out


def _find_sequence(words: list[_Word], tokens: list[str]) -> int | None:
    n = len(tokens)
    if n == 0:
        return None
    for i in range(len(words) - n + 1):
        if all(words[i + k].key == tokens[k] for k in range(n)):
            return i
    return None


def _line_rects(words: list[_Word], tight: bool = False) -> list[pymupdf.Rect]:
    """Union the words by text line, in reading order."""
    rects: dict[tuple[int, int], pymupdf.Rect] = {}
    for w in words:
        r = w.tight if tight else w.rect
        if w.line_id in rects:
            rects[w.line_id] |= r
        else:
            rects[w.line_id] = pymupdf.Rect(r)
    return sorted(rects.values(), key=lambda r: (round(r.y0), r.x0))


class _Hit:
    def __init__(self, page_no: int, words: list[_Word]):
        self.page_no = page_no
        self.words = words
        self.lines = _line_rects(words)


def _locate(pages_words: list[list[_Word]], source_text: str) -> _Hit | None:
    """Whole text first, then its first 8 words. First page / first position wins."""
    tokens = _tokens(source_text)
    attempts = [tokens]
    if len(tokens) > 8:
        attempts.append(tokens[:8])
    for attempt in attempts:
        for page_no, words in enumerate(pages_words):
            i = _find_sequence(words, attempt)
            if i is not None:
                return _Hit(page_no, words[i:i + len(attempt)])
    return None


def _locate_skill(hit: _Hit, name: str) -> list[_Word] | None:
    """The skill name's words inside the claim's own area only."""
    tokens = _tokens(name)
    i = _find_sequence(hit.words, tokens)
    if i is None:
        return None
    return hit.words[i:i + len(tokens)]


# ── pop-up content ────────────────────────────────────────────────────────────

def _popup_text(vc: dict[str, Any]) -> str:
    label = LABEL_WORDS.get(vc["label"], vc["label"])
    head = f"{label}: {vc.get('top_reason') or ''}".strip()
    flags = [FLAG_WORDS.get(f, str(f)) for f in vc.get("flags") or []]
    if flags:
        head = f"WARNING - {', '.join(flags)}. " + head
    lines = [_ascii(head)]
    lines += [_ascii(link) for link in vc.get("evidence_links") or []]
    return "\n".join(lines)


def _add_annot(page: pymupdf.Page, annot: pymupdf.Annot, content: str) -> None:
    """Set the pop-up text and author, and attach an explicit (closed) Popup annotation."""
    annot.set_info(content=content, title=AUTHOR)
    annot.update()
    page_rect = page.rect
    r = annot.rect
    x0 = min(r.x1 + 4, page_rect.x1 - 224)
    y0 = min(max(r.y0, page_rect.y0 + 4), page_rect.y1 - 84)
    annot.set_popup(pymupdf.Rect(x0, y0, x0 + 220, y0 + 80))
    annot.set_open(False)


# ── drawing marks ─────────────────────────────────────────────────────────────

def _draw_dot_and_bar(page: pymupdf.Page, hit: _Hit, vc: dict[str, Any], content: str,
                      margin_x: float) -> None:
    label = vc["label"]
    colour = STRONG[label]
    first = hit.lines[0]
    cx = max(DOT_R + 3, margin_x - 14)
    cy = (first.y0 + first.y1) / 2
    flagged = bool(vc.get("flags"))

    # Bar: thin, beside all of the claim's lines, always left of the text.
    bar_x1 = min(cx + 7, margin_x - 2)
    bar_x0 = max(bar_x1 - BAR_W, cx + DOT_R + 1)
    if bar_x1 > bar_x0:
        bar = pymupdf.Rect(bar_x0, hit.lines[0].y0, bar_x1, hit.lines[-1].y1)
        page.draw_rect(bar, color=None, fill=colour, overlay=True)

    # Dot: hollow for no-evidence, filled otherwise; a dark ring marks a warning.
    hollow = label == "no_evidence"
    dot_rect = pymupdf.Rect(cx - DOT_R, cy - DOT_R, cx + DOT_R, cy + DOT_R)
    if hollow:
        page.draw_circle((cx, cy), DOT_R - 0.6, color=colour, fill=(1, 1, 1), width=1.3, overlay=True)
    else:
        page.draw_circle((cx, cy), DOT_R, color=colour, fill=colour, width=0.5, overlay=True)
    if flagged:
        page.draw_circle((cx, cy), RING_R, color=RING, fill=None, width=1.3, overlay=True)
    hit_rect = pymupdf.Rect(cx - RING_R, cy - RING_R, cx + RING_R, cy + RING_R) if flagged else dot_rect

    # Native annotation over the dot: the pop-up. It repeats the drawn look.
    annot = page.add_circle_annot(hit_rect)
    if flagged:
        annot.set_colors(stroke=RING, fill=None)
        annot.set_border(width=1.3)
    elif hollow:
        annot.set_colors(stroke=colour, fill=(1, 1, 1))
        annot.set_border(width=1.3)
    else:
        annot.set_colors(stroke=colour, fill=colour)
        annot.set_border(width=0.5)
    _add_annot(page, annot, content)


def _draw_highlight(page: pymupdf.Page, words: list[_Word], colour: tuple[float, float, float],
                    content: str) -> list[pymupdf.Rect]:
    rects = _line_rects(words, tight=True)
    for r in rects:
        page.draw_rect(r, color=None, fill=colour, overlay=False)  # under the text
    # Native highlight carries the pop-up. Its own paint is fully transparent, since the
    # coloured rectangle is already drawn under the text.
    annot = page.add_highlight_annot(quads=[r.quad for r in rects])
    annot.set_colors(stroke=colour)
    annot.set_opacity(0.0)
    _add_annot(page, annot, content)
    return rects


# ── legend ────────────────────────────────────────────────────────────────────

def _draw_legend(page: pymupdf.Page, margin_x: float) -> None:
    size = 7.0
    h = page.rect.height
    x0 = margin_x
    y = h - 30
    page.insert_text((x0, y - 11), "Resume Verifier", fontname="hebo", fontsize=size, color=GREY_TEXT)
    x = x0 + pymupdf.get_text_length("Resume Verifier", fontname="hebo", fontsize=size) + 10
    for label in ("supported", "partial", "contradicted", "no_evidence"):
        text = LABEL_WORDS[label]
        if label == "no_evidence":
            page.draw_circle((x + 3, y - 13.2), 2.6, color=STRONG[label], fill=(1, 1, 1), width=1.1)
        else:
            page.draw_circle((x + 3, y - 13.2), 2.8, color=STRONG[label], fill=STRONG[label], width=0.4)
        page.insert_text((x + 9, y - 11), text, fontname="helv", fontsize=size, color=GREY_TEXT)
        x += 9 + pymupdf.get_text_length(text, fontname="helv", fontsize=size) + 10
    page.insert_text((x0, y), "Highlights mark skills. Hover for details. Full list on the "
                     "last page.", fontname="helv", fontsize=size, color=GREY_TEXT)


# ── notes page ────────────────────────────────────────────────────────────────

class _Notes:
    """Builds the appended notes page(s) with Helvetica and ASCII text."""

    MARGIN = 54.0

    def __init__(self, doc: pymupdf.Document, width: float, height: float):
        self.doc, self.w, self.h = doc, width, height
        self.page: pymupdf.Page | None = None
        self.y = 0.0
        self.pages = 0
        self._new_page()

    def _new_page(self) -> None:
        self.page = self.doc.new_page(width=self.w, height=self.h)
        self.pages += 1
        self.y = self.MARGIN

    def _room(self, needed: float) -> None:
        if self.y + needed > self.h - self.MARGIN:
            self._new_page()

    @property
    def text_width(self) -> float:
        return self.w - 2 * self.MARGIN

    def line(self, text: str, size: float = 9.0, font: str = "helv", indent: float = 0.0,
             color: tuple = BLACK, gap: float = 3.0, link: str | None = None) -> None:
        assert self.page is not None
        lines = _wrap(_ascii(text), font, size, self.text_width - indent)
        for ln in lines:
            self._room(size + gap)
            assert self.page is not None
            self.y += size
            self.page.insert_text((self.MARGIN + indent, self.y), ln, fontname=font, fontsize=size, color=color)
            if link:
                width = pymupdf.get_text_length(ln, fontname=font, fontsize=size)
                rect = pymupdf.Rect(self.MARGIN + indent, self.y - size, self.MARGIN + indent + width, self.y + 2)
                self.page.insert_link({"kind": pymupdf.LINK_URI, "from": rect, "uri": link})
            self.y += gap

    def space(self, pts: float) -> None:
        self.y += pts

    def entry_head(self, number: int, label: str, text: str) -> None:
        """'N. (dot) Label - Name' line; the dot is a drawn shape."""
        size = 10.0
        self._room(size * 4)
        assert self.page is not None
        num = f"{number}."
        self.page.insert_text((self.MARGIN, self.y + size), num, fontname="hebo", fontsize=size)
        dot_x = self.MARGIN + 20
        cy = self.y + size - 3.2
        colour = STRONG[label]
        if label == "no_evidence":
            self.page.draw_circle((dot_x, cy), 3.0, color=colour, fill=(1, 1, 1), width=1.3)
        else:
            self.page.draw_circle((dot_x, cy), 3.6, color=colour, fill=colour, width=0.5)
        indent = 30.0
        lines = _wrap(_ascii(text), "hebo", size, self.text_width - indent)
        for ln in lines:
            self._room(size + 3)
            assert self.page is not None
            self.y += size
            self.page.insert_text((self.MARGIN + indent, self.y), ln, fontname="hebo", fontsize=size)
            self.y += 3


def _claim_name(claim: dict[str, Any]) -> str:
    a = claim.get("assertions") or {}
    t = claim["type"]
    name = None
    if t == "skill":
        name = a.get("name")
    elif t == "project":
        name = a.get("repo_name")
    elif t == "oss_contribution":
        name = a.get("repo")
    if not name:
        name = " ".join(claim["source_text"].split())
        if len(name) > 60:
            name = name[:57].rstrip() + "..."
    return str(name)


def _build_notes(doc: pymupdf.Document, claims_doc: ClaimsDoc, verdict: Verdict,
                 rows: list[dict[str, Any]], unchecked: list[dict[str, Any]],
                 located: set[str]) -> None:
    first = doc[0].rect
    n = _Notes(doc, first.width, first.height)
    cand = (claims_doc.get("candidate") or {}).get("name")
    n.line("Resume Verifier - Verification notes", size=16, font="hebo", gap=6)
    sub = []
    if cand:
        sub.append(f"Candidate: {cand}")
    sub.append(f"Verdict: {verdict.get('verdict_id', '')}")
    if verdict.get("computed_at"):
        sub.append(f"Computed: {verdict['computed_at']}")
    n.line(" | ".join(sub), size=8.5, color=GREY_TEXT)
    tally = {k: 0 for k in LABEL_WORDS}
    for row in rows:
        if row["vc"]:
            tally[row["vc"]["label"]] = tally.get(row["vc"]["label"], 0) + 1
    n.line("Checked claims: " + ", ".join(f"{LABEL_WORDS[k]} {tally[k]}" for k in LABEL_WORDS),
           size=8.5, color=GREY_TEXT)
    n.line("This is the same information as the pop-ups on page 1, for viewers without pop-ups "
           "and for printing. No public evidence does not mean the claim is false.",
           size=8.5, color=GREY_TEXT, gap=4)
    n.space(8)

    if not rows:
        n.line("No checked claims.", size=9.5)
    for i, row in enumerate(rows, start=1):
        claim, vc = row["claim"], row["vc"]
        label = vc["label"] if vc else "no_evidence"
        title = f"{LABEL_WORDS.get(label, label)} - {_claim_name(claim)}"
        if claim["type"] == "oss_contribution":
            title += " (open source contribution)"
        n.entry_head(i, label, title)
        if vc is None:
            n.line("No verdict for this claim.", size=9, indent=30)
        else:
            flags = [FLAG_WORDS.get(f, str(f)) for f in vc.get("flags") or []]
            if flags:
                n.line("WARNING - " + ", ".join(flags), size=9, font="hebo", indent=30)
            n.line(vc.get("top_reason") or "", size=9, indent=30)
            for note in vc.get("notes") or []:
                n.line(f"Note: {note}", size=8.5, indent=30, color=GREY_TEXT)
            for link in vc.get("evidence_links") or []:
                n.line(link, size=8, indent=30, color=(0.05, 0.25, 0.65), link=link)
        if claim["claim_id"] not in located:
            n.line("Not marked on the resume page: the claim text was not found on the page.",
                   size=8, indent=30, color=GREY_TEXT)
        n.space(7)

    n.space(4)
    if unchecked:
        cats: dict[str, int] = {}
        for c in unchecked:
            cat = (c.get("assertions") or {}).get("category") or "other"
            cats[cat] = cats.get(cat, 0) + 1
        listed = ", ".join(f"{cat.replace('_', ' ')} ({k})" for cat, k in sorted(cats.items()))
        n.line(f"Not checked (no public code to check): {listed}.", size=9, color=GREY_TEXT)
    else:
        n.line("No unchecked categories.", size=9, color=GREY_TEXT)


# ── entry point ───────────────────────────────────────────────────────────────

def annotate_pdf(resume_pdf: Path, claims: ClaimsDoc, verdict: Verdict,
                 out_path: Path) -> AnnotateReport:
    """Writes a new annotated PDF at `out_path`; never touches `resume_pdf`."""
    resume_pdf, out_path = Path(resume_pdf), Path(out_path)
    try:
        original = resume_pdf.read_bytes()
        doc = pymupdf.open(stream=original, filetype="pdf")
    except Exception as exc:  # unreadable / not a PDF
        raise AnnotateError(f"cannot open {resume_pdf}: {exc}") from exc
    try:
        if doc.page_count == 0:
            raise AnnotateError(f"{resume_pdf} has no pages")
        by_id = {vc["claim_id"]: vc for vc in verdict.get("claims", [])}
        pages_words = [_page_words(p) for p in doc]
        margins = [min((w.rect.x0 for w in ws), default=54.0) for ws in pages_words]

        located: list[str] = []
        not_located: list[str] = []
        rows: list[dict[str, Any]] = []
        unchecked: list[dict[str, Any]] = []

        for claim in claims["claims"]:
            cid = claim["claim_id"]
            if claim["type"] == "unchecked":
                unchecked.append(claim)
                continue
            vc = by_id.get(cid)
            rows.append({"claim": claim, "vc": vc})
            hit = _locate(pages_words, claim["source_text"]) if vc else None
            if hit is None:
                not_located.append(cid)
                continue
            page = doc[hit.page_no]
            content = _popup_text(vc)
            if claim["type"] == "skill":
                words = _locate_skill(hit, (claim.get("assertions") or {}).get("name", ""))
                if words is None:
                    not_located.append(cid)
                    continue
                faint = (claim["assertions"].get("kind") == "framework_tool"
                         and vc["label"] == "no_evidence")
                _draw_highlight(page, words, FAINT_GREY if faint else PASTEL[vc["label"]], content)
            else:
                _draw_dot_and_bar(page, hit, vc, content, margins[hit.page_no])
            located.append(cid)

        _draw_legend(doc[0], margins[0])
        _build_notes(doc, claims, verdict, rows, unchecked, set(located))
        doc.set_metadata({**doc.metadata, "producer": AUTHOR})
        data = doc.tobytes(garbage=3, deflate=True)
    except AnnotateError:
        raise
    except Exception as exc:
        raise AnnotateError(f"annotating {resume_pdf} failed: {exc}") from exc
    finally:
        doc.close()

    try:
        store.write_bytes(out_path, data)
    except Exception as exc:
        raise AnnotateError(f"cannot write {out_path}: {exc}") from exc
    return {"out_path": str(out_path), "located": located, "not_located": not_located}
