"""分析器注册表。

四个分析器各占一个包，包名固定（见 `PACKAGES`）。每个包的 `__init__.py` 导出：

- `create() -> Analyzer`（必需）：返回分析器实例；
- `models` 子模块（可选）：定义自己的数据表，建表时自动导入。

新增或替换分析器实现只改自己的包，不改本文件。
"""

from __future__ import annotations

import importlib
import logging

from app.analyzers.base import (
    Analyzer,
    AnalysisInput,
    AnalyzerContext,
    AnalyzerOutput,
    AnalyzerRun,
    COUNTERPART_SPEAKERS,
    InputLine,
    MediaInput,
    SelfCheckItem,
    UrlInput,
)
from app.contract import AnalyzerName, DegradedFlag

logger = logging.getLogger("app.analyzers")

#: 分析器名字 → 包路径。顺序即流水线里的顺序。
PACKAGES: dict[AnalyzerName, str] = {
    AnalyzerName.SCRIPT: "app.analyzers.script",
    AnalyzerName.LINK: "app.analyzers.link",
    AnalyzerName.CASES: "app.analyzers.cases",
    AnalyzerName.DEEPFAKE: "app.analyzers.deepfake",
}

#: 某一路失败（超时、抛异常、加载失败）时加到报告里的降级标记。
#: 契约的 degraded 枚举里没有通用的「分析器失败」，按每一路最贴近的含义选取；超时另加 partial_timeout。
DEFAULT_FAILURE_FLAGS: dict[AnalyzerName, tuple[DegradedFlag, ...]] = {
    AnalyzerName.SCRIPT: (DegradedFlag.LLM_UNAVAILABLE_RULES_ONLY,),
    AnalyzerName.LINK: (DegradedFlag.INTEL_UNAVAILABLE,),
    AnalyzerName.CASES: (DegradedFlag.CASES_UNAVAILABLE,),
    AnalyzerName.DEEPFAKE: (DegradedFlag.DEEPFAKE_UNAVAILABLE,),
}


class BrokenAnalyzer(Analyzer):
    """包加载失败时的替身：每次运行都失败，让报告带上降级标记，而不是让服务起不来。"""

    def __init__(self, name: AnalyzerName, error: str) -> None:
        self.name = name
        self.failure_flags = DEFAULT_FAILURE_FLAGS[name]
        self.error = error

    async def run(self, inp: AnalysisInput, ctx: AnalyzerContext) -> AnalyzerOutput:
        raise RuntimeError(f"analyzer package failed to load: {self.error}")

    def status_line(self) -> str:
        return f"加载失败（{self.error}）"

    async def selfcheck(self, services) -> list[SelfCheckItem]:
        return [
            SelfCheckItem(
                f"{self.name.value} 加载",
                False,
                f"分析器包加载失败（{self.error}）",
                "看 var/logs/server.log 里的 analyzers.load_failed，修好导入错误或补装依赖后再自检；这一路运行时会降级",
            )
        ]


def load_analyzer(name: AnalyzerName) -> Analyzer:
    try:
        module = importlib.import_module(PACKAGES[name])
        analyzer = module.create()
        if not isinstance(analyzer, Analyzer) or analyzer.name != name:
            raise TypeError(f"{PACKAGES[name]}.create() must return an Analyzer named {name.value}")
        return analyzer
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "analyzers.load_failed",
            exc_info=True,
            extra={"fields": {"analyzer": name.value, "error": type(exc).__name__}},
        )
        return BrokenAnalyzer(name, type(exc).__name__)


def load_analyzers() -> list[Analyzer]:
    return [load_analyzer(name) for name in PACKAGES]


__all__ = [
    "Analyzer",
    "AnalysisInput",
    "AnalyzerContext",
    "AnalyzerOutput",
    "AnalyzerRun",
    "BrokenAnalyzer",
    "COUNTERPART_SPEAKERS",
    "DEFAULT_FAILURE_FLAGS",
    "InputLine",
    "MediaInput",
    "PACKAGES",
    "SelfCheckItem",
    "UrlInput",
    "load_analyzer",
    "load_analyzers",
]
