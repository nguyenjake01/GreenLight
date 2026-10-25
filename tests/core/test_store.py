import json
import re
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest

from verifier.contracts.errors import ResumeError, WriteOnceError
from verifier.core import resume, store

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
CLAIMS = json.loads((FIXTURES / "claims" / "henrychen4736.json").read_text())
VERDICT = json.loads((FIXTURES / "verdicts" / "henrychen4736.json").read_text())


@pytest.fixture
def runs(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path / "runs"))
    return tmp_path / "runs"


# ── canonical JSON, hashes, time ─────────────────────────────────────────────

def test_dump_json_is_canonical():
    data = store.dump_json({"b": 1, "a": "é", "c": [1, 2]})
    assert data == '{\n  "a": "é",\n  "b": 1,\n  "c": [\n    1,\n    2\n  ]\n}\n'.encode("utf-8")


def test_hashes():
    assert store.sha256_bytes(b"abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    obj = {"x": 1}
    assert store.sha256_json(obj) == store.sha256_bytes(store.dump_json(obj))
    assert store.sha256_json({"a": 1, "b": 2}) == store.sha256_json({"b": 2, "a": 1})


def test_now_iso_format():
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", store.now_iso())


def test_run_dir_created_under_runs_dir(runs):
    d = store.run_dir("12345")
    assert d == runs / "12345" and d.is_dir()
    assert store.run_dir("12345") == d  # idempotent


# ── write-once and atomic ────────────────────────────────────────────────────

def test_write_bytes_and_write_once(tmp_path):
    p = tmp_path / "a" / "b.bin"
    digest = store.write_bytes(p, b"hello")
    assert digest == store.sha256_bytes(b"hello")
    assert p.read_bytes() == b"hello"
    assert store.write_bytes(p, b"hello") == digest  # identical bytes: no-op
    with pytest.raises(WriteOnceError):
        store.write_bytes(p, b"different")
    assert p.read_bytes() == b"hello"


def test_write_leaves_no_temp_files(tmp_path):
    store.write_bytes(tmp_path / "x.bin", b"1")
    store.write_bytes(tmp_path / "x.bin", b"1")
    with pytest.raises(WriteOnceError):
        store.write_bytes(tmp_path / "x.bin", b"2")
    assert [p.name for p in tmp_path.iterdir()] == ["x.bin"]


def test_write_text_utf8(tmp_path):
    p = tmp_path / "t.txt"
    digest = store.write_text(p, "héllo\n")
    assert p.read_bytes() == "héllo\n".encode("utf-8")
    assert digest == store.sha256_bytes("héllo\n".encode("utf-8"))


def test_write_json_roundtrip_and_hash(tmp_path):
    p = tmp_path / "resume_links.json"  # not validated by name
    obj = {"links": ["https://github.com/x"], "n": 1}
    digest = store.write_json(p, obj)
    assert digest == store.sha256_json(obj)
    assert store.read_json(p) == obj
    assert p.read_bytes() == store.dump_json(obj)
    assert store.write_json(p, obj) == digest
    with pytest.raises(WriteOnceError):
        store.write_json(p, {"links": []})


def test_file_mode_is_readable(tmp_path):
    p = tmp_path / "m.txt"
    store.write_text(p, "x")
    assert p.stat().st_mode & 0o777 == 0o644


# ── validation by file name ──────────────────────────────────────────────────

@pytest.mark.parametrize("rel, expected", [
    ("claims.v1.json", "claims"), ("claims.v12.json", "claims"),
    ("snapshots/snap_1/evidence.json", "evidence"),
    ("snapshots/snap_1/run_traces/c3.json", "run_trace"),
    ("verdicts/verdict_3.json", "verdict"),
    ("generated/c3-more_context-20261025T141002Z.json", "generated"),
    ("webhook.json", None), ("webhook_headers.json", None), ("resume_links.json", None),
    ("meta.json", None), ("snapshots/snap_1/raw/abc.json", None),
])
def test_schema_name_for(rel, expected):
    assert store.schema_name_for(Path(rel)) == expected


def test_write_json_validates_claims(tmp_path):
    good = tmp_path / "claims.v1.json"
    store.write_json(good, CLAIMS)
    bad = tmp_path / "claims.v2.json"
    broken = json.loads(json.dumps(CLAIMS))
    del broken["claims"][0]["source_text"]
    with pytest.raises(jsonschema.ValidationError):
        store.write_json(bad, broken)
    assert not bad.exists()


def test_write_json_validates_verdict_and_trace_and_evidence(tmp_path):
    store.write_json(tmp_path / "verdicts" / "verdict_1.json", VERDICT)
    bad = dict(VERDICT, claims=[dict(VERDICT["claims"][0], label="great")])
    with pytest.raises(jsonschema.ValidationError):
        store.write_json(tmp_path / "verdicts" / "verdict_2.json", bad)
    trace = json.loads((FIXTURES / "run_traces" / "c3.json").read_text())
    store.write_json(tmp_path / "snap" / "run_traces" / "c3.json", trace)
    with pytest.raises(jsonschema.ValidationError):
        store.write_json(tmp_path / "snap2" / "run_traces" / "c3.json", {"claim_id": "c3"})
    with pytest.raises(jsonschema.ValidationError):
        store.write_json(tmp_path / "snap" / "evidence.json", {"schema_version": "1"})


def test_validate_by_name():
    store.validate({"repo": "none"}, "repo_match")
    with pytest.raises(jsonschema.ValidationError):
        store.validate({"repo": "other"}, "repo_match")
    with pytest.raises(ValueError):
        store.validate({}, "no_such_schema")


def test_claim_assertions_follow_type():
    broken = json.loads(json.dumps(CLAIMS))
    broken["claims"][3]["assertions"] = {"category": "education"}  # a skill claim
    with pytest.raises(jsonschema.ValidationError):
        store.validate(broken, "claims")


# ── version counters ─────────────────────────────────────────────────────────

def test_next_versions(tmp_path):
    assert store.next_claims_version(tmp_path) == 1
    assert store.next_verdict_n(tmp_path) == 1
    (tmp_path / "claims.v1.json").write_text("{}")
    (tmp_path / "claims.v2.json").write_text("{}")
    (tmp_path / "claims.v10.json").write_text("{}")
    (tmp_path / "verdicts").mkdir()
    (tmp_path / "verdicts" / "verdict_1.json").write_text("{}")
    (tmp_path / "verdicts" / "verdict_9.json").write_text("{}")
    assert store.next_claims_version(tmp_path) == 11
    assert store.next_verdict_n(tmp_path) == 10


# ── read_resume ──────────────────────────────────────────────────────────────

def test_read_resume_henry():
    r = resume.read_resume(FIXTURES / "resumes" / "henry.pdf")
    assert set(r) == {"text", "links", "sha256", "page_count"}
    assert r["page_count"] == 1
    assert "\f" not in r["text"]
    assert "Built cubesolver, a full Rubik's cube solver in C++" in r["text"]
    assert r["links"] == [{"uri": "https://github.com/henrychen4736",
                           "anchor_text": "github.com/henrychen4736", "page": 0}]
    assert r["sha256"] == store.sha256_bytes((FIXTURES / "resumes" / "henry.pdf").read_bytes())


def test_read_resume_errors(tmp_path):
    with pytest.raises(ResumeError):
        resume.read_resume(tmp_path / "missing.pdf")
    junk = tmp_path / "junk.pdf"
    junk.write_bytes(b"not a pdf")
    with pytest.raises(ResumeError):
        resume.read_resume(junk)


def test_read_resume_no_text(tmp_path):
    import pymupdf
    doc = pymupdf.open()
    doc.new_page()
    p = tmp_path / "blank.pdf"
    doc.save(str(p))
    with pytest.raises(ResumeError, match="no text"):
        resume.read_resume(p)


def test_resume_cli_prints_canonical_resume_text():
    proc = subprocess.run([sys.executable, "-m", "verifier.core.resume",
                           str(FIXTURES / "resumes" / "henry.pdf")],
                          capture_output=True, check=True)
    assert proc.stdout == store.dump_json(json.loads(proc.stdout))
    assert json.loads(proc.stdout)["links"][0]["uri"] == "https://github.com/henrychen4736"
