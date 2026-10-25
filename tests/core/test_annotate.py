"""J8 tests: annotated resume PDF (design doc 8.3 `annotate`)."""

import copy
import hashlib
import json
from pathlib import Path

import pymupdf
import pytest

from verifier.contracts.errors import AnnotateError
from verifier.core import annotate

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
HENRY_PDF = FIXTURES / "resumes" / "henry.pdf"
CLAIMS = json.loads((FIXTURES / "claims" / "henrychen4736.json").read_text())
VERDICT = json.loads((FIXTURES / "verdicts" / "henrychen4736.json").read_text())

LABEL_WORDS = {"supported": "Supported", "partial": "Partially supported",
               "contradicted": "Contradicted", "no_evidence": "No public evidence"}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(tmp_path, claims=CLAIMS, verdict=VERDICT, pdf=HENRY_PDF, name="out.pdf"):
    out = tmp_path / "annotated" / name
    report = annotate.annotate_pdf(pdf, claims, verdict, out)
    return report, out


class _A:
    """Plain snapshot of an annotation (PyMuPDF annots die with their page object)."""

    def __init__(self, a):
        self.kind = a.type[1]
        self.rect = pymupdf.Rect(a.rect)
        self.info = dict(a.info)
        self.has_popup = bool(a.has_popup)


def _annots(doc):
    return [_A(a) for page in doc for a in page.annots() if a.type[0] != 16]  # skip Popup


def _make_pdf(path: Path, lines: list[str]) -> Path:
    doc = pymupdf.open()
    page = doc.new_page()
    y = 100
    for ln in lines:
        page.insert_text((72, y), ln, fontname="helv", fontsize=11)
        y += 20
    doc.save(path)
    return path


def _claim(cid, ctype, text, **assertions):
    return {"claim_id": cid, "type": ctype, "section": None, "source_text": text,
            "needs_review": False, "assertions": assertions}


def _vc(cid, ctype, label, reason="reason here", flags=(), links=("https://github.com/o/r",)):
    return {"claim_id": cid, "type": ctype, "label": label, "flags": list(flags),
            "top_reason": reason, "checks": [], "notes": [], "evidence_links": list(links)}


def _docs(claims, vcs):
    cd = {"claims": claims, "candidate": {"name": "Test Person"}}
    v = {"verdict_id": "verdict_1", "claims": vcs, "tally": {}, "computed_at": "2026-01-01T00:00:00Z"}
    return cd, v


# ── Henry's resume with the verdict fixture ──────────────────────────────────

def test_output_written_and_original_untouched(tmp_path):
    before = _sha(HENRY_PDF)
    report, out = _run(tmp_path)
    assert out.is_file()
    assert report["out_path"] == str(out)
    assert _sha(HENRY_PDF) == before
    assert _sha(out) != before


def test_every_located_claim_has_a_mark_and_popup(tmp_path):
    report, out = _run(tmp_path)
    checked = [c["claim_id"] for c in CLAIMS["claims"] if c["type"] != "unchecked"]
    assert report["located"] == checked
    assert report["not_located"] == []

    doc = pymupdf.open(out)
    annots = _annots(doc)
    assert len(annots) == len(checked)  # one per mark; unchecked claims get none
    vcs = {v["claim_id"]: v for v in VERDICT["claims"]}
    contents = [a.info["content"] for a in annots]
    for cid in checked:
        vc = vcs[cid]
        expected = f"{LABEL_WORDS[vc['label']]}: {vc['top_reason']}"
        matches = [c for c in contents if c.startswith(expected)]
        assert matches, cid
        assert any(m.splitlines()[1] == vc["evidence_links"][0] for m in matches), cid
    for a in annots:
        assert a.info["title"] == "Resume Verifier"
        assert a.has_popup


def test_annotation_subtypes_per_claim_type(tmp_path):
    _, out = _run(tmp_path)
    doc = pymupdf.open(out)
    by_content = {a.info["content"].split(":")[0] + a.info["content"].splitlines()[1]: a.kind
                  for a in _annots(doc)}
    kinds = [a.kind for a in _annots(doc)]
    assert kinds.count("Circle") == 3     # three projects: dots
    assert kinds.count("Highlight") == 4  # four skills
    assert by_content


