"""链接体检自己的表：域名注册时间缓存。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, UtcDateTime


class DomainCache(Base):
    __tablename__ = "link_domain_cache"

    domain: Mapped[str] = mapped_column(String(253), primary_key=True)
    #: 注册时间；查到了但记录里没有注册时间时为空
    created_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    #: rdap / whois
    source: Mapped[str] = mapped_column(String(16))
    checked_at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
