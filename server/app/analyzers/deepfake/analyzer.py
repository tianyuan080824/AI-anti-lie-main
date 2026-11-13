"""媒体与深伪 `deepfake`。

- 没有媒体条目时 skipped。
- 有媒体时：优先用流水线预处理阶段存下的结果（`store`）；没有时自己分析。
- 图片只做 OCR，返回 `not_analyzed`（说明里写明没有做合成检测）；音频和视频做转写 + 合成语音检测。
- 视频只分析音轨，说明里写明「画面是否换脸没有检测」。
- 缺依赖、缺权重、缺 ffmpeg 时如实返回 `not_analyzed` + `deepfake_unavailable`，不报错。
"""

from __future__ import annotations

import asyncio
import time

from app.analyzers.base import Analyzer, AnalysisInput, AnalyzerContext, AnalyzerOutput, SelfCheckItem
from app.analyzers.deepfake.engines import Engines
from app.analyzers.deepfake.env import find_tools, missing
from app.analyzers.deepfake.preprocess import Prepared, _not_analyzed, analyze_item, prepare as preprocess_media
from app.analyzers.deepfake.runtime import await_loading, create_engines, engine_status, manifest_files, preload
from app.analyzers.deepfake.settings import MediaSettings, get_media_settings
from app.analyzers.deepfake.storage import sweep
from app.analyzers.deepfake.weights import check, manual_instructions
from app.config import Settings
from app.contract import AnalyzerName, AnalyzerStatus, DegradedFlag
from app.services import Services

#: 预处理结果没被取走时保留这么久（流水线只隔几毫秒，留足重试的时间）
_PREPARED_KEEP_S = 600.0


class PreparedStore:
    """按 check_id 暂存预处理结果，`run()` 取走后删除。"""

    def __init__(self) -> None:
        self._items: dict[str, tuple[float, Prepared]] = {}

    def put(self, check_id: str, prepared: Prepared) -> None:
        self._items[check_id] = (time.monotonic(), prepared)

    def pop(self, check_id: str) -> Prepared | None:
        item = self._items.pop(check_id, None)
        return item[1] if item else None

    def drop_stale(self, keep_s: float = _PREPARED_KEEP_S) -> None:
        cutoff = time.monotonic() - keep_s
        self._items = {key: value for key, value in self._items.items() if value[0] >= cutoff}


