"""Generate placeholder resume PDFs (henry.pdf, fake.pdf) next to this script.

Placeholders only: the real resumes may replace them. Every claim sits on one line so its
`source_text` can be found in the pdftotext output. Run from the repo root:

    .venv/bin/python fixtures/resumes/make_placeholder_resumes.py
    for n in henry fake; do .venv/bin/python -m verifier.core.resume fixtures/resumes/$n.pdf > fixtures/resumes/$n.resume.json; done
"""

from __future__ import annotations

from pathlib import Path

try:
    import pymupdf
except ImportError:  # older PyMuPDF
    import fitz as pymupdf  # type: ignore[no-redef]

HERE = Path(__file__).resolve().parent
FONT, BOLD = "helv", "hebo"
SIZE = 10
LEFT = 54

# Each entry: ("title"|"heading"|"line"|"contact", text) ; contact lines may carry a link.
HENRY = [
    ("title", "Henry Chen"),
    ("contact", "henry@example.com | ", "github.com/henrychen4736", "https://github.com/henrychen4736"),
    ("line", "Applying for: Software Engineer Intern"),
    ("gap",),
    ("heading", "Projects"),
    ("line", "Polytrack-ML (JavaScript, 2026): reinforcement learning agents for a racing game"),
    ("line", "CAC24 (Dart, Python, 2024): Flutter mobile app with a Python backend"),
    ("line", "Built cubesolver, a full Rubik's cube solver in C++ (2024)"),
    ("gap",),
    ("heading", "Skills"),
    ("line", "Languages: Python, Dart, JavaScript, C++"),
    ("gap",),
    ("heading", "Experience"),
    ("line", "Math Tutor, Brightpath Learning, Jun 2025 to Aug 2025"),
    ("gap",),
    ("heading", "Education"),
    ("line", "Westview High School, Class of 2027"),
]

FAKE = [
    ("title", "Alex Rivera"),
    ("contact", "alex.rivera@example.com | ", "github.com/rv-fake-candidate", "https://github.com/rv-fake-candidate"),
    ("line", "Applying for: Software Engineer Intern"),
    ("gap",),
    ("heading", "Projects"),
    ("line", "fastgrid (Python): maintain an open-source library with 2k stars since 2023"),
    ("gap",),
    ("heading", "Open Source"),
    ("line", "Contributed a merged pull request to henrychen4736/cubesolver"),
    ("gap",),
    ("heading", "Skills"),
    ("line", "Languages: Python, Rust"),
    ("gap",),
    ("heading", "Education"),
    ("line", "Northside University, B.S. Computer Science, 2025"),
]


def build(path: Path, entries: list[tuple]) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    y = 72.0
    for entry in entries:
        kind = entry[0]
        if kind == "title":
            page.insert_text((LEFT, y), entry[1], fontname=BOLD, fontsize=18)
            y += 22
        elif kind == "heading":
            page.insert_text((LEFT, y), entry[1].upper(), fontname=BOLD, fontsize=11)
            page.draw_line((LEFT, y + 3), (558, y + 3), width=0.5)
            y += 17
        elif kind == "line":
            page.insert_text((LEFT, y), entry[1], fontname=FONT, fontsize=SIZE)
            y += 15
        elif kind == "contact":
            _, prefix, anchor, uri = entry
            page.insert_text((LEFT, y), prefix + anchor, fontname=FONT, fontsize=SIZE)
            x0 = LEFT + pymupdf.get_text_length(prefix, fontname=FONT, fontsize=SIZE)
            x1 = x0 + pymupdf.get_text_length(anchor, fontname=FONT, fontsize=SIZE)
            rect = pymupdf.Rect(x0, y - SIZE + 1, x1, y + 2)
            page.insert_link({"kind": pymupdf.LINK_URI, "from": rect, "uri": uri})
            y += 15
        elif kind == "gap":
            y += 8
    doc.set_metadata({"title": "Placeholder resume", "creator": "make_placeholder_resumes.py"})
    # deterministic output: no timestamps in the info dict
    doc.set_metadata({"creationDate": "", "modDate": ""})
    doc.save(str(path), garbage=4, deflate=True, no_new_id=True)
    doc.close()


if __name__ == "__main__":
    build(HERE / "henry.pdf", HENRY)
    build(HERE / "fake.pdf", FAKE)
    print("wrote", HERE / "henry.pdf", HERE / "fake.pdf")