def test_page_count_and_legend(tmp_path):
    _, out = _run(tmp_path)
    orig = pymupdf.open(HENRY_PDF)
    doc = pymupdf.open(out)
    assert doc.page_count == orig.page_count + 1  # one notes page for 7 claims
    text = doc[0].get_text()
    for word in ("Hover for details", "Highlights mark skills", "Supported", "Partially supported",
                 "Contradicted", "No public evidence"):
        assert word in text


def test_notes_page_lists_every_checked_claim(tmp_path):
    _, out = _run(tmp_path)
    doc = pymupdf.open(out)
    notes = doc[doc.page_count - 1].get_text()
    assert "Verification notes" in notes
    for vc in VERDICT["claims"]:
        if vc["type"] == "unchecked":
            continue
        assert vc["top_reason"].split()[0] in notes
        for link in vc["evidence_links"]:
            assert link in notes
    for name in ("Polytrack-ML", "CAC24", "cubesolver", "Python", "Dart", "JavaScript", "C++"):
        assert name in notes
    assert "employment" in notes and "education" in notes  # unchecked categories line
    assert notes.isascii()
    uris = [lk["uri"] for lk in doc[doc.page_count - 1].get_links() if lk.get("uri")]
    assert "https://github.com/henrychen4736/cubesolver" in uris


def test_marks_drawn_into_page(tmp_path):
    _, out = _run(tmp_path)
    page = pymupdf.open(out)[0]
    fills = {tuple(round(c, 2) for c in d["fill"]) for d in page.get_drawings() if d.get("fill")}
    assert tuple(round(c, 2) for c in annotate.STRONG["supported"]) in fills
    assert tuple(round(c, 2) for c in annotate.STRONG["partial"]) in fills
    assert tuple(round(c, 2) for c in annotate.PASTEL["supported"]) in fills


def test_skill_highlight_inside_source_text_area(tmp_path):
    _, out = _run(tmp_path)
    doc = pymupdf.open(out)
    area = pymupdf.Rect()
    for r in doc[0].search_for(CLAIMS["claims"][3]["source_text"]):
        area |= r
    assert not area.is_empty
    area = area + (-4, -4, 4, 4)
    highlights = [a for a in _annots(doc) if a.kind == "Highlight"]
    assert highlights
    for a in highlights:
        assert area.contains(a.rect), (a.rect, area)


def test_dot_level_with_first_line_in_left_margin(tmp_path):
    _, out = _run(tmp_path)
    doc = pymupdf.open(out)
    first = doc[0].search_for(CLAIMS["claims"][0]["source_text"])[0]
    dot = next(a for a in _annots(doc) if a.kind == "Circle")
    assert dot.rect.x1 < first.x0
    assert abs((dot.rect.y0 + dot.rect.y1) / 2 - (first.y0 + first.y1) / 2) < 1.5


def test_rerun_same_path_is_idempotent_or_clear_error(tmp_path):
    report, out = _run(tmp_path)
    # A second run to the same path either leaves identical bytes or raises AnnotateError;
    # it never silently overwrites.
    before = out.read_bytes()
    try:
        annotate.annotate_pdf(HENRY_PDF, CLAIMS, VERDICT, out)
    except AnnotateError:
        pass
    assert out.read_bytes() == before


# ── not located, flags, labels ───────────────────────────────────────────────

def test_source_text_not_in_pdf_is_not_located_and_on_notes_page(tmp_path):
    claims = copy.deepcopy(CLAIMS)
    claims["claims"].append(_claim("c99", "project", "Zorblax Engine, a quantum database nobody wrote",
                                   repo_name="Zorblax"))
    verdict = copy.deepcopy(VERDICT)
    verdict["claims"].append(_vc("c99", "project", "no_evidence", "No matching repo found",
                                 links=["https://example.com/zorblax"]))
    report, out = _run(tmp_path, claims, verdict)
    assert report["not_located"] == ["c99"]
    assert "c99" not in report["located"]
    doc = pymupdf.open(out)
    assert len(_annots(doc)) == 7  # no mark for c99
    notes = doc[doc.page_count - 1].get_text()
    assert "Zorblax" in notes and "No matching repo found" in notes
    assert "https://example.com/zorblax" in notes
    assert "not found on the page" in notes


