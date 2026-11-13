"""模拟模式（MOCK_MODE=1）。

不调用任何外部服务：按固定延迟依次点亮各分析器，最后返回契约里的示例报告
（直接读 `shared/contract-examples/report.json`，与契约示例始终一致）。
只用于 Android 端联调和界面开发，路演时不要开。健康检查如实返回 `mock: true`。
"""

from __future__ import annotations

import asyncio
import json
from typing import Awaitable, Callable

from app.contract import ANALYZER_ORDER, AnalyzerName, AnalyzerState, AnalyzerStatus, Report
from app.paths import CONTRACT_EXAMPLES_DIR

MOCK_REPORT_FILE = CONTRACT_EXAMPLES_DIR / "report.json"


def load_mock_report() -> Report:
    return Report.model_validate(json.loads(MOCK_REPORT_FILE.read_text(encoding="utf-8")))


async def run_mock(
    step_ms: int, on_update: Callable[[list[AnalyzerState]], Awaitable[None]]
) -> tuple[Report, list[AnalyzerState]]:
    """依次点亮 script → link → cases → deepfake → summary，每步 `step_ms` 毫秒。"""
    report = load_mock_report()
    states = {n: AnalyzerState(name=n, status=AnalyzerStatus.PENDING) for n in ANALYZER_ORDER}

    def snapshot() -> list[AnalyzerState]:
        return [states[n].model_copy() for n in ANALYZER_ORDER]

    for name in ANALYZER_ORDER:
        if name == AnalyzerName.DEEPFAKE and not report.media:
            states[name] = AnalyzerState(name=name, status=AnalyzerStatus.SKIPPED)
            await on_update(snapshot())
            continue
        states[name] = AnalyzerState(name=name, status=AnalyzerStatus.RUNNING)
        await on_update(snapshot())
        await asyncio.sleep(step_ms / 1000)
        states[name] = AnalyzerState(name=name, status=AnalyzerStatus.DONE, elapsed_ms=step_ms)
        await on_update(snapshot())
    return report, snapshot()
