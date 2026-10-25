"""Offline tests for verifier.stack.slack: a fake WebClient checks each call's name and args."""
from __future__ import annotations

from pathlib import Path

import pytest
from slack_sdk.errors import SlackApiError, SlackClientError
from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler
from slack_sdk.web import SlackResponse

from verifier.contracts.errors import SlackError
from verifier.stack import slack

MSG = {"text": "fallback text", "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}]}
PARENT = {"channel": "C123", "ts": "1700000000.000100"}


class FakeClient:
    def __init__(self, responses=None, raises=None):
        self.calls: list[tuple[str, dict]] = []
        self.responses = responses or {}
        self.raises = raises or {}

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def method(**kwargs):
            self.calls.append((name, kwargs))
            if name in self.raises:
                raise self.raises[name]
            return self.responses.get(name, {})
        return method


@pytest.fixture
def fake(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(slack, "_client", client)
    return client


def api_error(code):
    resp = SlackResponse(client=None, http_verb="POST", api_url="", req_args={}, data={"ok": False, "error": code},
                         headers={}, status_code=200)
    return SlackApiError(f"The request to the Slack API failed. ({code})", resp)


def test_post_card(fake):
    fake.responses["chat_postMessage"] = {"ok": True, "channel": "C123", "ts": "1.1"}
    assert slack.post_card("C123", MSG) == {"channel": "C123", "ts": "1.1"}
    assert fake.calls == [("chat_postMessage", {"channel": "C123", "text": "fallback text",
                                                "blocks": MSG["blocks"]})]


def test_post_card_blank_channel_uses_env(fake, monkeypatch):
    monkeypatch.setenv("SLACK_CHANNEL_ID", "CENV")
    fake.responses["chat_postMessage"] = {"ts": "1.1"}
    assert slack.post_card("", MSG) == {"channel": "CENV", "ts": "1.1"}
    assert fake.calls[0][1]["channel"] == "CENV"


def test_update_card(fake):
    assert slack.update_card(PARENT, MSG) is None
    assert fake.calls == [("chat_update", {"channel": "C123", "ts": PARENT["ts"], "text": "fallback text",
                                           "blocks": MSG["blocks"]})]


def test_post_thread(fake):
    fake.responses["chat_postMessage"] = {"channel": "C123", "ts": "2.2"}
    assert slack.post_thread(PARENT, MSG) == {"channel": "C123", "ts": "2.2"}
    assert fake.calls == [("chat_postMessage", {"channel": "C123", "thread_ts": PARENT["ts"],
                                                "text": "fallback text", "blocks": MSG["blocks"]})]


def test_text_always_sent(fake):
    fake.responses["chat_postMessage"] = {"ts": "1.1"}
    slack.post_card("C123", MSG)
    slack.post_thread(PARENT, MSG)
    slack.update_card(PARENT, MSG)
    assert len(fake.calls) == 3
    assert all(kw["text"] == "fallback text" for _, kw in fake.calls)


def test_upload_file_permalink_from_response(fake, tmp_path):
    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    fake.responses["files_upload_v2"] = {"file": {"id": "F1", "permalink": "https://x/F1"}}
    ref = slack.upload_file(PARENT, pdf, title="Annotated", comment="here")
    assert ref == {"file_id": "F1", "permalink": "https://x/F1"}
    assert fake.calls == [("files_upload_v2", {"channel": "C123", "thread_ts": PARENT["ts"],
                                               "file": str(pdf), "title": "Annotated",
                                               "initial_comment": "here"})]


def test_upload_file_no_comment_omits_initial_comment(fake, tmp_path):
    fake.responses["files_upload_v2"] = {"file": {"id": "F1", "permalink": "p"}}
    slack.upload_file(PARENT, tmp_path / "r.pdf", title="T")
    assert "initial_comment" not in fake.calls[0][1]


def test_upload_file_permalink_falls_back_to_files_info(fake, tmp_path):
    fake.responses["files_upload_v2"] = {"files": [{"id": "F9"}]}
    fake.responses["files_info"] = {"file": {"id": "F9", "permalink": "https://x/F9"}}
    ref = slack.upload_file(PARENT, tmp_path / "r.pdf", title="T")
    assert ref == {"file_id": "F9", "permalink": "https://x/F9"}
    assert [c[0] for c in fake.calls] == ["files_upload_v2", "files_info"]
    assert fake.calls[1][1] == {"file": "F9"}


def test_upload_file_no_permalink_anywhere(fake, tmp_path):
    fake.responses["files_upload_v2"] = {"file": {"id": "F9"}}
    fake.responses["files_info"] = {"file": {"id": "F9"}}
    with pytest.raises(SlackError):
        slack.upload_file(PARENT, tmp_path / "r.pdf", title="T")


def test_get_permalink(fake):
    fake.responses["chat_getPermalink"] = {"permalink": "https://x/p1"}
    assert slack.get_permalink(PARENT) == "https://x/p1"
    assert fake.calls == [("chat_getPermalink", {"channel": "C123", "message_ts": PARENT["ts"]})]


def test_open_modal(fake):
    view = {"type": "modal", "callback_id": "rv_edit", "blocks": []}
    assert slack.open_modal("T1", view) is None
    assert fake.calls == [("views_open", {"trigger_id": "T1", "view": view})]


def test_slack_api_error_carries_code(fake):
    fake.raises["chat_postMessage"] = api_error("channel_not_found")
    with pytest.raises(SlackError) as ei:
        slack.post_card("C123", MSG)
    assert ei.value.code == "channel_not_found"
    assert "channel_not_found" in str(ei.value)


@pytest.mark.parametrize("fn,args", [
    (slack.update_card, (PARENT, MSG)),
    (slack.post_thread, (PARENT, MSG)),
    (slack.get_permalink, (PARENT,)),
    (slack.open_modal, ("T1", {})),
])
def test_every_function_maps_errors(fake, fn, args):
    for name in ("chat_update", "chat_postMessage", "chat_getPermalink", "views_open"):
        fake.raises[name] = api_error("invalid_auth")
    with pytest.raises(SlackError) as ei:
        fn(*args)
    assert ei.value.code == "invalid_auth"


def test_upload_error_mapped(fake, tmp_path):
    fake.raises["files_upload_v2"] = api_error("missing_scope")
    with pytest.raises(SlackError) as ei:
        slack.upload_file(PARENT, tmp_path / "r.pdf", title="T")
    assert ei.value.code == "missing_scope"


def test_client_error_mapped(fake):
    fake.raises["chat_update"] = SlackClientError("boom")
    with pytest.raises(SlackError) as ei:
        slack.update_card(PARENT, MSG)
    assert ei.value.code == "client_error"


def test_missing_ts_is_error(fake):
    fake.responses["chat_postMessage"] = {"ok": True}
    with pytest.raises(SlackError):
        slack.post_card("C123", MSG)


def test_no_token_raises_slack_error(monkeypatch):
    monkeypatch.setattr(slack, "_client", None)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "")
    with pytest.raises(SlackError) as ei:
        slack.post_card("C123", MSG)
    assert ei.value.code == "not_configured"


def test_client_has_rate_limit_retry_handler(monkeypatch):
    monkeypatch.setattr(slack, "_client", None)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake")
    client = slack._get_client()
    assert client is slack._get_client()  # one module-level client
    handlers = [h for h in client.retry_handlers if isinstance(h, RateLimitErrorRetryHandler)]
    assert len(handlers) == 1
    assert handlers[0].max_retry_count == 2
    monkeypatch.setattr(slack, "_client", None)


def test_no_socket_mode_import():
    src = Path(slack.__file__).read_text()
    assert "socket_mode" not in src.lower() and "SocketMode" not in src


def test_text_only_message_omits_blocks(fake):
    fake.responses["chat_postMessage"] = {"ts": "1.1"}
    slack.post_thread(PARENT, {"text": "just text", "blocks": []})
    assert fake.calls == [("chat_postMessage", {"channel": "C123", "thread_ts": PARENT["ts"],
                                                "text": "just text"})]
