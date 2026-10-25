"""Resume reading (design doc 8.3 `resume`, J1). `link_github` belongs to J4.

`python -m verifier.core.resume <pdf>` prints the PDF's `ResumeText` as canonical JSON.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from verifier.contracts.errors import ResumeError
from verifier.core.store import dump_json, sha256_bytes


def read_resume(pdf_path: Path) -> dict:
    """Return a `ResumeText`: `{text, links, sha256, page_count}`. Writes nothing.

    Text comes from `pdftotext -enc UTF-8 <pdf> -` (default reading-order mode; form feeds become
    newlines). Links are PyMuPDF link annotations with a URI: `{uri, anchor_text, page}`.
    Raises `ResumeError` if pdftotext is missing or fails, the PDF won't open, or it has no text.
    """
    pdf_path = Path(pdf_path)
    try:
        data = pdf_path.read_bytes()
    except OSError as e:
        raise ResumeError(f"cannot read {pdf_path}: {e}") from e

    try:
        proc = subprocess.run(
            ["pdftotext", "-enc", "UTF-8", str(pdf_path), "-"],
            capture_output=True, timeout=60,
        )
    except FileNotFoundError as e:
        raise ResumeError("pdftotext is not installed") from e
    except subprocess.TimeoutExpired as e:
        raise ResumeError("pdftotext timed out") from e
    if proc.returncode != 0:
        msg = proc.stderr.decode("utf-8", "replace").strip()[:300]
        raise ResumeError(f"pdftotext failed ({proc.returncode}): {msg}")
    text = proc.stdout.decode("utf-8", "replace").replace("\f", "\n")
    if not text.strip():
        raise ResumeError("no text in PDF (scanned resume?)")

    links, page_count = _read_links(pdf_path)
    return {
        "text": text,
        "links": links,
        "sha256": sha256_bytes(data),
        "page_count": page_count,
    }


def _read_links(pdf_path: Path) -> tuple[list[dict], int]:
    try:
        import pymupdf
    except ImportError:  # older PyMuPDF
        import fitz as pymupdf  # type: ignore[no-redef]
    try:
        doc = pymupdf.open(str(pdf_path))
    except Exception as e:  # pymupdf raises several types for bad files
        raise ResumeError(f"cannot open PDF: {e}") from e
    links: list[dict] = []
    try:
        for page_no, page in enumerate(doc):
            for link in page.get_links():
                uri = link.get("uri")
                if not uri:
                    continue
                anchor = " ".join(page.get_textbox(link["from"]).split())
                links.append({"uri": uri, "anchor_text": anchor, "page": page_no})
        return links, doc.page_count
    except Exception as e:
        raise ResumeError(f"cannot read PDF links: {e}") from e
    finally:
        doc.close()


# ── link_github (J4): pure text matching, no network, no LLM ─────────────────────────────
_RESERVED_PATHS = frozenset((
    "orgs features topics sponsors settings marketplace explore about pricing login join apps "
    "collections trending events notifications enterprise security site contact search pulls "
    "issues new codespaces"
).split())
_LOGIN_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9]))*")
_SEG = r"[A-Za-z0-9_.\-]+"
# `gist.github.com`, `raw.githubusercontent.com` and look-alike hosts never match: the host must
# not be preceded by a word character, dot or hyphen (a scheme or `www.` is fine).
_GH_COM_RE = re.compile(
    r"(?<![\w.\-])(?:https?://)?(?:www\.)?github\.com/(" + _SEG + r")(?:/(" + _SEG + r"))?",
    re.IGNORECASE,
)
_GH_IO_RE = re.compile(
    r"(?<![\w.\-])(?:https?://)?([A-Za-z0-9\-]+)\.github\.io(?![A-Za-z0-9\-])", re.IGNORECASE
)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")
_PROFILE, _REPO, _PAGES = 0, 1, 2  # rank within a source: lower wins


def _valid_login(seg: str) -> str | None:
    seg = seg.rstrip(".")  # trailing sentence punctuation
    if len(seg) <= 39 and _LOGIN_RE.fullmatch(seg):
        return seg
    return None


def _github_logins(text: str) -> list[tuple[int, str]]:
    """Every GitHub login in `text` as `(rank, login)`, in order of appearance."""
    found: list[tuple[int, int, str]] = []  # (position, rank, login)
    for m in _GH_COM_RE.finditer(text):
        login = _valid_login(m.group(1))
        if login is None or login.lower() in _RESERVED_PATHS:
            continue
        second = m.group(2)
        is_repo = bool(second) and bool(second.rstrip("."))
        found.append((m.start(), _REPO if is_repo else _PROFILE, login))
    for m in _GH_IO_RE.finditer(text):
        login = _valid_login(m.group(1))
        if login is not None:
            found.append((m.start(), _PAGES, login))
    found.sort(key=lambda t: t[0])
    return [(rank, login) for _, rank, login in found]


def _best_login(texts: list[str]) -> str | None:
    """Best login across one source's strings: best rank, then most frequent, then earliest."""
    found = [hit for t in texts for hit in _github_logins(t)]
    if not found:
        return None
    best_rank = min(rank for rank, _ in found)
    pool = [login for rank, login in found if rank == best_rank]
    counts: dict[str, int] = {}
    for login in pool:
        counts[login.lower()] = counts.get(login.lower(), 0) + 1
    top = max(counts.values())
    return next(login for login in pool if counts[login.lower()] == top)


def link_github(resume: dict, app: dict) -> dict:
    """Return a `Candidate` (design doc 8.2): the applicant's GitHub login and merged emails.

    Sources in priority order: resume link URIs, resume text (both `resume_link`), Greenhouse
    `website_addresses` (`greenhouse_website`), `social_media_addresses` (`greenhouse_social`).
    The first source with a login wins; a different login (ignoring case) in a later Greenhouse
    source is recorded as `github_login_conflict`. Pure: no network, never raises.
    """
    resume = resume or {}
    text = resume.get("text") or ""
    uris = [str(link.get("uri") or "") for link in (resume.get("links") or []) if link]
    sources: list[tuple[str, list[str]]] = [
        ("resume_link", uris),
        ("resume_link", [text]),
        ("greenhouse_website", [str(w) for w in (app.get("website_addresses") or [])]),
        ("greenhouse_social", [str(w) for w in (app.get("social_media_addresses") or [])]),
    ]
    bests = [(label, _best_login(texts)) for label, texts in sources]

    login: str | None = None
    source = "none"
    conflict: dict | None = None
    for i, (label, best) in enumerate(bests):
        if best is None:
            continue
        login, source = best, label
        for other_label, other in bests[i + 1:]:
            if other_label.startswith("greenhouse") and other and other.lower() != best.lower():
                conflict = {"login": other, "source": other_label}
                break
        break

    emails: list[str] = []
    seen: set[str] = set()
    found_in_text = [m.group(0).rstrip(".") for m in _EMAIL_RE.finditer(text)]
    for email in [*(app.get("emails") or []), *found_in_text]:
        email = str(email).strip()
        if email and email.lower() not in seen:
            seen.add(email.lower())
            emails.append(email)

    return {
        "name": app.get("candidate_name", ""),
        "emails": emails,
        "github_login": login,
        "github_login_source": source,
        "github_login_conflict": conflict,
    }


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: python -m verifier.core.resume <resume.pdf>", file=sys.stderr)
        return 2
    try:
        result = read_resume(Path(argv[0]))
    except ResumeError as e:
        print(f"ResumeError: {e}", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(dump_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
