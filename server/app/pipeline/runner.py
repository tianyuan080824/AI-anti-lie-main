"""分析流水线：四路并行 + 一次汇总。

- 每一路有独立超时，且不超过「整单截止时间 − 汇总预算」；
- 某一路超时、抛异常或返回非法结果，只把这一路标成 failed 并加降级标记，其余照常；
- 汇总失败时用兜底报告；任何单点故障都不让用户拿不到报告；
- 每次状态变化调用 `on_update(states)`，HTTP 层用它写库，轮询接口就能看到进度。

超时用 `asyncio.wait` 实现：到点就放弃这一路并取消它，不等它响应取消，
所以即使某个分析器吞掉了 CancelledError，也拖不住整单。
但分析器里的同步阻塞调用会卡住整个事件循环——阻塞操作必须放进 `asyncio.to_thread`。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.analyzers import DEFAULT_FAILURE_FLAGS, load_analyzers
from app.analyzers.base import Analyzer, AnalysisInput, AnalyzerContext, AnalyzerOutput, AnalyzerRun
from app.contract import (
    ANALYZER_ORDER,
    AnalyzerName,
    AnalyzerState,
    AnalyzerStatus,
    DegradedFlag,
    DerivedLine,
    Report,
    ordered_flags,
)
from app.logs import log_event
from app.services import Services
from app.summary import Summarizer, create as create_summarizer, fallback_report

logger = logging.getLogger("app.pipeline")

OnUpdate = Callable[[list[AnalyzerState]], Awaitable[None]]


@dataclass
class PipelineResult:
    report: Report
    #: 契约 4.3 的 analyzers 数组（五项，固定顺序）
    analyzers: list[AnalyzerState]
    #: 四个分析器各自的运行结果（含 extras，评测用）
    runs: dict[AnalyzerName, AnalyzerRun]
    derived_lines: list[DerivedLine] = field(default_factory=list)
    elapsed_ms: int = 0
    #: 汇总失败的原因（`timeout` 或异常类型名），成功为 None
    summary_error: str | None = None

    def runs_json(self) -> dict[str, Any]:
        return {name.value: run.model_dump(mode="json") for name, run in self.runs.items()}


#: 预处理交给分析器的截止时间比外层超时早这么多秒
_PREPARE_MARGIN_S = 0.25


def _ms(seconds: float) -> int:
    return max(0, int(round(seconds * 1000)))


def _retrieve(task: asyncio.Task) -> None:
    """被放弃的任务结束时取走结果，避免「exception was never retrieved」警告。"""
    if not task.cancelled():
        task.exception()


async def _wait(task: asyncio.Task, timeout: float) -> bool:
    """等 `task` 最多 `timeout` 秒，返回是否完成。整单被取消时连同子任务一起取消（服务关闭时不留孤儿任务）。"""
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout)
    except asyncio.CancelledError:
        task.cancel()
        task.add_done_callback(_retrieve)
        raise
    return bool(done)


class Pipeline:
    def __init__(
        self,
        services: Services,
        analyzers: list[Analyzer] | None = None,
        summarizer: Summarizer | None = None,
    ) -> None:
        self.services = services
        loaded = analyzers if analyzers is not None else load_analyzers()
        by_name = {a.name: a for a in loaded}
        missing = [n for n in ANALYZER_ORDER if n != AnalyzerName.SUMMARY and n not in by_name]
        if missing:
            raise ValueError(f"missing analyzers: {[n.value for n in missing]}")
        self.analyzers: list[Analyzer] = [by_name[n] for n in ANALYZER_ORDER if n in by_name]
        self.summarizer = summarizer or create_summarizer()
        self.startup_errors: dict[AnalyzerName, str] = {}

    @property
    def settings(self):
        return self.services.settings

    # ---- 生命周期 ----

    async def startup(self) -> None:
        for analyzer in self.analyzers:
            try:
                await analyzer.startup(self.services)
            except Exception as exc:  # noqa: BLE001
                self.startup_errors[analyzer.name] = type(exc).__name__
                logger.error(
                    "analyzer.startup_failed",
                    exc_info=True,
                    extra={"fields": {"analyzer": analyzer.name.value, "error": type(exc).__name__}},
                )

    async def shutdown(self) -> None:
        for analyzer in self.analyzers:
            try:
                await analyzer.shutdown()
            except Exception:  # noqa: BLE001
                logger.warning("analyzer.shutdown_failed", extra={"fields": {"analyzer": analyzer.name.value}})

    async def cleanup(self) -> None:
        for analyzer in self.analyzers:
            try:
                await analyzer.cleanup(self.services)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "analyzer.cleanup_failed",
                    extra={"fields": {"analyzer": analyzer.name.value, "error": type(exc).__name__}},
                )

    def status_lines(self) -> dict[AnalyzerName, str]:
        lines = {}
        for analyzer in self.analyzers:
            if analyzer.name in self.startup_errors:
                lines[analyzer.name] = f"启动失败（{self.startup_errors[analyzer.name]}），运行时会降级"
            else:
                try:
                    lines[analyzer.name] = analyzer.status_line()
                except Exception as exc:  # noqa: BLE001
                    lines[analyzer.name] = f"状态未知（{type(exc).__name__}）"
        return lines

    # ---- 运行 ----

    async def _prepare(self, inp: AnalysisInput, deadline: float, summary_budget: float) -> AnalysisInput:
        """依次调用各分析器的预处理。预算压到「整单截止 − 汇总 − 话术超时」之内，不挤占话术；
        超时或出错时放弃这次预处理、原样返回输入，由那一路自己在 run() 里降级。"""
        loop = asyncio.get_running_loop()
        settings = self.settings
        for analyzer in self.analyzers:
            if not analyzer.has_prepare:
                continue
            try:
                if not analyzer.should_run(inp):
                    continue
            except Exception:  # noqa: BLE001 - 第 1 步会再判一次并标成 failed
                continue
            room = deadline - summary_budget - settings.analyzer_timeout(AnalyzerName.SCRIPT.value) - loop.time()
            budget = max(0.0, min(analyzer.prepare_budget_s(settings), room))
            # 交给分析器的截止时间比外层早一点，让它自己收尾（记下降级结果）而不是被外层砍掉
            ctx = AnalyzerContext(
                services=self.services,
                check_id=inp.check_id,
                trace_id=inp.trace_id,
                deadline=loop.time() + max(0.0, budget - _PREPARE_MARGIN_S),
            )
            t0 = loop.time()
            task = asyncio.create_task(analyzer.prepare(inp, ctx), name=f"prepare:{analyzer.name.value}")
            done = await _wait(task, budget)
            error: str | None = None
            if not done:
                task.cancel()
                task.add_done_callback(_retrieve)
                error = "timeout"
            elif task.cancelled():
                error = "cancelled"
            elif task.exception() is not None:
                error = type(task.exception()).__name__
            else:
                result = task.result()
                if isinstance(result, AnalysisInput) and result.check_id == inp.check_id:
                    inp = result
                else:
                    error = "invalid_output"
            log_event(
                logger,
                "analyzer.prepared",
                logging.WARNING if error else logging.INFO,
                trace_id=inp.trace_id,
                check_id=inp.check_id,
                analyzer=analyzer.name.value,
                elapsed_ms=_ms(loop.time() - t0),
                budget_ms=_ms(budget),
                lines=len(inp.lines),
                error=error,
            )
        return inp

    async def run(self, inp: AnalysisInput, on_update: OnUpdate | None = None) -> PipelineResult:
        loop = asyncio.get_running_loop()
        settings = self.settings
        started = loop.time()
        wall_started = time.perf_counter()
        deadline = started + settings.check_deadline_s
        summary_budget = settings.timeout_summary_s

        states: dict[AnalyzerName, AnalyzerState] = {
            name: AnalyzerState(name=name, status=AnalyzerStatus.PENDING) for name in ANALYZER_ORDER
        }
        runs: dict[AnalyzerName, AnalyzerRun] = {}

        async def emit() -> None:
            if on_update is None:
                return
            try:
                await on_update([states[n].model_copy() for n in ANALYZER_ORDER])
            except Exception as exc:  # noqa: BLE001 - 写进度失败不影响分析
                logger.warning("pipeline.update_failed", extra={"fields": {"trace_id": inp.trace_id, "error": type(exc).__name__}})

        def flags_for(analyzer: Analyzer) -> tuple[DegradedFlag, ...]:
            return tuple(analyzer.failure_flags) or DEFAULT_FAILURE_FLAGS.get(analyzer.name, ())

        # 0. 预处理（媒体转写 / OCR）：补上派生行，话术、链接、案例三路都能看到
        inp = await self._prepare(inp, deadline, summary_budget)

        # 1. 决定哪些路要跑
        to_run: list[Analyzer] = []
        for analyzer in self.analyzers:
            try:
                wanted = analyzer.should_run(inp)
            except Exception as exc:  # noqa: BLE001
                runs[analyzer.name] = AnalyzerRun(
                    name=analyzer.name,
                    status=AnalyzerStatus.FAILED,
                    elapsed_ms=0,
                    output=AnalyzerOutput(status=AnalyzerStatus.FAILED, degraded=list(flags_for(analyzer))),
                    error=type(exc).__name__,
                )
                states[analyzer.name] = AnalyzerState(name=analyzer.name, status=AnalyzerStatus.FAILED, elapsed_ms=0)
                continue
            if wanted:
                to_run.append(analyzer)
                states[analyzer.name] = AnalyzerState(name=analyzer.name, status=AnalyzerStatus.RUNNING)
            else:
                runs[analyzer.name] = AnalyzerRun(
                    name=analyzer.name, status=AnalyzerStatus.SKIPPED, output=AnalyzerOutput(status=AnalyzerStatus.SKIPPED)
                )
                states[analyzer.name] = AnalyzerState(name=analyzer.name, status=AnalyzerStatus.SKIPPED)
        await emit()

        # 2. 并行运行，各自超时
        async def run_one(analyzer: Analyzer) -> None:
            timeout = min(analyzer.timeout_s(settings), max(0.0, deadline - summary_budget - loop.time()))
            ctx = AnalyzerContext(
                services=self.services,
                check_id=inp.check_id,
                trace_id=inp.trace_id,
                deadline=loop.time() + timeout,
            )
            t0 = loop.time()
            task = asyncio.create_task(analyzer.run(inp, ctx), name=f"analyzer:{analyzer.name.value}")
            done = await _wait(task, timeout)
            elapsed = _ms(loop.time() - t0)
            error: str | None = None
            output: AnalyzerOutput
            if not done:
                task.cancel()
                task.add_done_callback(_retrieve)
                error = "timeout"
                output = AnalyzerOutput(
                    status=AnalyzerStatus.FAILED,
                    degraded=list(flags_for(analyzer)) + [DegradedFlag.PARTIAL_TIMEOUT],
                )
            elif task.cancelled():
                error = "cancelled"
                output = AnalyzerOutput(status=AnalyzerStatus.FAILED, degraded=list(flags_for(analyzer)))
            elif task.exception() is not None:
                error = type(task.exception()).__name__
                output = AnalyzerOutput(status=AnalyzerStatus.FAILED, degraded=list(flags_for(analyzer)))
            else:
                result = task.result()
                try:
                    output = AnalyzerOutput.model_validate(result, from_attributes=True)
                except Exception as exc:  # noqa: BLE001 - 分析器返回了不合规的结果
                    error = f"invalid_output:{type(exc).__name__}"
                    output = AnalyzerOutput(status=AnalyzerStatus.FAILED, degraded=list(flags_for(analyzer)))
                else:
                    if output.status == AnalyzerStatus.FAILED and not output.degraded:
                        output.degraded = list(flags_for(analyzer))

            status = AnalyzerStatus(output.status)
            runs[analyzer.name] = AnalyzerRun(
                name=analyzer.name,
                status=status,
                elapsed_ms=None if status == AnalyzerStatus.SKIPPED else elapsed,
                output=output,
                error=error,
            )
            states[analyzer.name] = AnalyzerState(
                name=analyzer.name,
                status=status,
                elapsed_ms=None if status == AnalyzerStatus.SKIPPED else elapsed,
            )
            log_event(
                logger,
                "analyzer.finished",
                logging.WARNING if error else logging.INFO,
                trace_id=inp.trace_id,
                check_id=inp.check_id,
                analyzer=analyzer.name.value,
                status=status.value,
                elapsed_ms=elapsed,
                error=error,
                note=output.note,
            )
            await emit()

        await asyncio.gather(*(run_one(a) for a in to_run))

        # 3. 汇总
        states[AnalyzerName.SUMMARY] = AnalyzerState(name=AnalyzerName.SUMMARY, status=AnalyzerStatus.RUNNING)
        await emit()
        summary_timeout = max(0.05, min(summary_budget, deadline - loop.time()))
        ctx = AnalyzerContext(
            services=self.services, check_id=inp.check_id, trace_id=inp.trace_id, deadline=loop.time() + summary_timeout
        )
        t0 = loop.time()
        summary_error: str | None = None
        task = asyncio.create_task(self.summarizer.summarize(inp, dict(runs), ctx), name="summary")
        done = await _wait(task, summary_timeout)
        report: Report | None = None
        if not done:
            task.cancel()
            task.add_done_callback(_retrieve)
            summary_error = "timeout"
        elif task.cancelled():
            summary_error = "cancelled"
        elif task.exception() is not None:
            summary_error = type(task.exception()).__name__
        else:
            try:
                report = Report.model_validate(task.result(), from_attributes=True)
            except Exception as exc:  # noqa: BLE001
                summary_error = f"invalid_output:{type(exc).__name__}"
        if report is None:
            report = fallback_report(inp, dict(runs))
        summary_elapsed = _ms(loop.time() - t0)

        # 各路的降级标记一律并进报告（汇总漏掉也不丢）
        report.degraded = ordered_flags(report.degraded, *(run.output.degraded for run in runs.values()))

        states[AnalyzerName.SUMMARY] = AnalyzerState(
            name=AnalyzerName.SUMMARY,
            status=AnalyzerStatus.FAILED if summary_error else AnalyzerStatus.DONE,
            elapsed_ms=summary_elapsed,
        )
        if summary_error:
            log_event(
                logger, "summary.failed", logging.WARNING, trace_id=inp.trace_id, check_id=inp.check_id, error=summary_error
            )

        derived: list[DerivedLine] = []
        deepfake = runs.get(AnalyzerName.DEEPFAKE)
        if deepfake is not None:
            derived = list(deepfake.output.derived_lines)

        return PipelineResult(
            report=report,
            analyzers=[states[n] for n in ANALYZER_ORDER],
            runs={n: runs[n] for n in ANALYZER_ORDER if n in runs},
            derived_lines=derived,
            elapsed_ms=_ms(time.perf_counter() - wall_started),
            summary_error=summary_error,
        )
