"""FastAPI 应用：`create_app(settings)`。启动命令见 `app/__main__.py`（`uv run python -m app`）。"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from app import __version__
from app.analyzers.deepfake.router import router as media_router
from app.api.middleware import install_middleware
from app.api.ratelimit import RateLimiter
from app.api.routes import router
from app.checks.service import CheckService, orphan_error
from app.checks.store import CheckStore
from app.config import Settings, get_settings
from app.errors import install_error_handlers
from app.logs import log_event
from app.pipeline.runner import Pipeline
from app.rules import load_rules_meta
from app.selfcheck import load_llm_health, save_llm_health
from app.services import open_services

logger = logging.getLogger("app.main")


async def _cleanup_loop(app: FastAPI) -> None:
    settings: Settings = app.state.settings
    while True:
        await asyncio.sleep(settings.cleanup_interval_s)
        try:
            purged = await app.state.store.purge_expired()
            if app.state.pipeline is not None:
                await app.state.pipeline.cleanup()
            if purged:
                log_event(logger, "cleanup.purged", checks=purged)
        except Exception as exc:  # noqa: BLE001
            log_event(logger, "cleanup.failed", logging.WARNING, error=type(exc).__name__)


def create_app(settings: Settings | None = None, *, pipeline_factory=None) -> FastAPI:
    """`pipeline_factory(services) -> Pipeline`：测试里用来注入替身分析器。"""
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with open_services(settings) as services:
            store = CheckStore(services.db, settings.retention_days)
            orphans = await store.fail_orphans(orphan_error)
            purged = await store.purge_expired()
            try:
                llm_from_selfcheck = await load_llm_health(services.db, services.llm_health)
            except Exception:  # noqa: BLE001  读不到自检结果就保持 unknown
                llm_from_selfcheck = False
            pipeline = None
            if not settings.mock_mode:
                pipeline = pipeline_factory(services) if pipeline_factory else Pipeline(services)
                await pipeline.startup()
            app.state.services = services
            app.state.store = store
            app.state.pipeline = pipeline
            app.state.rules_meta = load_rules_meta()
            app.state.checks = CheckService(services, store, pipeline)
            app.state.rate_limiter = RateLimiter(settings.rate_limit_per_minute, settings.rate_limit_burst)
            app.state.startup_info = {
                "orphans_failed": orphans,
                "expired_purged": purged,
                "llm_from_selfcheck": llm_from_selfcheck,
            }
            log_event(
                logger,
                "server.started",
                mock=settings.mock_mode,
                orphans_failed=orphans,
                expired_purged=purged,
                llm=services.llm_health.status,
            )
            cleaner = asyncio.create_task(_cleanup_loop(app), name="cleanup")
            try:
                yield
            finally:
                cleaner.cancel()
                await app.state.checks.shutdown()
                if pipeline is not None:
                    await pipeline.shutdown()
                # 存下本次运行里的真实调用结果；服务运行中另跑过的自检更新时以自检为准，不覆盖
                if services.llm_health.source == "call" and services.llm_health.updated_at is not None:
                    try:
                        await save_llm_health(services.db, services.llm_health, only_if_newer=True)
                    except Exception:  # noqa: BLE001
                        log_event(logger, "llm_health.save_failed", logging.WARNING)

    app = FastAPI(
        title="一键鉴诈服务",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.settings = settings
    install_error_handlers(app)
    install_middleware(app)
    app.include_router(router)
    app.include_router(media_router)
    return app
