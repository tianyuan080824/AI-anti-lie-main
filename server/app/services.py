"""进程级共享资源：配置、数据库、大模型健康状态。

HTTP 服务、`open_pipeline()`（评测用）和各分析器的命令行入口都通过 `open_services()` 拿到同一套资源。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncIterator

from app.config import Settings, get_settings
from app.db import Database
from app.llm import LlmHealth


@dataclass
class Services:
    settings: Settings
    db: Database
    llm_health: LlmHealth = field(default_factory=LlmHealth)
    #: 建表时导入失败的 models 模块
    table_errors: list[str] = field(default_factory=list)


@asynccontextmanager
async def open_services(settings: Settings | None = None) -> AsyncIterator[Services]:
    """打开数据库并自动建表（含各分析器包的表），退出时释放连接。

    用法（分析器包的命令行入口）::

        async with open_services() as services:
            async with services.db.session() as session:
                ...
    """
    settings = settings or get_settings()
    db = Database(settings.database_path)
    try:
        table_errors = await db.create_all()
        yield Services(settings=settings, db=db, table_errors=table_errors)
    finally:
        await db.dispose()
