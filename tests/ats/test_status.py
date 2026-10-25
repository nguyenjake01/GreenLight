"""Processing status: the pure reader, the polling endpoint, and the two pages that show it."""
import json
import os
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from verifier.ats import mock_app
from verifier.ats.mock_app import processing_status


@pytest.fixture
def runs(tmp_path, monkeypatch):
    d = tmp_path / "runs"
    d.mkdir()
    monkeypatch.setenv("RUNS_DIR", str(d))
    monkeypatch.setenv("ATS_PUBLIC_URL", "http://ats.test")
    monkeypatch.setenv("GREENHOUSE_WEBHOOK_SECRET", "s")
    return d


@pytest.fixture
def client(runs):
    app = mock_app.create_app(webhook_client=httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"status": "accepted"}))))
    return TestClient(app, base_url="http://ats.test")


def make(runs, app_id="1", *, meta=True, resume=False, claims=None, snapshot=False,
         verdict=False, run_states=None, errors=(), writebacks=(), extraction=None):
    d = runs / app_id
    d.mkdir(exist_ok=True)
    if meta:
        latest = {"claims_version": 1 if claims else None,
                  "snapshot_id": "snap_1" if snapshot else None,
                  "verdict_n": 1 if verdict else None}
        (d / "meta.json").write_text(json.dumps({
            "application_id": app_id, "latest": latest, "runs": run_states or {},
            "stage_errors": list(errors), "writebacks": list(writebacks)}))
    if resume:
        (d / "resume.txt").write_text("resume")
    if claims:
        doc = {"claims": []}
        if extraction:
            doc["extraction"] = {"status": extraction}
        (d / "claims.v1.json").write_text(json.dumps(doc))
    return d


def steps_of(st):
    return {s["label"]: s["status"] for s in st["steps"]}


def test_queued_when_folder_missing_or_bad_id(runs):
    st = processing_status("123")
    assert (st["state"], st["step"], st["progress"]) == ("queued", "received", 0.0)
    assert processing_status("../etc")["state"] == "queued"


@pytest.mark.parametrize("kwargs,step", [
    ({}, "reading resume"),
    ({"resume": True}, "finding claims"),
    ({"resume": True, "claims": True}, "checking github"),
    ({"resume": True, "claims": True, "snapshot": True}, "weighing evidence"),
    ({"resume": True, "claims": True, "snapshot": True, "verdict": True,
      "run_states": {"c1": "done", "c2": "done"}}, "writing back"),
])
def test_each_working_step(runs, kwargs, step):
    make(runs, **kwargs)
    st = processing_status("1")
    assert st["state"] == "working" and st["step"] == step
    sm = steps_of(st)
    assert sm[step] == "current"
    labels = [s["label"] for s in st["steps"]]
    assert labels == ["received", "reading resume", "finding claims", "checking github",
                      "weighing evidence", "running code", "writing back", "done"]
    assert all(sm[l] == "done" for l in labels[:labels.index(step)])
    assert all(sm[l] == "todo" for l in labels[labels.index(step) + 1:])


def test_running_code_counts_and_progress_grows(runs):
    make(runs, resume=True, claims=True, snapshot=True, verdict=True,
         run_states={"c1": "done", "c2": "running", "c3": "queued"})
    st = processing_status("1")
    assert st["step"] == "running code" and st["detail"] == "1 of 3 projects"
    make(runs, resume=True, claims=True, snapshot=True, verdict=True,
         run_states={"c1": "done", "c2": "skipped", "c3": "running"})
    assert processing_status("1")["detail"] == "2 of 3 projects"
    assert 0 < processing_status("1")["progress"] < 1


def test_done_when_writeback_exists(runs):
    make(runs, resume=True, claims=True, snapshot=True, verdict=True,
         run_states={"c1": "done"}, writebacks=[{"note_id": "1"}])
    st = processing_status("1")
    assert st["state"] == "done" and st["progress"] == 1.0
    assert all(s["status"] == "done" for s in st["steps"])


def test_failed_via_stage_error_and_via_extraction(runs):
    make(runs, "1", resume=True, errors=[{"stage": "extract", "error": "x"}])
    st = processing_status("1")
    assert st["state"] == "failed" and st["detail"] == "couldn't read the resume"
    make(runs, "2", resume=True, claims=True, extraction="failed")
    assert processing_status("2")["state"] == "failed"
    make(runs, "3", errors=[{"stage": "resume", "error": "x"}])
    assert processing_status("3")["state"] == "failed"