def test_retry_with_first_eight_words(tmp_path):
    pdf = _make_pdf(tmp_path / "r.pdf", ["alpha beta gamma delta epsilon zeta eta theta and the rest"])
    claims, verdict = _docs(
        [_claim("c1", "project", "alpha beta gamma delta epsilon zeta eta theta OTHER ENDING",
                repo_name="Alpha")],
        [_vc("c1", "project", "supported")])
    report, _ = _run(tmp_path, claims, verdict, pdf)
    assert report["located"] == ["c1"]


def test_whitespace_and_wrapped_claim_located(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Built a thing that does lots of", fontname="helv", fontsize=11)
    page.insert_text((72, 114), "interesting work across two lines", fontname="helv", fontsize=11)
    pdf = tmp_path / "w.pdf"
    doc.save(pdf)
    claims, verdict = _docs(
        [_claim("c1", "project", "Built a thing  that does lots of\n interesting work across two lines",
                repo_name="Thing")],
        [_vc("c1", "project", "partial")])
    report, out = _run(tmp_path, claims, verdict, pdf)
    assert report["located"] == ["c1"]
    dot = next(a for a in _annots(pymupdf.open(out)) if a.kind == "Circle")
    # Level with the first line, not the middle of the claim.
    assert dot.rect.y1 < 108


def test_skill_word_only_highlighted_inside_its_claim_area(tmp_path):
    pdf = _make_pdf(tmp_path / "s.pdf", [
        "Built Python tools for fun",
        "Languages: Rust, Python",
    ])
    claims, verdict = _docs(
        [_claim("c1", "skill", "Languages: Rust, Python", name="Python", kind="language")],
        [_vc("c1", "skill", "supported")])
    report, out = _run(tmp_path, claims, verdict, pdf)
    assert report["located"] == ["c1"]
    hl = [a for a in _annots(pymupdf.open(out)) if a.kind == "Highlight"]
    assert len(hl) == 1
    assert hl[0].rect.y0 > 105  # second line, not the "Python" on the first line


def test_skill_name_missing_from_its_area_is_not_located(tmp_path):
    pdf = _make_pdf(tmp_path / "s.pdf", ["Languages: Rust, Go"])
    claims, verdict = _docs(
        [_claim("c1", "skill", "Languages: Rust, Go", name="Python", kind="language")],
        [_vc("c1", "skill", "supported")])
    report, _ = _run(tmp_path, claims, verdict, pdf)
    assert report["not_located"] == ["c1"]


def test_multiword_and_punctuated_skill_names(tmp_path):
    pdf = _make_pdf(tmp_path / "s.pdf", ["Tools: Machine Learning, C++, Node.js"])
    claims, verdict = _docs(
        [_claim("c1", "skill", "Tools: Machine Learning, C++, Node.js", name="Machine Learning",
                kind="framework_tool"),
         _claim("c2", "skill", "Tools: Machine Learning, C++, Node.js", name="C++", kind="language"),
         _claim("c3", "skill", "Tools: Machine Learning, C++, Node.js", name="Node.js",
                kind="framework_tool")],
        [_vc("c1", "skill", "no_evidence", "Framework or tool; not checked in v1", links=()),
         _vc("c2", "skill", "supported"), _vc("c3", "skill", "no_evidence",
                                              "Framework or tool; not checked in v1", links=())])
    report, out = _run(tmp_path, claims, verdict, pdf)
    assert report["located"] == ["c1", "c2", "c3"]
    page = pymupdf.open(out)[0]
    fills = {tuple(round(c, 2) for c in d["fill"]) for d in page.get_drawings() if d.get("fill")}
    assert tuple(round(c, 2) for c in annotate.FAINT_GREY) in fills  # unchecked tools: faint grey


def test_flagged_hollow_and_contradicted_marks(tmp_path):
    pdf = _make_pdf(tmp_path / "f.pdf", ["Project Alpha does a thing", "Project Beta does another",
                                         "Project Gamma is third"])
    claims, verdict = _docs(
        [_claim("c1", "project", "Project Alpha does a thing", repo_name="Alpha"),
         _claim("c2", "project", "Project Beta does another", repo_name="Beta"),
         _claim("c3", "oss_contribution", "Project Gamma is third", repo="o/gamma")],
        [_vc("c1", "project", "supported", "Runs, mostly", flags=["run_failed"]),
         _vc("c2", "project", "no_evidence", "No repo found", links=()),
         _vc("c3", "oss_contribution", "contradicted", "No merged PRs")])
    report, out = _run(tmp_path, claims, verdict, pdf)
    assert report["located"] == ["c1", "c2", "c3"]
    doc = pymupdf.open(out)
    by = {a.info["content"].split(":")[0]: a for a in _annots(doc)}
    flagged = next(a for a in _annots(doc) if "WARNING" in a.info["content"])
    assert flagged.info["content"].startswith("WARNING - run failed. Supported: Runs, mostly")
    assert flagged.rect.width > 2 * annotate.DOT_R  # ring is larger than the dot
    assert "No public evidence" in by
    assert by["No public evidence"].info["content"] == "No public evidence: No repo found"
    assert by["Contradicted"].info["content"].startswith("Contradicted: No merged PRs")
    notes = doc[doc.page_count - 1].get_text()
    assert "WARNING - run failed" in notes
    assert "open source contribution" in notes


def test_unchecked_claims_get_no_marks(tmp_path):
    pdf = _make_pdf(tmp_path / "u.pdf", ["Math Tutor, Brightpath"])
    claims, verdict = _docs(
        [_claim("c1", "unchecked", "Math Tutor, Brightpath", category="employment")],
        [_vc("c1", "unchecked", "no_evidence", "Not verifiable from public code", links=())])
    report, out = _run(tmp_path, claims, verdict, pdf)
    assert report == {"out_path": str(out), "located": [], "not_located": []}
    doc = pymupdf.open(out)
    assert _annots(doc) == []
    assert "employment" in doc[doc.page_count - 1].get_text()


def test_emoji_and_non_ascii_stripped_from_pdf_text(tmp_path):
    pdf = _make_pdf(tmp_path / "e.pdf", ["Project Alpha does a thing"])
    claims, verdict = _docs(
        [_claim("c1", "project", "Project Alpha does a thing", repo_name="Alpha")],
        [_vc("c1", "project", "supported", "Great \U0001F7E2 work — really", links=())])
    _, out = _run(tmp_path, claims, verdict, pdf)
    doc = pymupdf.open(out)
    assert "\U0001F7E2" not in doc[doc.page_count - 1].get_text()
    assert _annots(doc)[0].info["content"].isascii()


def test_long_list_spills_onto_more_notes_pages(tmp_path):
    n = 40
    lines = [f"Project number {i} does something" for i in range(n)]
    doc = pymupdf.open()
    page = doc.new_page()
    for i, ln in enumerate(lines[:30]):
        page.insert_text((72, 60 + 20 * i), ln, fontname="helv", fontsize=10)
    pdf = tmp_path / "long.pdf"
    doc.save(pdf)
    claims, verdict = _docs(
        [_claim(f"c{i}", "project", lines[i], repo_name=f"P{i}") for i in range(n)],
        [_vc(f"c{i}", "project", "supported", "A long reason " * 8) for i in range(n)])
    report, out = _run(tmp_path, claims, verdict, pdf)
    assert len(report["located"]) == 30 and len(report["not_located"]) == 10
    result = pymupdf.open(out)
    assert result.page_count >= 3
    all_notes = "".join(result[i].get_text() for i in range(1, result.page_count))
    assert "P39" in all_notes and "P0" in all_notes


# ── errors ───────────────────────────────────────────────────────────────────

def test_not_a_pdf_raises_annotate_error(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"this is not a pdf")
    with pytest.raises(AnnotateError):
        annotate.annotate_pdf(bad, CLAIMS, VERDICT, tmp_path / "o.pdf")
    assert not (tmp_path / "o.pdf").exists()


def test_missing_input_raises_annotate_error(tmp_path):
    with pytest.raises(AnnotateError):
        annotate.annotate_pdf(tmp_path / "nope.pdf", CLAIMS, VERDICT, tmp_path / "o.pdf")
