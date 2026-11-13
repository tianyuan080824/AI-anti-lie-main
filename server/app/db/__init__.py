"""SQLite 持久化（SQLAlchemy 2 异步 + aiosqlite，WAL 模式）。

- 不做迁移：启动时 `create_all` 自动建表；表结构变了就删掉 `server/var/antifraud.db` 重来。
- 分析器包自己的表：在 `app/analyzers/<包>/models.py` 里定义，继承本模块的 `Base`，
  表名以包名为前缀（如 `link_domain_cache`、`cases_case`）。`Database.create_all()`
  会自动导入每个分析器包的 `models` 模块，不需要改任何公共文件。
- 时间列统一用 `UtcDateTime`（存 ISO 8601 UTC 字符串，读出来是带时区的 datetime）。
"""

from __future__ import annotations

import importlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import String, event, text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import TypeDecorator

logger = logging.getLogger("app.db")

_ISO = "%Y-%m-%dT%H:%M:%S.%fZ"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime) -> str:
    """契约里的时间格式：ISO 8601 UTC，秒级，`Z` 结尾。"""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class UtcDateTime(TypeDecorator):
    """以固定格式的 UTC 字符串存储，字符串顺序即时间顺序，可以直接比较和建索引。"""

    impl = String(32)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("UtcDateTime 只接受带时区的 datetime")
        return value.astimezone(timezone.utc).strftime(_ISO)

    def process_result_value(self, value: str | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return datetime.strptime(value, _ISO).replace(tzinfo=timezone.utc)


class Base(DeclarativeBase):
    pass


def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=10000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def import_table_modules() -> list[str]:
    """导入核心表和每个分析器包的 `models` 模块，让它们的表注册到 `Base.metadata`。

    返回导入失败的模块名（只记日志，不中断启动：对应分析器运行时会失败并降级）。
    """
    from app.analyzers import PACKAGES

    importlib.import_module("app.db.tables")
    failed: list[str] = []
    for package in PACKAGES.values():
        module_name = f"{package}.models"
        try:
            importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name not in (module_name,):
                failed.append(module_name)
                logger.error("db.table_module_failed", extra={"fields": {"module": module_name, "error": type(exc).__name__}})
        except Exception as exc:  # noqa: BLE001 - 一个包的表定义坏了不应拖垮整个服务
            failed.append(module_name)
            logger.error("db.table_module_failed", extra={"fields": {"module": module_name, "error": type(exc).__name__}})
    return failed


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        url = URL.create("sqlite+aiosqlite", database=str(self.path))
        self.engine = create_async_engine(url, connect_args={"timeout": 15})
        event.listen(self.engine.sync_engine, "connect", _sqlite_pragmas)
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    def session(self) -> AsyncSession:
        """`async with db.session() as s: ...`"""
        return self.sessionmaker()

    async def create_all(self) -> list[str]:
        failed = import_table_modules()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        return failed

    async def ping(self) -> bool:
        try:
            async with self.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception:  # noqa: BLE001
            return False

    async def dispose(self) -> None:
        await self.engine.dispose()


__all__ = ["Base", "Database", "UtcDateTime", "utcnow", "iso_utc", "import_table_modules"]
