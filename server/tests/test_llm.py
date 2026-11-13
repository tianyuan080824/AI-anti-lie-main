"""大模型客户端：OpenAI 兼容请求的形状、超时、异常归类，JSON 校验与重试一次。全部离线（MockTransport / 假后端）。"""

from __future__ import annotations

import asyncio
import json

import httpx2
import pytest
from pydantic import BaseModel

from app.llm import LlmError, endpoints
from app.llm.client import RETRY_HINT, Endpoint, OpenAIChatBackend, call_json, parse_json
from tests.llm_fakes import FakeBackend, Hang

FAKE_KEY = "sk-test-not-a-real-key"


def completion(content: str | None) -> dict:
    return {
        "id": "x",
        "object": "chat.completion",
        "created": 0,
        "model": "m",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
    }


def make_backend(handler, base_url="https://dashscope.aliyuncs.com/compatible-mode/v1", connect=3.0):
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return OpenAIChatBackend(Endpoint("primary", base_url, FAKE_KEY, "qwen-test"), connect_timeout_s=connect, http_client=client)


async def test_request_shape_json_mode_and_connect_shorter_than_read():
    seen = {}

    def handler(req):
        seen["timeout"] = req.extensions.get("timeout")
        seen["body"] = json.loads(req.content)
        seen["url"] = str(req.url)
        seen["auth"] = req.headers.get("authorization")
        return httpx2.Response(200, json=completion('{"ok": true}'))

    backend = make_backend(handler)
    assert await backend.chat([{"role": "user", "content": "hi"}], timeout_s=8) == '{"ok": true}'
    assert seen["url"].endswith("/chat/completions")
    assert seen["auth"] == f"Bearer {FAKE_KEY}"
    assert seen["body"]["response_format"] == {"type": "json_object"}
    assert seen["body"]["temperature"] == 0
    assert seen["body"]["enable_thinking"] is False  # 百炼：关掉思考模式
    assert seen["timeout"]["connect"] == 3 and seen["timeout"]["read"] == 8
    assert seen["timeout"]["connect"] < seen["timeout"]["read"]
    await backend.aclose()


async def test_connect_timeout_never_exceeds_remaining_budget():
    seen = {}

    def handler(req):
        seen["timeout"] = req.extensions.get("timeout")
        return httpx2.Response(200, json=completion("{}"))

    backend = make_backend(handler, connect=3.0)
    await backend.chat([{"role": "user", "content": "hi"}], timeout_s=1.5)
    assert seen["timeout"]["connect"] == 1.5


async def test_bigmodel_disables_thinking_with_its_own_parameter():
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        return httpx2.Response(200, json=completion("{}"))

    backend = make_backend(handler, base_url="https://open.bigmodel.cn/api/paas/v4/")
    await backend.chat([{"role": "user", "content": "hi"}], timeout_s=5)
    assert seen["body"]["thinking"] == {"type": "disabled"}
    assert "enable_thinking" not in seen["body"]


async def test_official_deepseek_disables_thinking():
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        return httpx2.Response(200, json=completion("{}"))

    backend = make_backend(handler, base_url="https://api.deepseek.com")
    await backend.chat([{"role": "user", "content": "hi"}], timeout_s=5)
    assert seen["body"]["thinking"] == {"type": "disabled"}
    assert "enable_thinking" not in seen["body"]


@pytest.mark.parametrize(
    "base_url",
    ["https://ai.apiclub.top/v1", "https://api.deepseek.com.example.net/v1", "https://proxy.example.net/api.deepseek.com/v1"],
)
async def test_other_hosts_get_no_thinking_parameter(base_url):
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        return httpx2.Response(200, json=completion("{}"))

    backend = make_backend(handler, base_url=base_url)
    await backend.chat([{"role": "user", "content": "hi"}], timeout_s=5)
    assert "thinking" not in seen["body"]
    assert "enable_thinking" not in seen["body"]


