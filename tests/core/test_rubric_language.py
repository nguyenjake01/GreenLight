"""Framework parts in a project `language` claim ("Dart/Flutter") are not checked as languages."""
from __future__ import annotations

import pytest

from verifier.core import rubric
from tests.core.test_rubric import World, check, only, project, skill, verdict


@pytest.mark.parametrize("text,parts", [
    ("JavaScript (React)", ["JavaScript", "React"]),
    ("Dart/Flutter", ["Dart", "Flutter"]),
    ("Python", ["Python"]),
])
def test_split_language_parts(text, parts):
    assert rubric.split_language_parts(text) == parts


@pytest.mark.parametrize("name", ["Flutter", "flutter", "Next.js", "scikit-learn", "Ruby on Rails", ".NET"])
def test_is_framework(name):
    assert rubric.is_framework(name)


@pytest.mark.parametrize("name", ["Dart", "SQL", "HTML", "CSS", "Shell", "Dockerfile",
                                  "Jupyter Notebook", "Python"])
def test_real_languages_are_not_frameworks(name):
    assert not rubric.is_framework(name)


def test_dart_flutter_passes_on_dart_repo(tmp_path):
    w = World(tmp_path)
    w.repo("proj", languages={"Dart": 530, "C++": 200, "Swift": 270})
    vc = only(verdict(tmp_path, [project(language="Dart/Flutter")], w))
    chk = check(vc, "language")
    assert chk["result"] == "pass"
    assert chk["reason"] == "Dart is the main language"
    assert "Flutter" not in chk["reason"]


def test_python_django_checks_python_only(tmp_path):
    w = World(tmp_path)
    w.repo("proj", languages={"Python": 900, "HTML": 100})
    chk = check(only(verdict(tmp_path, [project(language="Python/Django")], w)), "language")
    assert chk["result"] == "pass" and chk["reason"] == "Python is the main language"


def test_javascript_parenthesized_react(tmp_path):
    w = World(tmp_path)
    w.repo("proj", languages={"JavaScript": 900, "CSS": 100})
    chk = check(only(verdict(tmp_path, [project(language="JavaScript (React)")], w)), "language")
    assert chk["result"] == "pass" and chk["reason"] == "JavaScript is the main language"


def test_framework_only_claim_skips_without_label_effect(tmp_path):
    w = World(tmp_path)
    w.repo("proj", languages={"Dart": 1000})
    base = only(verdict(tmp_path, [project(language=None)], w))
    vc = only(verdict(tmp_path, [project(language="Flutter")], w))
    chk = check(vc, "language")
    assert chk["result"] == "skip"
    assert chk["reason"] == "Flutter is a framework, not a language"
    assert vc["label"] == base["label"]


def test_framework_only_skips_even_when_languages_unavailable(tmp_path):
    w = World(tmp_path)
    w.repo("proj", languages="miss")
    chk = check(only(verdict(tmp_path, [project(language="React")], w)), "language")
    assert chk["result"] == "skip" and chk["reason"] == "React is a framework, not a language"


def test_html_css_still_checked_as_languages(tmp_path):
    w = World(tmp_path)
    w.repo("proj", languages={"HTML": 600, "CSS": 400})
    chk = check(only(verdict(tmp_path, [project(language="HTML/CSS")], w)), "language")
    assert chk["result"] == "pass"
    assert chk["reason"] == "HTML is the main language; CSS is 40% of the code"
    w2 = World(tmp_path / "b")
    w2.repo("proj", languages={"HTML": 1000})
    chk = check(only(verdict(tmp_path, [project(language="HTML/CSS")], w2)), "language")
    assert chk["result"] == "hard" and chk["reason"] == "No CSS code in the repo"


def test_missing_real_language_still_hard_next_to_framework(tmp_path):
    w = World(tmp_path)
    w.repo("proj", languages={"Python": 1000})
    vc = only(verdict(tmp_path, [project(language="Dart/Flutter")], w))
    chk = check(vc, "language")
    assert chk["result"] == "hard" and chk["reason"] == "No Dart code in the repo"
    assert vc["label"] == "contradicted"


# ── Skills ───────────────────────────────────────────────────────────────────

def test_skill_dart_flutter_judged_on_dart_only(tmp_path):
    w = World(tmp_path)
    w.repo("app", languages={"Dart": 900, "Swift": 100})
    vc = only(verdict(tmp_path, [skill("c1", "Dart/Flutter")], w))
    assert vc["label"] == "supported"
    assert "Flutter" not in check(vc, "skill_presence")["reason"]


def test_skill_javascript_parenthesized_react(tmp_path):
    w = World(tmp_path)
    w.repo("site", languages={"JavaScript": 900, "CSS": 100})
    assert only(verdict(tmp_path, [skill("c1", "JavaScript (React)")], w))["label"] == "supported"


def test_skill_missing_real_language_still_no_evidence(tmp_path):
    w = World(tmp_path)
    w.repo("app", languages={"Python": 1000})
    vc = only(verdict(tmp_path, [skill("c1", "Dart/Flutter")], w))
    assert vc["label"] == "no_evidence"
    assert "Dart" in check(vc, "skill_presence")["reason"]


def test_skill_framework_only_language_kind_skips(tmp_path):
    w = World(tmp_path)
    w.repo("app", languages={"Dart": 1000})
    vc = only(verdict(tmp_path, [skill("c1", "Flutter")], w))
    chk = check(vc, "skill_presence")
    assert vc["label"] == "no_evidence" and chk["result"] == "skip"
    assert chk["reason"] == "Flutter is a framework, not checked as a language"


def test_skill_framework_tool_kind_unchanged(tmp_path):
    w = World(tmp_path)
    w.repo("app", languages={"Dart": 1000})
    vc = only(verdict(tmp_path, [skill("c1", "Flutter", kind="framework_tool")], w))
    assert check(vc, "skill_presence")["reason"] == "Framework or tool; not checked in v1"
