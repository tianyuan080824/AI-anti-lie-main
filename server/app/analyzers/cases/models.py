"""案例表 `cases_case`：契约 `similar_cases` 的字段 + 正文 + 向量。

表结构变了就删掉数据库文件重新导入（不做迁移）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Integer, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, UtcDateTime


class Case(Base):
    __tablename__ = "cases_case"

    case_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    scene: Mapped[str] = mapped_column(String(40))
    snippet: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    source_name: Mapped[str] = mapped_column(String(200))
    source_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    published_at: Mapped[str | None] = mapped_column(String(10), nullable=True)
    synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    #: 算向量所用文本的 sha256；正文没变就不重新算向量
    content_hash: Mapped[str] = mapped_column(String(64))
    #: float32 小端字节；导入失败时可能为空
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    embedding_dim: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_file: Mapped[str] = mapped_column(String(200))
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime)
