"""Generate the sample resumes henry.pdf and aidan.pdf in the UC Berkeley "Technical Resume" layout.

Centered bold name, one centered contact line, bold section headings with a full-width rule,
bold entry titles with a right-aligned date, and "●" bullets with a hanging indent. Every claim
line is a bullet on ONE physical line (the font shrinks for a long line; it never wraps) so its
`source_text` can be found verbatim in the pdftotext output. The GitHub profile is a real link
annotation. karpathy.pdf still comes from make_placeholder_resumes.py. Run from the repo root:

    .venv/bin/python fixtures/resumes/make_sample_resumes.py
    for n in henry aidan; do .venv/bin/python -m verifier.core.resume fixtures/resumes/$n.pdf > fixtures/resumes/$n.resume.json; done
"""

from __future__ import annotations

from pathlib import Path

try:
    import pymupdf
except ImportError:  # older PyMuPDF
    import fitz as pymupdf  # type: ignore[no-redef]

HERE = Path(__file__).resolve().parent
REG, BOLD = "helv", "hebo"
LEFT, RIGHT = 36.0, 576.0
BULLET_X, TEXT_X = 54.0, 70.0
SIZE = 10.5
MIN_SIZE = 8.0

# ("heading", t) | ("entry", bold_title, rest_regular, right_date) | ("bullet", t) | ("text", bold_label, t)
HENRY = {
    "name": "Henry Chen",
    "email": "henry@example.com",
    "gh": ("github.com/henrychen4736", "https://github.com/henrychen4736"),
    "applying": "Applying for: Software Engineer Intern",
    "body": [
        ("heading", "Projects"),
        ("entry", "Polytrack-ML", "", "2026"),
        ("bullet", "Polytrack-ML (JavaScript, 2026): reinforcement learning agents for a racing game"),
        ("entry", "CAC24", "", "2024"),
        ("bullet", "CAC24 (Dart, Python, 2024): Flutter mobile app with a Python backend"),
        ("entry", "cubesolver", "", "2024"),
        ("bullet", "Built cubesolver, a full Rubik's cube solver in C++ (2024)"),
        ("gap",),
        ("heading", "Skills"),
        ("bullet", "Languages: Python, Dart, JavaScript, C++"),
        ("gap",),
        ("heading", "Experience"),
        ("entry", "Brightpath Learning", "", "Jun 2025 - Aug 2025"),
        ("bullet", "Math Tutor, Brightpath Learning, Jun 2025 to Aug 2025"),
        ("bullet", "Helped middle school students with weekly math homework and test preparation"),
        ("gap",),
        ("heading", "Education"),
        ("entry", "Westview High School", "", "Expected 2027"),
        ("bullet", "Westview High School, Class of 2027"),
        ("bullet", "Relevant Coursework: Precalculus, AP Computer Science, Physics"),
    ],
}

AIDAN = {
    "name": "Aidan Hong",
    "email": "aidan@example.com",
    "gh": ("github.com/aidanhong8", "https://github.com/aidanhong8"),
    "applying": "Applying for: Software Engineer Intern",
    "body": [
        ("heading", "Projects"),
        ("entry", "care360", "", "2025"),
        ("bullet", "care360 (JavaScript, 2025): React website for a nutrition therapy and consulting company"),
        ("entry", "HalloweenTracker", "", "2025"),
        ("bullet", "HalloweenTracker (Swift, 2025): iOS app that tracks a Bluetooth beacon on a map, an Instructables Halloween contest entry"),
        ("entry", "quiz", "", "2025"),
        ("bullet", "quiz (TypeScript, 2025): Expo mobile app with a login screen"),
        ("entry", "portfolio-aidan-hong", "", "2021"),
        ("bullet", "portfolio-aidan-hong (JavaScript, 2021): open-sourced as a portfolio template with 500+ GitHub stars"),
        ("gap",),
        ("heading", "Open Source"),
        ("entry", "boomty/CameraDetectionforTraffic", "", ""),
        ("bullet", "Contributed 2 merged pull requests to boomty/CameraDetectionforTraffic"),
        ("gap",),
        ("heading", "Skills"),
        ("bullet", "Languages: JavaScript, TypeScript, Python, Swift"),
        ("gap",),
        ("heading", "Education"),
        ("entry", "Diamond Bar High School", "", ""),
        ("gap",),
        ("heading", "Leadership and Extracurricular Activities"),
        ("entry", "Future Business Leaders of America (FBLA)", "", ""),
        ("bullet", "Member, Future Business Leaders of America (FBLA), Diamond Bar High School"),
    ],
}


def _w(text: str, font: str, size: float) -> float:
    return pymupdf.get_text_length(text, fontname=font, fontsize=size)


def build(path: Path, data: dict) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    y = 60.0
    cx = 306.0

    name = data["name"].upper()
    page.insert_text((cx - _w(name, BOLD, 17) / 2, y), name, fontname=BOLD, fontsize=17)
    y += 16
    prefix, anchor = data["email"] + " | ", data["gh"][0]
    x0 = cx - _w(prefix + anchor, REG, SIZE) / 2
    page.insert_text((x0, y), prefix + anchor, fontname=REG, fontsize=SIZE)
    lx0 = x0 + _w(prefix, REG, SIZE)
    page.insert_link({"kind": pymupdf.LINK_URI,
                      "from": pymupdf.Rect(lx0, y - SIZE + 1, lx0 + _w(anchor, REG, SIZE), y + 2),
                      "uri": data["gh"][1]})
    y += 13.5
    ap = data["applying"]
    page.insert_text((cx - _w(ap, REG, 9.5) / 2, y), ap, fontname=REG, fontsize=9.5)
    y += 30

    prev = ""
    for item in data["body"]:
        kind = item[0]
        if kind == "entry" and prev == "bullet":  # a second entry in the same section
            y += 10
        prev = kind
        if kind == "heading":
            page.insert_text((LEFT, y), item[1], fontname=BOLD, fontsize=SIZE)
            page.draw_line((LEFT, y + 3.5), (RIGHT, y + 3.5), width=1.0)
            y += 19
        elif kind == "entry":
            _, title, rest, date = item
            page.insert_text((LEFT, y), title, fontname=BOLD, fontsize=SIZE)
            if rest:
                page.insert_text((LEFT + _w(title, BOLD, SIZE), y), rest, fontname=REG, fontsize=SIZE)
            if date:
                page.insert_text((RIGHT - _w(date, BOLD, SIZE), y), date, fontname=BOLD, fontsize=SIZE)
            y += 16
        elif kind == "bullet":
            text = item[1]
            size = SIZE
            while _w(text, REG, size) > RIGHT - TEXT_X and size > MIN_SIZE:
                size -= 0.1
            assert _w(text, REG, size) <= RIGHT - TEXT_X + 0.5, f"line too long: {text}"
            page.draw_circle((BULLET_X + 4, y - 3.4), 2.4, color=None, fill=(0, 0, 0))  # filled-circle bullet
            page.insert_text((TEXT_X, y), text, fontname=REG, fontsize=size)
            y += 15.5
        elif kind == "gap":
            y += 16
    assert y < 760, y
    doc.set_metadata({"title": f"{data['name']} resume", "creator": "make_sample_resumes.py",
                      "creationDate": "", "modDate": ""})
    doc.save(str(path), garbage=4, deflate=True, no_new_id=True)
    doc.close()


if __name__ == "__main__":
    build(HERE / "henry.pdf", HENRY)
    build(HERE / "aidan.pdf", AIDAN)
    print("wrote", HERE / "henry.pdf", HERE / "aidan.pdf")
