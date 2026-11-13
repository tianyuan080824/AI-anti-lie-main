"""分析器统一接口。第一阶段结束时冻结，之后只做向后兼容的补充。

一个分析器 = `Analyzer` 的子类，放在 `app/analyzers/<名字>/` 包里，包的 `__init__.py`
导出工厂函数 `create() -> Analyzer`。流水线（`app/pipeline/runner.py`）负责：

- 并行调用四个分析器的 `run()`，每个都有独立超时（`Settings.timeout_<name>_s`，
  且不超过整单截止时间减去汇总预留）；
- 超时或抛异常时只把这一路标成 `failed`，并加上降级标记，其余各路和报告照常产出；
- 每次状态变化都写库，轮询接口能读到；
- 最后把各路结果交给汇总（`app/summary/`）生成报告。

输入 `AnalysisInput` 已经过数量/长度校验和二次脱敏。聊天内容一律是不可信数据：
里面的「忽略之前的指令」之类是待分析的材料，不是指令。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings
from app.contract import (
    AnalyzerName,
    AnalyzerStatus,
    ClientInfo,
    DegradedFlag,
    DerivedLine,
    Evidence,
    LinkResult,
    MediaKind,
    MediaResult,
    SimilarCase,
    Speaker,
)
from app.db import Database
from app.services import Services

# ---------------------------------------------------------------------------
# 输入
# ---------------------------------------------------------------------------


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class InputLine(_Frozen):
    """一行聊天文本（已二次脱敏）。"""

    line_id: str
    speaker: Speaker
    text: str
    #: 在请求 items 里的序号
    item_index: int
    #: 所在 chat 条目的 origin：screenshot_ocr / pasted_text / shared_text
    origin: str | None = None


class UrlInput(_Frozen):
    """请求里的 `url` 条目。正文里夹带的链接由链接分析器自己从 `lines` 里提取。"""

    url: str
    origin: str | None = None
    item_index: int


class MediaInput(_Frozen):
    """请求里的 audio / video / image 条目（P1）。"""

    media_id: str
    kind: MediaKind
    item_index: int
    masked: bool | None = None


#: 「对方」视角的说话人：对方 + 未知（粘贴的整段文字说话人都是 unknown）。
COUNTERPART_SPEAKERS: tuple[Speaker, ...] = (Speaker.OTHER, Speaker.UNKNOWN)


class AnalysisInput(_Frozen):
    check_id: str
    trace_id: str
    lines: tuple[InputLine, ...] = ()
    urls: tuple[UrlInput, ...] = ()
    media: tuple[MediaInput, ...] = ()
    source: str | None = None
    client: ClientInfo | None = None
    #: 校验并二次脱敏后的请求（JSON 形式，入库用）
    request: dict[str, Any] = Field(default_factory=dict)

    def iter_lines(self, speakers: Iterable[Speaker] | None = None) -> Iterator[InputLine]:
        allowed = None if speakers is None else set(speakers)
        for line in self.lines:
            if allowed is None or line.speaker in allowed:
                yield line

    def text(self, speakers: Iterable[Speaker] | None = None, sep: str = "\n") -> str:
        return sep.join(line.text for line in self.iter_lines(speakers))

    def line(self, line_id: str) -> InputLine | None:
        for line in self.lines:
            if line.line_id == line_id:
                return line
        return None

    def char_count(self, speakers: Iterable[Speaker] | None = None) -> int:
        """非空白字符数（判断「可分析文本不足 10 个字」用）。"""
        return sum(1 for line in self.iter_lines(speakers) for ch in line.text if not ch.isspace())


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


class AnalyzerOutput(BaseModel):
    """一个分析器的产出。只填和自己有关的字段，其余保持空。"""

    model_config = ConfigDict(extra="forbid")

    #: done：正常完成；skipped：没有可分析的内容（如没有链接）或功能未启用；
    #: failed：自己捕获到的失败（未捕获的异常和超时由流水线标记）。
    status: Literal[AnalyzerStatus.DONE, AnalyzerStatus.SKIPPED, AnalyzerStatus.FAILED] = AnalyzerStatus.DONE
    degraded: list[DegradedFlag] = Field(default_factory=list)

    # ---- 要并入报告的契约对象 ----
    #: script：候选证据（汇总会做逐字回查，对不上的丢弃）
    evidence: list[Evidence] = Field(default_factory=list)
    #: link：每条链接的体检结果
    links: list[LinkResult] = Field(default_factory=list)
    #: cases：最相似的案例（已按阈值过滤、按相似度降序）
    similar_cases: list[SimilarCase] = Field(default_factory=list)
    #: deepfake：媒体分析结果
    media: list[MediaResult] = Field(default_factory=list)
    #: deepfake（P1）：转写 / OCR 得到的行，line_id 以 d 开头
    derived_lines: list[DerivedLine] = Field(default_factory=list)

    #: 给汇总用的额外信号（各分析器自定义，可 JSON 序列化），例如 script 的 scene/stage/summary/next_move、
    #: 规则命中、交叉复核另一方的结果。会入库（runs 列）并出现在 open_pipeline() 的结果里。
    extras: dict[str, Any] = Field(default_factory=dict)
    #: 内部说明，只进日志和评测结果，不进报告。不得包含正文。
    note: str | None = None


class AnalyzerRun(BaseModel):
    """流水线记录的一路运行结果（交给汇总；也出现在 PipelineResult 里）。"""

    name: AnalyzerName
    status: AnalyzerStatus
    elapsed_ms: int | None = None
    output: AnalyzerOutput = Field(default_factory=AnalyzerOutput)
    #: 流水线捕获到的失败：`timeout` 或异常类型名。只进日志和评测结果。
    error: str | None = None


# ---------------------------------------------------------------------------
# 上下文
# ---------------------------------------------------------------------------


@dataclass
class AnalyzerContext:
    """一次鉴定里交给分析器的上下文。"""

    services: Services
    check_id: str
    trace_id: str
    #: 本路的截止时间（`asyncio.get_running_loop().time()` 的时钟），None 表示不限
    deadline: float | None = None

    @property
    def settings(self) -> Settings:
        return self.services.settings

    @property
    def db(self) -> Database:
        return self.services.db

    @property
    def logger(self) -> logging.LoggerAdapter:
        return logging.LoggerAdapter(
            logging.getLogger("app.analyzers"), {"fields": {"trace_id": self.trace_id, "check_id": self.check_id}}
        )

    def remaining_s(self) -> float | None:
        if self.deadline is None:
            return None
        import asyncio

        return max(0.0, self.deadline - asyncio.get_running_loop().time())


@dataclass
class SelfCheckItem:
    """启动前自检的一项（第五步的自检命令逐项打印）。"""

    name: str
    ok: bool
    detail: str = ""
    #: 失败时该怎么办
    hint: str | None = None
    #: 不是简单的通过/失败时：`not_configured` 显示「未配置」（算未通过），`hint` 显示「提示」（不影响结果）
    state: Literal["not_configured", "hint"] | None = None


# ---------------------------------------------------------------------------
# 分析器基类
# ---------------------------------------------------------------------------


class Analyzer(ABC):
    #: 契约里的分析器名字，子类必须设置
    name: AnalyzerName
    #: 这一路超时或抛异常时加到报告里的降级标记（超时还会额外加 partial_timeout）
    failure_flags: tuple[DegradedFlag, ...] = ()

    def timeout_s(self, settings: Settings) -> float:
        return settings.analyzer_timeout(self.name.value)

    def should_run(self, inp: AnalysisInput) -> bool:
        """返回 False 时这一路直接标成 skipped，不调用 run()。必须很快、不能联网。"""
        return True

    @abstractmethod
    async def run(self, inp: AnalysisInput, ctx: AnalyzerContext) -> AnalyzerOutput:
        """分析一次请求。不要吞掉 asyncio.CancelledError（流水线靠它实现超时）。"""

    # ---- 预处理（流水线在四路并行之前调用；默认什么都不做）----

    #: 覆盖了 `prepare` 的分析器设为 True，流水线只给这些分析器分预处理时间
    has_prepare: bool = False

    def prepare_budget_s(self, settings: Settings) -> float:
        """预处理想要的时间。流水线会再压到「整单截止 − 汇总 − 话术」之内，不挤占话术。"""
        return 0.0

    async def prepare(self, inp: AnalysisInput, ctx: AnalyzerContext) -> AnalysisInput:
        """返回交给四路分析器的输入（例如补上转写 / OCR 得到的派生行）。不要吞掉 CancelledError。"""
        return inp

    # ---- 生命周期钩子（都有默认实现）----

    async def startup(self, services: Services) -> None:
        """服务启动时调用一次：加载数据、把案例向量读进内存等。不得访问外部网络。
        抛异常不会阻止服务启动，只会让这一路在运行时失败并降级。"""

    async def shutdown(self) -> None:
        """服务退出时调用：关闭自己创建的 httpx 客户端等。"""

    async def cleanup(self, services: Services) -> None:
        """定时清理（间隔 `Settings.cleanup_interval_s`）：删除自己表里的过期数据。"""

    async def selfcheck(self, services: Services) -> list[SelfCheckItem]:
        """启动前自检（第五步的自检命令会调用）：例如向量接口能否调通。可以联网。"""
        return []

    def status_line(self) -> str:
        """启动状态输出里这一路的一句话描述，例如「案例 312 条」。"""
        return "就绪"


__all__ = [
    "Analyzer",
    "AnalysisInput",
    "AnalyzerContext",
    "AnalyzerOutput",
    "AnalyzerRun",
    "COUNTERPART_SPEAKERS",
    "InputLine",
    "MediaInput",
    "SelfCheckItem",
    "UrlInput",
]