class DeepfakeAnalyzer(Analyzer):
    name = AnalyzerName.DEEPFAKE
    failure_flags = (DegradedFlag.DEEPFAKE_UNAVAILABLE,)

    def __init__(self, ms: MediaSettings | None = None, engines: Engines | None = None) -> None:
        self.ms = ms or get_media_settings()
        self.engines = engines
        self.store = PreparedStore()
        self.services: Services | None = None

    def should_run(self, inp: AnalysisInput) -> bool:
        return bool(inp.media)

    # ---- 预处理：流水线在四路并行之前调用 ----

    has_prepare = True

    def prepare_budget_s(self, settings: Settings) -> float:
        return self.ms.preprocess_budget_s if self.ms.enabled else 0.0

    async def prepare(self, inp: AnalysisInput, ctx: AnalyzerContext) -> AnalysisInput:
        if not self.ms.enabled:
            return inp
        # 先占位：预处理被流水线中途放弃时，run() 看到占位就知道媒体已被取走，记成超时降级而不是重新分析
        self.store.put(inp.check_id, Prepared(degraded={DegradedFlag.DEEPFAKE_UNAVAILABLE}))
        remaining = ctx.remaining_s()
        budget = self.ms.preprocess_budget_s if remaining is None else min(self.ms.preprocess_budget_s, remaining)
        ms = self.ms.model_copy(update={"preprocess_budget_s": max(0.0, budget)})
        merged, prepared = await preprocess_media(inp, ctx.services, self._engines(), ms)
        self.store.put(inp.check_id, prepared)
        return merged

    async def run(self, inp: AnalysisInput, ctx: AnalyzerContext) -> AnalyzerOutput:
        prepared = self.store.pop(inp.check_id)
        results = []
        lines = []
        degraded: set[DegradedFlag] = set(prepared.degraded) if prepared else set()
        for item in inp.media:
            outcome = prepared.take(item.media_id) if prepared else None
            if outcome is None and not self.ms.enabled:
                outcome = _not_analyzed(item, "媒体分析未启用", degraded=True)
            if outcome is None and prepared is not None:
                outcome = _not_analyzed(item, "分析超时", degraded=True)
            if outcome is None:
                remaining = ctx.remaining_s()
                budget = self.ms.preprocess_budget_s if remaining is None else min(self.ms.preprocess_budget_s, remaining)
                outcome = await analyze_item(
                    item, check_id=inp.check_id, services=ctx.services, engines=self._engines(), ms=self.ms, budget_s=max(0.0, budget)
                )
            results.append(outcome.media)
            lines.extend(outcome.lines)
            degraded |= outcome.degraded
        return AnalyzerOutput(
            status=AnalyzerStatus.DONE,
            media=results,
            derived_lines=lines,
            degraded=sorted(degraded, key=lambda f: list(DegradedFlag).index(f)),
        )

    def _engines(self) -> Engines:
        if self.engines is None:
            self.engines = create_engines(self.ms)
        return self.engines

    # ---- 生命周期 ----

    async def startup(self, services: Services) -> None:
        self.services = services
        if self.ms.preload:
            preload(self._engines())

    async def shutdown(self) -> None:
        if self.engines is not None:
            await asyncio.to_thread(await_loading, self.engines)

    async def cleanup(self, services: Services) -> None:
        self.store.drop_stale()
        await sweep(services.db, services.settings, self.ms)

    async def selfcheck(self, services: Services) -> list[SelfCheckItem]:
        return await asyncio.to_thread(self._selfcheck, services.settings)

    def _selfcheck(self, settings: Settings) -> list[SelfCheckItem]:
        items = []
        absent = missing("python-multipart", "Pillow", "rapidocr", "onnxruntime", "torch", "funasr")
        if absent:
            items.append(
                SelfCheckItem(
                    "媒体依赖",
                    False,
                    "缺少 " + "、".join(absent),
                    "在 server/ 目录执行 uv sync --extra media",
                )
            )
        else:
            items.append(SelfCheckItem("媒体依赖", True, "media 扩展已安装"))
        tools = find_tools(settings)
        if tools.ok:
            items.append(SelfCheckItem("ffmpeg", True, tools.ffmpeg or ""))
        else:
            items.append(
                SelfCheckItem(
                    "ffmpeg",
                    False,
                    "找不到 ffmpeg 或 ffprobe，音频和视频分析不可用",
                    "安装 ffmpeg 并加入 PATH，或设置 FFMPEG_PATH",
                )
            )
        for label, used_by in (("语音转写权重", "asr"), ("合成检测权重", "spoof")):
            files = manifest_files(self.ms, used_by)
            problems = [f"{wf.path}：{reason}" for wf in files if (reason := check(wf, self.ms.models_dir))]
            if not files:
                items.append(SelfCheckItem(label, False, "权重清单里没有对应文件"))
            elif problems:
                hint = manual_instructions(files[0], self.ms.models_dir)
                items.append(SelfCheckItem(label, False, "；".join(problems), hint))
            else:
                items.append(SelfCheckItem(label, True, f"{len(files)} 个文件校验通过"))
        return items

    def status_line(self) -> str:
        if not self.ms.enabled:
            return "未启用（MEDIA_ENABLED=0）"
        return engine_status(self._engines())


def create() -> DeepfakeAnalyzer:
    return DeepfakeAnalyzer()


__all__ = ["DeepfakeAnalyzer", "PreparedStore", "create"]
