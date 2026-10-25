"""Generate placeholder resume PDFs (henry.pdf, aidan.pdf) next to this script.

Placeholders only: the real resumes may replace them. Every claim sits on one line so its
`source_text` can be found in the pdftotext output. Run from the repo root:

    .venv/bin/python fixtures/resumes/make_placeholder_resumes.py
    for n in henry aidan; do .venv/bin/python -m verifier.core.resume fixtures/resumes/$n.pdf > fixtures/resumes/$n.resume.json; done
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

AIDAN = [
    ("title", "Aidan Hong"),
    ("contact", "aidan@example.com | ", "github.com/aidanhong8", "https://github.com/aidanhong8"),
    ("line", "Applying for: Software Engineer Intern"),
    ("gap",),
    ("heading", "Projects"),
    ("line", "care360 (JavaScript, 2025): React website for a nutrition therapy and consulting company"),
    ("line", "HalloweenTracker (Swift, 2025): iOS app that tracks a Bluetooth beacon on a map, an Instructables Halloween contest entry"),
    ("line", "quiz (TypeScript, 2025): Expo mobile app with a login screen"),
    ("line", "portfolio-aidan-hong (JavaScript, 2021): open-sourced as a portfolio template with 500+ GitHub stars"),
    ("gap",),
    ("heading", "Open Source"),
    ("line", "Contributed 2 merged pull requests to boomty/CameraDetectionforTraffic"),
    ("gap",),
    ("heading", "Skills"),
    ("line", "Languages: JavaScript, TypeScript, Python, Swift"),
    ("gap",),
    ("heading", "Education"),
    ("line", "Diamond Bar High School"),
    ("gap",),
    ("heading", "Activities"),
    ("line", "Member, Future Business Leaders of America (FBLA), Diamond Bar High School"),
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
    build(HERE / "aidan.pdf", AIDAN)
    print("wrote", HERE / "henry.pdf", HERE / "aidan.pdf")
