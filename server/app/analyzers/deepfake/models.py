"""媒体分析自己的表：上传的媒体文件登记。

原文件放在 `var/media/<随机名>`，表里只记元数据（不记客户端给的文件名）。
分析时先「认领」（写 claimed_by），读进内存后立即删文件；行留到 expires_at 之后由清理任务删除。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, UtcDateTime


class MediaRecord(Base):
    __tablename__ = "deepfake_media"

    media_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    kind: Mapped[str] = mapped_column(String(8))
    #: var/media 下的文件名（服务端生成的随机名）
    file_name: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    #: ffprobe / Pillow 认出来的真实格式
    format_name: Mapped[str] = mapped_column(String(64))
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: 原始音轨采样率（音频 / 视频）
    sample_rate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    has_audio: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    claimed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    #: 认领它的 check_id；同一次鉴定可以重复认领
    claimed_by: Mapped[str | None] = mapped_column(String(40), nullable=True)
