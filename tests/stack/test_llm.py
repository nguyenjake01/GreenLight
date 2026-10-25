import json

import httpx
import pytest

from verifier.contracts.errors import LLMError, LLMSchemaError
from verifier.stack import llm

SCHEMA = {"title": "thing", "type": "object",
          "properties": {"n": {"type": "integer"}}, "required": ["n"],
          "additionalProperties": False}
MSGS = [{"role": "user", "content": "hi"}]


def _completion(content, model="served-model", usage=(11, 7)):
    return {"model": model, "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1]}}


@pytest.fixture
def server(monkeypatch):
    """Install a MockTransport client; `server.handler` is set by each test, `server.requests` logs."""
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.test/v1")
    monkeypatch.setenv("LLM_MODEL", "cfg-model")

    class S:
        requests: list[httpx.Request] = []
        handler = staticmethod(lambda req: httpx.Response(200, json=_completion('{"n": 1}')))

    s = S()
    s.requests = []

    def handle(req):
        s.requests.append(req)
        return s.handler(req)

    monkeypatch.setattr(llm, "_client", httpx.Client(transport=httpx.MockTransport(handle)))
    return s


def test_valid_schema_reply(server):
    r = llm.llm_chat(MSGS, SCHEMA, max_tokens=50)
    assert r["json"] == {"n": 1}
    assert r["text"] == '{"n": 1}'
    assert r["model"] == "served-model"
    assert r["usage"] == {"prompt_tokens": 11, "completion_tokens": 7}
    assert r["duration_s"] >= 0
    (req,) = server.requests
    assert str(req.url) == "http://llm.test/v1/chat/completions"
    body = json.loads(req.content)
    assert body["model"] == "cfg-model"
    assert body["messages"] == MSGS
    assert body["temperature"] == 0.0
    assert body["max_tokens"] == 50
    assert body["response_format"] == {
        "type": "json_schema", "json_schema": {"name": "thing", "schema": SCHEMA, "strict": True}}
    # reasoning off by default: LLM_NO_THINK_EXTRA merged in
    assert body["chat_template_kwargs"] == {"enable_thinking": False}


def test_no_schema_and_reasoning_on(server):
    server.handler = lambda req: httpx.Response(200, json=_completion("plain text"))
    r = llm.llm_chat(MSGS, reasoning=True)
    assert r["text"] == "plain text" and r["json"] is None
    body = json.loads(server.requests[0].content)
    assert "response_format" not in body
    assert "chat_template_kwargs" not in body


def test_no_think_extra_is_configurable(server, monkeypatch):
    monkeypatch.setenv("LLM_NO_THINK_EXTRA", '{"foo": {"bar": 1}, "temperature": 0.5}')
    llm.llm_chat(MSGS, SCHEMA)
    body = json.loads(server.requests[0].content)
    assert body["foo"] == {"bar": 1}


@pytest.mark.parametrize("content", [
    '<think>let me think {"n": 99}</think>\n{"n": 2}',
    '<think>\nmulti\nline\n</think>{"n": 2}',
    'dangling reasoning</think>\n{"n": 2}',
])
def test_think_block_stripped(server, content):
    server.handler = lambda req: httpx.Response(200, json=_completion(content))
    r = llm.llm_chat(MSGS, SCHEMA)
    assert r["json"] == {"n": 2}
    assert r["text"] == '{"n": 2}'


def test_bad_json_raises_schema_error_with_text(server):
    server.handler = lambda req: httpx.Response(200, json=_completion('{"n": '))
    with pytest.raises(LLMSchemaError) as ei:
        llm.llm_chat(MSGS, SCHEMA)
    assert ei.value.text == '{"n":'


def test_schema_invalid_reply_raises_schema_error(server):
    server.handler = lambda req: httpx.Response(200, json=_completion('{"n": "one"}'))
    with pytest.raises(LLMSchemaError) as ei:
        llm.llm_chat(MSGS, SCHEMA)
    assert ei.value.text == '{"n": "one"}'


def test_schema_error_is_an_llm_error():
    assert issubclass(LLMSchemaError, LLMError)


def test_http_500_raises_llm_error(server):
    server.handler = lambda req: httpx.Response(500, text="boom")
    with pytest.raises(LLMError) as ei:
        llm.llm_chat(MSGS, SCHEMA)
    assert not isinstance(ei.value, LLMSchemaError)
    assert "500" in str(ei.value)
    assert len(server.requests) == 1  # no retries


def test_timeout_raises_llm_error(server):
    def boom(req):
        raise httpx.ReadTimeout("slow", request=req)

    server.handler = boom
    with pytest.raises(LLMError) as ei:
        llm.llm_chat(MSGS, SCHEMA, timeout_s=1)
    assert not isinstance(ei.value, LLMSchemaError)
    assert "timed out" in str(ei.value)


def test_connection_error_raises_llm_error(server):
    def boom(req):
        raise httpx.ConnectError("refused", request=req)

    server.handler = boom
    with pytest.raises(LLMError):
        llm.llm_chat(MSGS)


def test_malformed_envelope_raises_llm_error(server):
    server.handler = lambda req: httpx.Response(200, json={"choices": []})
    with pytest.raises(LLMError):
        llm.llm_chat(MSGS)


def test_logs_model_duration_tokens(server, caplog):
    with caplog.at_level("INFO", logger="verifier.stack.llm"):
        llm.llm_chat(MSGS, SCHEMA)
    line = caplog.text
    assert "served-model" in line and "prompt_tokens=11" in line and "completion_tokens=7" in line


def test_tls_verification_never_disabled():
    verify = llm._verify()
    assert verify is not False