@pytest.mark.parametrize(
    "handler,kind",
    [
        (lambda req: httpx2.Response(429, json={"error": {"message": "rate"}}), "status_429"),
        (lambda req: httpx2.Response(500, json={"error": {"message": "x"}}), "status_500"),
        (lambda req: httpx2.Response(401, json={"error": {"message": "bad key"}}), "status_401"),
        (lambda req: httpx2.Response(200, json=completion(None)), "empty"),
        (lambda req: httpx2.Response(200, json=completion("")), "empty"),
    ],
)
async def test_errors_are_classified(handler, kind):
    backend = make_backend(handler)
    with pytest.raises(LlmError) as exc:
        await backend.chat([{"role": "user", "content": "hi"}], timeout_s=5)
    assert exc.value.kind == kind


async def test_connect_error_is_classified():
    def handler(req):
        raise httpx2.ConnectError("unreachable", request=req)

    with pytest.raises(LlmError) as exc:
        await make_backend(handler).chat([{"role": "user", "content": "hi"}], timeout_s=5)
    assert exc.value.kind == "connect"


async def test_read_timeout_is_classified():
    def handler(req):
        raise httpx2.ReadTimeout("slow", request=req)

    with pytest.raises(LlmError) as exc:
        await make_backend(handler).chat([{"role": "user", "content": "hi"}], timeout_s=5)
    assert exc.value.kind == "timeout"


async def test_hard_deadline_even_if_transport_hangs():
    async def handler(req):
        await asyncio.sleep(3600)

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    with pytest.raises(LlmError) as exc:
        await make_backend(handler).chat([{"role": "user", "content": "hi"}], timeout_s=0.3)
    assert exc.value.kind == "timeout"
    assert loop.time() - t0 < 1.0


def test_endpoints_only_for_configured_keys(make_settings):
    assert endpoints(make_settings()) == {}
    eps = endpoints(make_settings(llm_backup_api_key=FAKE_KEY))
    assert set(eps) == {"backup"} and eps["backup"].model == "glm-5.3-flash"
    assert set(endpoints(make_settings(llm_primary_api_key=FAKE_KEY, llm_backup_api_key=FAKE_KEY))) == {"primary", "backup"}


# ---- JSON 解析与重试 ----


class Out(BaseModel):
    a: int


def test_parse_json_accepts_code_fence():
    assert parse_json('```json\n{"a": 1}\n```', Out).a == 1
    assert parse_json('{"a": 2}', Out).a == 2


@pytest.mark.parametrize("text,kind", [("not json", "invalid_json"), ('{"a": "x"}', "validation"), ("[]", "validation")])
def test_parse_json_errors(text, kind):
    with pytest.raises(LlmError) as exc:
        parse_json(text, Out)
    assert exc.value.kind == kind


async def test_call_json_retries_once_with_hint():
    backend = FakeBackend("primary", ["好的，结果如下：a=1", {"a": 1}])
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    assert (await call_json(backend, msgs, Out, timeout_s=5)).a == 1
    assert len(backend.calls) == 2
    assert backend.calls[0] == msgs
    assert backend.calls[1] == [*msgs, {"role": "user", "content": RETRY_HINT}]


async def test_call_json_gives_up_after_one_retry():
    backend = FakeBackend("primary", ["x", '{"a": "y"}', {"a": 3}])
    with pytest.raises(LlmError) as exc:
        await call_json(backend, [{"role": "user", "content": "u"}], Out, timeout_s=5)
    assert exc.value.kind == "validation"
    assert len(backend.calls) == 2


async def test_call_json_does_not_retry_transport_errors():
    backend = FakeBackend("primary", [LlmError("status_500"), {"a": 1}])
    with pytest.raises(LlmError) as exc:
        await call_json(backend, [{"role": "user", "content": "u"}], Out, timeout_s=5)
    assert exc.value.kind == "status_500"
    assert len(backend.calls) == 1


async def test_call_json_shares_one_deadline_across_attempts():
    backend = FakeBackend("primary", ["x", {"a": 1}], delay_s=0.2)
    await call_json(backend, [{"role": "user", "content": "u"}], Out, timeout_s=2)
    assert backend.timeouts[0] == pytest.approx(2, abs=0.05)
    assert backend.timeouts[1] < backend.timeouts[0] - 0.15


async def test_call_json_passes_hang_to_caller_timeout():
    backend = FakeBackend("primary", [Hang()])
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(call_json(backend, [{"role": "user", "content": "u"}], Out, timeout_s=5), 0.2)
