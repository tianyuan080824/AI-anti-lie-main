"""分析流水线的对外入口。

- HTTP 层：`Pipeline.run(inp, on_update)`。
- 评测脚本和命令行（不经过 HTTP）：

      from app.pipeline import open_pipeline

      async with open_pipeline() as pipe:          # 可传 settings=... 指定临时数据库等
          result = await pipe.analyze(request)     # request: dict 或 CheckRequest
          result.report.verdict, result.runs[...].output.extras, result.elapsed_ms

  或一次性的 `await run_check(request)`。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from app.analyzers.base import Analyzer, AnalysisInput
from app.config import Settings
from app.contract import CheckRequest
from app.ids import new_check_id, new_trace_id
from app.pipeline.inputs import build_input
from app.pipeline.runner import OnUpdate, Pipeline, PipelineResult
from app.services import Services, open_services
from app.summary import Summarizer


class LocalPipeline:
    """不经过 HTTP、不写任务表的流水线：评测脚本直接用它。"""

    def __init__(self, pipeline: Pipeline) -> None:
        self.pipeline = pipeline

    @property
    def services(self) -> Services:
        return self.pipeline.services

    def build_input(self, request: CheckRequest | dict[str, Any]) -> AnalysisInput:
        req = request if isinstance(request, CheckRequest) else CheckRequest.model_validate(request)
        return build_input(req, check_id=new_check_id(), trace_id=new_trace_id(), settings=self.services.settings)

    async def analyze(self, request: CheckRequest | dict[str, Any], on_update: OnUpdate | None = None) -> PipelineResult:
        """校验 + 二次脱敏 + 四路分析 + 汇总。请求不合法时抛 `app.errors.ApiError`（code=INVALID_INPUT）。"""
        return await self.pipeline.run(self.build_input(request), on_update)


@asynccontextmanager
async def open_pipeline(
    settings: Settings | None = None,
    *,
    analyzers: list[Analyzer] | None = None,
    summarizer: Summarizer | None = None,
) -> AsyncIterator[LocalPipeline]:
    """打开数据库（自动建表）、加载并启动四个分析器，退出时关闭。`settings` 缺省为全局配置。"""
    async with open_services(settings) as services:
        pipeline = Pipeline(services, analyzers, summarizer)
        await pipeline.startup()
        try:
            yield LocalPipeline(pipeline)
        finally:
            await pipeline.shutdown()


async def run_check(request: CheckRequest | dict[str, Any], settings: Settings | None = None) -> PipelineResult:
    """一次性跑完一个请求。批量评测请用 `open_pipeline()`，避免每条都重新加载分析器。"""
    async with open_pipeline(settings) as pipe:
        return await pipe.analyze(request)


__all__ = ["LocalPipeline", "Pipeline", "PipelineResult", "build_input", "open_pipeline", "run_check"]