def test_other_stage_errors_do_not_fail(runs):
    make(runs, resume=True, claims=True, snapshot=True,
         errors=[{"stage": "github", "error": "rate limited"}])
    st = processing_status("1")
    assert st["state"] == "working" and st["detail"] is None


def test_stalled_keeps_working(runs):
    d = make(runs, resume=True, errors=[{"stage": "github", "error": "boom"}])
    old = time.time() - 20 * 60
    os.utime(d / "meta.json", (old, old))
    st = processing_status("1")
    assert st["state"] == "working" and st["detail"].startswith("stalled")


def test_corrupt_or_partial_files_do_not_raise(runs):
    d = make(runs, meta=False)
    (d / "meta.json").write_text("{not json")
    assert processing_status("1")["state"] == "queued"
    (d / "meta.json").write_text("[]")
    assert processing_status("1")["state"] == "queued"
    (d / "meta.json").write_text(json.dumps({"latest": None, "runs": None, "stage_errors": "x",
                                              "writebacks": None}))
    (d / "claims.v1.json").write_text("garbage")
    (d / "resume.txt").write_text("r")
    assert processing_status("1")["state"] == "working"


def test_endpoint_parses_ids_and_caps(client, runs):
    make(runs, "7", resume=True, claims=True, snapshot=True, verdict=True,
         run_states={"c": "done"}, writebacks=[{"note_id": "1"}])
    r = client.get("/_mock/status?ids=7,8, 9,abc,../x,7")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"7", "8", "9"}
    assert body["7"]["state"] == "done" and body["8"]["state"] == "queued"
    assert client.get("/_mock/status").json() == {}
    many = ",".join(str(i) for i in range(1, 80))
    assert len(client.get(f"/_mock/status?ids={many}").json()) == 50


def apply(client):
    return client.post("/_mock/apply", json={"fixture": "henry"}).json()


def test_index_renders_status_chip(client, runs):
    ids = apply(client)
    text = client.get("/").text
    assert "<th>Status</th>" in text and "queued" in text
    assert "rv-indet" in text and "/static/rv/status.js" in text
    make(runs, str(ids["application_id"]), resume=True, claims=True, snapshot=True,
         verdict=True, run_states={"c1": "done", "c2": "running", "c3": "queued"})
    text = client.get("/").text
    assert "running code · 1 of 3" in text and "rv-spin" in text
    make(runs, str(ids["application_id"]), resume=True, claims=True, snapshot=True,
         verdict=True, run_states={"c1": "done"}, writebacks=[{"note_id": "1"}])
    text = client.get("/").text
    assert "report ready" in text and "rv-indet" not in text
    make(runs, str(ids["application_id"]), resume=True, errors=[{"stage": "extract", "error": "x"}])
    assert "couldn&#39;t read resume" in client.get("/").text or "couldn't read resume" in client.get("/").text


def page(client, ids):
    return client.get(f"/candidates/{ids['candidate_id']}?application_id={ids['application_id']}").text


def test_candidate_page_status_card(client, runs):
    ids = apply(client)
    aid = str(ids["application_id"])
    make(runs, aid, resume=True, claims=True)
    text = page(client, ids)
    assert "Resume Verifier is checking this resume" in text
    assert "checking github" in text and "data-rv-steps" in text
    assert "/static/rv/status.js" in text
    make(runs, aid, resume=True, claims=True, snapshot=True, verdict=True,
         run_states={"c": "done"}, writebacks=[{"note_id": "1"}])
    text = page(client, ids)
    assert "data-rv-panel" not in text and "status.js" not in text
    make(runs, aid, resume=True, errors=[{"stage": "extract", "error": "x"}])
    text = page(client, ids)
    assert "Couldn't read the resume" in text or "Couldn&#39;t read the resume" in text
    assert "status.js" not in text


@pytest.mark.parametrize("state", ["working", "done", "failed"])
def test_candidate_page_forbidden_strings(client, runs, state):
    ids = apply(client)
    aid = str(ids["application_id"])
    if state == "working":
        make(runs, aid, resume=True, claims=True, snapshot=True, verdict=True,
             run_states={"c": "running"})
    elif state == "done":
        make(runs, aid, resume=True, claims=True, snapshot=True, verdict=True,
             run_states={"c": "done"}, writebacks=[{"note_id": "1"}])
    else:
        make(runs, aid, resume=True, errors=[{"stage": "extract", "error": "x"}])
    text = page(client, ids).lower()
    assert "score" not in text and "fraud" not in text and "tag" not in text.replace("stage", "")
    assert "greenhouse" not in text
    js = client.get("/static/rv/status.js").text.lower()
    assert "score" not in js and "greenhouse" not in js
