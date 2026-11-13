"""核心表（归话术/汇总会话维护）。分析器包的表放在各自包的 models.py 里。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, UtcDateTime


class CheckRecord(Base):
    """一次鉴定任务。只存脱敏后的请求、各分析器状态、报告和耗时。"""

    __tablename__ = "checks"

    check_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(40))
    device_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    mock: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    #: 校验并二次脱敏之后的请求
    request: Mapped[dict[str, Any]] = mapped_column(JSON)
    #: 契约 4.3 的 analyzers 数组（已按契约序列化）
    analyzers: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    derived_lines: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    report: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    #: 任务级失败时的错误对象（契约第 7 节的 error 内层对象）
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    #: 各分析器的原始输出（调试和评测用，不对外返回）
    runs: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    elapsed_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)


class FeedbackRecord(Base):
    """用户对一次鉴定的反馈（契约 4.4）。每次鉴定只留最新一条；备注是脱敏后的。随鉴定一起过期。"""

    __tablename__ = "feedback"

    check_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    device_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    label: Mapped[str] = mapped_column(String(16))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)


class StateRecord(Base):
    """进程之间要共享的小状态（键值）。目前：启动前自检写入的大模型状态，服务启动时读回健康检查。"""

    __tablename__ = "app_state"

    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime)
