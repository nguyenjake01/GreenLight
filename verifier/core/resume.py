"""Resume reading (design doc 8.3 `resume`, J1). `link_github` belongs to J4.

`python -m verifier.core.resume <pdf>` prints the PDF's `ResumeText` as canonical JSON.
"""

from __future__ import annotations

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


def link_github(resume: dict, app: dict) -> dict:
    """Added by J4."""
    raise NotImplementedError("link_github is implemented in J4")


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
