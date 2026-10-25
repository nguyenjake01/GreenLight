"""Slack Web API transport (design doc §8.3 `slack`, module H3).

Plain `slack_sdk.WebClient` calls with the bot token of OpenClaw's Slack app. Never opens a
Socket Mode connection (§5.2): OpenClaw holds the app's only one.
Scopes needed: chat:write, files:write, files:read.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError, SlackClientError
from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler

from verifier.contracts.config import env
from verifier.contracts.errors import SlackError
from verifier.contracts.types import MessageRef, SlackMessage, UploadRef

_client: WebClient | None = None


def _get_client() -> WebClient:
    """The one module-level client, built on first use (so importing never needs the token)."""
    global _client
    if _client is None:
        token = env("SLACK_BOT_TOKEN")
        if not token:
            raise _error("client", "not_configured", "SLACK_BOT_TOKEN is not set")
        client = WebClient(token=token)
        client.retry_handlers.append(RateLimitErrorRetryHandler(max_retry_count=2))
        _client = client
    return _client


def _error(call: str, code: str, detail: str | None = None) -> SlackError:
    err = SlackError(f"slack {call} failed: {code}" + (f" ({detail})" if detail else ""))
    err.code = code  # Slack's error code, e.g. "channel_not_found"
    return err


def _call(name: str, **kwargs: Any) -> Any:
    """Run one WebClient method; map every failure to SlackError carrying Slack's error code."""
    try:
        return getattr(_get_client(), name)(**kwargs)
    except SlackError:
        raise
    except SlackApiError as e:
        resp = e.response
        code = (resp.get("error") if resp is not None else None) or "unknown_error"
        raise _error(name, str(code)) from e
    except SlackClientError as e:
        raise _error(name, "client_error", str(e)) from e
    except OSError as e:  # connection problems that slack_sdk did not wrap
        raise _error(name, "network_error", str(e)) from e


def _ref(call: str, resp: Any, fallback_channel: str) -> MessageRef:
    ts = resp.get("ts")
    if not ts:
        raise _error(call, "missing_ts")
    return {"channel": resp.get("channel") or fallback_channel, "ts": ts}


def _blocks(msg: SlackMessage) -> dict[str, Any]:
    """`blocks` kwarg for a new message; omitted when empty (text-only message)."""
    return {"blocks": msg["blocks"]} if msg.get("blocks") else {}


def post_card(channel: str, msg: SlackMessage) -> MessageRef:
    channel = channel or env("SLACK_CHANNEL_ID")
    resp = _call("chat_postMessage", channel=channel, text=msg["text"], **_blocks(msg))
    return _ref("chat_postMessage", resp, channel)


def update_card(ref: MessageRef, msg: SlackMessage) -> None:
    _call("chat_update", channel=ref["channel"], ts=ref["ts"], text=msg["text"],
          blocks=msg["blocks"])


def post_thread(parent: MessageRef, msg: SlackMessage) -> MessageRef:
    resp = _call("chat_postMessage", channel=parent["channel"], thread_ts=parent["ts"],
                 text=msg["text"], **_blocks(msg))
    return _ref("chat_postMessage", resp, parent["channel"])


def upload_file(parent: MessageRef, path: Path, *, title: str,
                comment: str | None = None) -> UploadRef:
    kwargs: dict[str, Any] = {"channel": parent["channel"], "thread_ts": parent["ts"],
                              "file": str(path), "title": title}
    if comment:
        kwargs["initial_comment"] = comment
    resp = _call("files_upload_v2", **kwargs)
    f = resp.get("file")
    if not f and resp.get("files"):
        f = resp["files"][0]
    f = f or {}
    file_id = f.get("id")
    if not file_id:
        raise _error("files_upload_v2", "missing_file_id")
    permalink = f.get("permalink")
    if not permalink:  # not always in the upload response
        info = _call("files_info", file=file_id)
        permalink = (info.get("file") or {}).get("permalink")
        if not permalink:
            raise _error("files_info", "missing_permalink")
    return {"file_id": file_id, "permalink": permalink}


def get_permalink(ref: MessageRef) -> str:
    resp = _call("chat_getPermalink", channel=ref["channel"], message_ts=ref["ts"])
    link = resp.get("permalink")
    if not link:
        raise _error("chat_getPermalink", "missing_permalink")
    return link


def open_modal(trigger_id: str, view: dict) -> None:
    _call("views_open", trigger_id=trigger_id, view=view)
