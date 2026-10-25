"""The mock ATS warns when its stack has Slack stubbed (VERIFIER_REAL without `slack`)."""
from fastapi.testclient import TestClient

from verifier.ats.mock_app import create_app


def _page(monkeypatch, tmp_path, real: str | None, path: str = "/") -> str:
    monkeypatch.setenv("RUNS_DIR", str(tmp_path))
    if real is None:
        monkeypatch.delenv("VERIFIER_REAL", raising=False)
    else:
        monkeypatch.setenv("VERIFIER_REAL", real)
    return TestClient(create_app(state_root=tmp_path / "_mock_ats")).get(path).text


def test_stub_stack_shows_the_warning(monkeypatch, tmp_path):
    for real in (None, "", "llm,extract,render"):
        text = _page(monkeypatch, tmp_path, real)
        assert "Slack is off on this stack" in text and "8101" in text


def test_real_slack_stack_has_no_warning(monkeypatch, tmp_path):
    for path in ("/", "/apply"):
        text = _page(monkeypatch, tmp_path, "llm, extract, slack", path)
        assert "Slack is off" not in text and "Demo build" in text
