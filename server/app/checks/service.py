"""鉴定任务服务：接收请求 → 入库(queued) → 后台运行流水线（或模拟）→ 入库(succeeded/failed)。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.checks.store import CheckStore
from app.contract import (
    ANALYZER_ORDER,
    AnalyzerState,
    AnalyzerStatus,
    CheckCreated,
    CheckRequest,
    CheckStatus,
    CheckStatusResponse,
    DerivedLine,
    ErrorBody,
    ErrorCode,
    Report,
)
from app.errors import error_body
from app.ids import new_check_id
from app.logs import log_event
from app.mock import run_mock
from app.pipeline.inputs import build_input
from app.pipeline.runner import Pipeline
from app.services import Services

logger = logging.getLogger("app.checks")


def orphan_error(trace_id: str) -> ErrorBody:
    return error_body(ErrorCode.INTERNAL, trace_id, "服务重启，这次鉴定没有完成，请重新提交", retryable=True)


class CheckService:
    def __init__(self, services: Services, store: CheckStore, pipeline: Pipeline | None) -> None:
        self.services = services
        self.store = store
        self.pipeline = pipeline
        self._tasks: set[asyncio.Task] = set()
        self._semaphore = asyncio.Semaphore(max(1, services.settings.max_concurrent_checks))

    @property
    def mock(self) -> bool:
        return self.services.settings.mock_mode

    async def submit(self, req: CheckRequest, *, device_id: str | None, trace_id: str) -> CheckCreated:
        settings = self.services.settings
        check_id = new_check_id()
        inp = build_input(req, check_id=check_id, trace_id=trace_id, settings=settings)
        await self.store.create(
            check_id=check_id, trace_id=trace_id, device_id=device_id, request=inp.request, mock=self.mock
        )
        log_event(
            logger,
            "check.created",
            trace_id=trace_id,
            check_id=check_id,
            mock=self.mock,
            lines=len(inp.lines),
            chars=inp.char_count(),
            urls=len(inp.urls),
            media=len(inp.media),
            source=req.source,
        )
        task = asyncio.create_task(self._execute(inp), name=f"check:{check_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return CheckCreated(check_id=check_id, status=CheckStatus.QUEUED, poll_after_ms=settings.poll_after_ms)

    async def _execute(self, inp) -> None:
        check_id, trace_id = inp.check_id, inp.trace_id

        async def on_update(states: list[AnalyzerState]) -> None:
            await self.store.set_progress(check_id, states)

        last_states: list[AnalyzerState] | None = None
        try:
            async with self._semaphore:
                await self.store.set_running(check_id)
                if self.mock:
                    report, states = await run_mock(self.services.settings.mock_step_ms, on_update)
                    await self._succeed(check_id, report, states, [], None, 0, trace_id)
                    return
                assert self.pipeline is not None
                result = await self.pipeline.run(inp, on_update)
                last_states = result.analyzers
                await self._succeed(
                    check_id,
                    result.report,
                    result.analyzers,
                    result.derived_lines,
                    result.runs_json(),
                    result.elapsed_ms,
                    trace_id,
                )
        except asyncio.CancelledError:
            # 进程退出时被取消：下次启动会把它标成 failed（fail_orphans）
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "check.failed",
                exc_info=True,
                extra={"fields": {"trace_id": trace_id, "check_id": check_id, "error": type(exc).__name__}},
            )
            states = None
            if last_states is not None:
                states = [s.model_dump(mode="json") for s in last_states]
            try:
                await self.store.set_failed(check_id, error_body(ErrorCode.INTERNAL, trace_id), self._close_states(states))
            except Exception:  # noqa: BLE001
                logger.error("check.mark_failed_failed", extra={"fields": {"trace_id": trace_id, "check_id": check_id}})

    @staticmethod
    def _close_states(states: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        if states is None:
            return [
                AnalyzerState(name=n, status=AnalyzerStatus.FAILED).model_dump(mode="json") for n in ANALYZER_ORDER
            ]
        return [
            {"name": s["name"], "status": AnalyzerStatus.FAILED.value}
            if s.get("status") in (AnalyzerStatus.PENDING.value, AnalyzerStatus.RUNNING.value)
            else s
            for s in states
        ]

    async def _succeed(
        self,
        check_id: str,
        report: Report,
        states: list[AnalyzerState],
        derived_lines: list[DerivedLine],
        runs: dict[str, Any] | None,
        elapsed_ms: int,
        trace_id: str,
    ) -> None:
        await self.store.set_succeeded(
            check_id,
            report=report.model_dump(mode="json"),
            states=states,
            derived_lines=[d.model_dump(mode="json") for d in derived_lines],
            runs=runs,
            elapsed_ms=elapsed_ms,
        )
        log_event(
            logger,
            "check.succeeded",
            trace_id=trace_id,
            check_id=check_id,
            verdict=report.verdict.value,
            risk_score=report.risk_score,
            degraded=",".join(f.value for f in report.degraded) or None,
            elapsed_ms=elapsed_ms,
        )

    async def get(self, check_id: str) -> CheckStatusResponse | None:
        record = await self.store.get(check_id)
        if record is None:
            return None
        return CheckStatusResponse(
            check_id=record.check_id,
            status=CheckStatus(record.status),
            poll_after_ms=self.services.settings.poll_after_ms,
            analyzers=[AnalyzerState.model_validate(s) for s in record.analyzers],
            derived_lines=[DerivedLine.model_validate(d) for d in record.derived_lines or []],
            report=Report.model_validate(record.report) if record.report else None,
            error=ErrorBody.model_validate(record.error) if record.error else None,
        )

    async def wait_idle(self, timeout: float | None = None) -> None:
        """测试用：等待所有后台任务结束。"""
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=timeout)

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=2)
