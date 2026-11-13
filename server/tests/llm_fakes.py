"""打桩的大模型（实现 `app.llm.ChatBackend`），测试里不发任何网络请求。

- `FakeBackend(role, replies)`：按顺序返回 `replies` 里的东西。元素可以是字符串（原样返回）、dict（转 JSON）、
  异常实例（抛出）、`Hang`（一直等到被取消）或可调用对象 `f(messages) -> 上面任一种`。
- `quote_reply(scene, stage, [(intent, quote), ...])`：生成一个可调用对象，按引文在 `<chat_data>` 里找到所在行，
  填上 `line_id`，模拟一个「好好按格式回答」的模型。找不到的引文照样返回（line_id 用 "9-9"），用来测回查。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable


@dataclass
class Hang:
    """模拟一直不返回的调用。"""


@dataclass
class FakeBackend:
    role: str
    replies: list[Any] = field(default_factory=list)
    model: str = ""
    delay_s: float = 0.0
    calls: list[list[dict[str, str]]] = field(default_factory=list)
    timeouts: list[float] = field(default_factory=list)
    closed: bool = False

    def __post_init__(self) -> None:
        if not self.model:
            self.model = f"fake-{self.role}"

    async def chat(self, messages: list[dict[str, str]], *, timeout_s: float) -> str:
        self.calls.append(messages)
        self.timeouts.append(timeout_s)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        reply: Any = self.replies[min(len(self.calls), len(self.replies)) - 1] if self.replies else Hang()
        if callable(reply) and not isinstance(reply, (Hang, BaseException)):
            reply = reply(messages)
        if isinstance(reply, Hang):
            await asyncio.sleep(3600)
        if isinstance(reply, BaseException):
            raise reply
        if isinstance(reply, (dict, list)):
            return json.dumps(reply, ensure_ascii=False)
        return str(reply)

    async def aclose(self) -> None:
        self.closed = True


def chat_rows(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    """从发给模型的消息里取回 `<chat_data>` 里的每一行。"""
    user = messages[-1]["content"] if messages[-1]["role"] == "user" and "<chat_data>" in messages[-1]["content"] else messages[1]["content"]
    body = user.split("<chat_data>\n", 1)[1].split("\n</chat_data>", 1)[0]
    return [json.loads(row) for row in body.splitlines() if row.strip()]


def quote_reply(
    scene: str,
    stage: str | None,
    intents: Iterable[tuple[str, str]],
    *,
    summary: str = "",
    next_move: str = "",
) -> Callable[[list[dict[str, str]]], dict[str, Any]]:
    wanted = list(intents)

    def reply(messages: list[dict[str, str]]) -> dict[str, Any]:
        rows = chat_rows(messages)
        out = []
        for intent, quote in wanted:
            line_id = next((r["line_id"] for r in rows if quote in r["text"]), "9-9")
            out.append({"intent": intent, "line_id": line_id, "quote": quote, "explanation": "测试说明"})
        return {"scene": scene, "stage": stage, "intents": out, "summary": summary, "next_move": next_move}

    return reply


def backends(primary: Any = None, backup: Any = None, **kw: Any) -> dict[str, FakeBackend]:
    """`backends(primary=[...], backup=[...])`：None 表示这一方没配置。"""
    out = {}
    if primary is not None:
        out["primary"] = FakeBackend("primary", list(primary), **kw)
    if backup is not None:
        out["backup"] = FakeBackend("backup", list(backup), **kw)
    return out
