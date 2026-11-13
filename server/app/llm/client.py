"""OpenAI 兼容接口的大模型调用：JSON 输出、Pydantic 校验、校验失败重试一次。

`ChatBackend` 是一个最小接口（发消息、拿回文本），测试用假实现替换；
`OpenAIChatBackend` 用官方 `AsyncOpenAI` 指向通义千问 / 智谱的兼容接口。
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Protocol, TypeVar
from urllib.parse import urlsplit

import openai
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


class LlmError(Exception):
    """一次大模型调用失败。`kind` 只是类别，不含正文：timeout / connect / status_<码> / empty / invalid_json / validation / error。"""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


class ChatBackend(Protocol):
    role: str
    model: str

    async def chat(self, messages: list[dict[str, str]], *, timeout_s: float) -> str: ...

    async def aclose(self) -> None: ...


@dataclass(frozen=True)
class Endpoint:
    role: str
    base_url: str
    api_key: str
    model: str


def _extra_body(base_url: str) -> dict:
    # 思考模式要显式关掉，否则一次调用就会超出话术预算。DeepSeek 官方接口已用真实密钥验证（2026-11-12）；
    # 百炼、智谱的参数名取自各自的官方文档，未用真实密钥验证。其他中转站（如 ai.apiclub.top）实测关不掉，不传参数。
    if urlsplit(base_url).hostname == "api.deepseek.com":
        return {"thinking": {"type": "disabled"}}
    if "dashscope" in base_url:
        return {"enable_thinking": False}
    if "bigmodel" in base_url:
        return {"thinking": {"type": "disabled"}}
    return {}


class OpenAIChatBackend:
    def __init__(self, endpoint: Endpoint, *, connect_timeout_s: float, http_client=None):
        self.role = endpoint.role
        self.model = endpoint.model
        self._extra = _extra_body(endpoint.base_url)
        self._connect_timeout_s = connect_timeout_s
        self._client = openai.AsyncOpenAI(
            api_key=endpoint.api_key,
            base_url=endpoint.base_url,
            max_retries=0,
            http_client=http_client,
        )

    async def chat(self, messages: list[dict[str, str]], *, timeout_s: float) -> str:
        # 连接超时比读取超时短：连不上就尽快放弃
        connect = min(self._connect_timeout_s, timeout_s)
        timeout = openai.Timeout(timeout_s, connect=connect)
        try:
            resp = await asyncio.wait_for(
                self._client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    response_format={"type": "json_object"},
                    temperature=0,
                    timeout=timeout,
                    extra_body=self._extra or None,
                ),
                timeout=timeout_s,
            )
        except (asyncio.TimeoutError, openai.APITimeoutError) as exc:
            raise LlmError("timeout") from exc
        except openai.APIStatusError as exc:
            raise LlmError(f"status_{exc.status_code}") from exc
        except openai.APIConnectionError as exc:
            raise LlmError("connect") from exc
        except openai.OpenAIError as exc:
            raise LlmError("error") from exc
        if not resp.choices or not resp.choices[0].message.content:
            raise LlmError("empty")
        return resp.choices[0].message.content

    async def aclose(self) -> None:
        await self._client.close()


_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def parse_json(text: str, model: type[T]) -> T:
    m = _FENCE.match(text)
    if m:
        text = m.group(1)
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise LlmError("invalid_json") from exc
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise LlmError("validation") from exc


RETRY_HINT = "上一次的输出不是符合要求的 JSON。请只输出一个 JSON 对象，字段和取值严格按系统提示里的格式。"


async def call_json(
    backend: ChatBackend,
    messages: list[dict[str, str]],
    model: type[T],
    *,
    timeout_s: float,
    retries: int = 1,
) -> T:
    """调用一次，输出不合格时带提示重试（最多 `retries` 次）。整体不超过 `timeout_s`。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    attempt_messages = list(messages)
    last: LlmError | None = None
    for _ in range(retries + 1):
        remaining = deadline - loop.time()
        if remaining <= 0.05:
            raise last or LlmError("timeout")
        text = await backend.chat(attempt_messages, timeout_s=remaining)
        try:
            return parse_json(text, model)
        except LlmError as exc:
            last = exc
            attempt_messages = [*messages, {"role": "user", "content": RETRY_HINT}]
    assert last is not None
    raise last
