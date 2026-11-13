"""汇总 `summary`（归话术/汇总会话）。

接口（冻结）：`Summarizer.summarize(inp, runs, ctx) -> Report`，纯代码，不调大模型。
`runs` 含四个分析器的 `AnalyzerRun`（一定齐全，没跑的是 skipped）。
流水线会在汇总之后把各路的 `degraded` 并进报告，所以汇总漏掉也不会丢标记。
汇总自己超时或抛异常时，流水线改用 `fallback_report()`，报告照常产出。
"""

from app.summary.base import Summarizer, create, fallback_report

__all__ = ["Summarizer", "create", "fallback_report"]
