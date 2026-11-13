"""大模型客户端与健康状态。

健康检查接口里的 `llm` 字段反映最近一次真实调用或自检的结果，接口本身不调用大模型。
主：通义千问（百炼），备：智谱 GLM，都走 OpenAI 兼容接口（`app/llm/client.py`）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from app.db import utcnow
from app.llm.client import (
    ChatBackend,
    Endpoint,
    LlmError,
    OpenAIChatBackend,
    call_json,
    parse_json,
)

LlmStatus = Literal["ok", "down", "unknown"]


@dataclass
class LlmHealth:
    status: LlmStatus = "unknown"
    updated_at: datetime | None = None
    model: str | None = None
    detail: str | None = field(default=None)
    #: 状态来自哪里：`call`（真实调用）或 `selfcheck`（启动前自检）
    source: str | None = None

    def record(
        self,
        ok: bool,
        *,
        model: str | None = None,
        detail: str | None = None,
        source: str = "call",
        at: datetime | None = None,
    ) -> None:
        self.status = "ok" if ok else "down"
        self.updated_at = at or utcnow()
        self.model = model
        self.detail = detail
        self.source = source


def endpoints(settings) -> dict[str, Endpoint]:
    """配置了密钥的模型（primary / backup）。没有密钥的不出现。"""
    out: dict[str, Endpoint] = {}
    if settings.llm_primary_api_key:
        out["primary"] = Endpoint(
            "primary", settings.llm_primary_base_url, settings.llm_primary_api_key, settings.llm_primary_model
        )
    if settings.llm_backup_api_key:
        out["backup"] = Endpoint(
            "backup", settings.llm_backup_base_url, settings.llm_backup_api_key, settings.llm_backup_model
        )
    return out


def build_backends(settings) -> dict[str, ChatBackend]:
    return {
        role: OpenAIChatBackend(ep, connect_timeout_s=settings.llm_connect_timeout_s)
        for role, ep in endpoints(settings).items()
    }


__all__ = [
    "ChatBackend",
    "Endpoint",
    "LlmError",
    "LlmHealth",
    "LlmStatus",
    "OpenAIChatBackend",
    "build_backends",
    "call_json",
    "endpoints",
    "parse_json",
]
