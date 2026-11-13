"""相似案例分析器。

- `startup()`：只读本地数据库，把案例向量读进内存（不联网）。
- `should_run()`：没有对方 / 未知说话人的文字，或案例库一条都没有时返回 False（→ skipped，不发请求）。
- `run()`：取对方和未知说话人的文本，截断到 `cases_query_max_chars`，调用一次向量接口，
  取相似度 ≥ 阈值的前 `cases_top_k` 条。向量接口不可用（没密钥、连不上、超时、返回不对）→ failed + cases_unavailable。
- 导入新案例后需要重启服务才能看到（启动时一次性读入）。
"""

from __future__ import annotations

from typing import Callable

from app.analyzers.base import (
    COUNTERPART_SPEAKERS,
    Analyzer,
    AnalysisInput,
    AnalyzerContext,
    AnalyzerOutput,
    SelfCheckItem,
)
from app.analyzers.cases.embedder import KEY_HINT, Embedder, EmbeddingError, create_embedder
from app.analyzers.cases.index import CaseIndex, load_index
from app.config import Settings
from app.contract import AnalyzerName, AnalyzerStatus, DegradedFlag
from app.services import Services

IMPORT_CMD = "uv run python -m app.analyzers.cases import"
#: 留给向量请求之后检索和返回的余量（秒）
_MARGIN_S = 0.1


def query_text(inp: AnalysisInput, max_chars: int) -> str:
    """对方和未知说话人的脱敏文本，按行拼接后截断。"""
    return inp.text(COUNTERPART_SPEAKERS, sep="\n").strip()[:max_chars]


class CasesAnalyzer(Analyzer):
    name = AnalyzerName.CASES
    failure_flags = (DegradedFlag.CASES_UNAVAILABLE,)

    def __init__(self, embedder_factory: Callable[[Settings], Embedder] | None = None) -> None:
        self._embedder_factory = embedder_factory or create_embedder
        self.embedder: Embedder | None = None
        self.index: CaseIndex | None = None

    # ---- 生命周期 ----

    async def startup(self, services: Services) -> None:
        settings = services.settings
        self.embedder = self._embedder_factory(settings)
        self.index = await load_index(services.db, model=self.embedder.model, dim=self.embedder.dim)

    async def shutdown(self) -> None:
        if self.embedder is not None:
            await self.embedder.aclose()

    def status_line(self) -> str:
        if self.index is None:
            return "未加载"
        idx = self.index
        if idx.total == 0:
            return f"案例库为空（运行 {IMPORT_CMD} 导入），这一路会跳过"
        parts = [f"案例 {idx.usable} 条可用"]
        if idx.synthetic:
            parts.append(f"其中合成示例 {idx.synthetic} 条")
        if idx.mismatched:
            parts.append(f"{idx.mismatched} 条向量型号或维度与配置不符，需重新导入")
        if idx.missing:
            parts.append(f"{idx.missing} 条没有向量，需重新导入")
        if self.embedder is not None and not self.embedder.configured:
            parts.append("向量接口未配置密钥，运行时会降级")
        return "，".join(parts)

    async def selfcheck(self, services: Services) -> list[SelfCheckItem]:
        settings = services.settings
        embedder = self._embedder_factory(settings)
        try:
            idx = await load_index(services.db, model=embedder.model, dim=embedder.dim)
            items = [
                SelfCheckItem(
                    name="案例库",
                    ok=idx.usable > 0,
                    detail=f"{idx.total} 条，可用 {idx.usable} 条，合成示例 {idx.synthetic} 条",
                    hint=None if idx.usable > 0 else f"整理案例放进 server/data/cases/*.jsonl 后运行 {IMPORT_CMD}",
                ),
                SelfCheckItem(
                    name="案例向量与配置一致",
                    ok=idx.mismatched == 0 and idx.missing == 0,
                    detail=f"当前配置 {embedder.model} / {embedder.dim} 维；不符 {idx.mismatched} 条，缺向量 {idx.missing} 条",
                    hint=None
                    if idx.mismatched == 0 and idx.missing == 0
                    else f"换过向量型号或维度、或上次导入没做完：重新运行 {IMPORT_CMD}",
                ),
            ]
            if not embedder.configured:
                items.append(
                    SelfCheckItem(name="向量接口", ok=False, detail=f"{embedder.model}：未配置密钥", hint=KEY_HINT, state="not_configured")
                )
            else:
                try:
                    await embedder.embed(["自检"])
                    items.append(SelfCheckItem(name="向量接口", ok=True, detail=f"{embedder.model} 调用成功"))
                except EmbeddingError as exc:
                    items.append(
                        SelfCheckItem(
                            name="向量接口",
                            ok=False,
                            detail=f"调用失败（{exc.kind}）",
                            hint="检查网络、EMBEDDING_BASE_URL、EMBEDDING_MODEL 和密钥",
                        )
                    )
            return items
        finally:
            await embedder.aclose()

    # ---- 运行 ----

    def should_run(self, inp: AnalysisInput) -> bool:
        if inp.char_count(COUNTERPART_SPEAKERS) == 0:
            return False
        # 库是空的：部署没做完，由 status_line / selfcheck 提醒，不算降级
        return not (self.index is not None and self.index.total == 0)

    def _fail(self, reason: str, extras: dict | None = None) -> AnalyzerOutput:
        return AnalyzerOutput(
            status=AnalyzerStatus.FAILED,
            degraded=[DegradedFlag.CASES_UNAVAILABLE],
            note=reason,
            extras=extras or {},
        )

    async def run(self, inp: AnalysisInput, ctx: AnalyzerContext) -> AnalyzerOutput:
        if self.index is None or self.embedder is None:
            return self._fail("not_started")
        settings = ctx.settings
        idx, embedder = self.index, self.embedder
        extras = {"cases_total": idx.total, "cases_usable": idx.usable, "embedding_model": embedder.model}
        if idx.usable == 0:
            return self._fail("index_unusable", extras)
        if not embedder.configured:
            return self._fail("no_api_key", extras)

        text = query_text(inp, settings.cases_query_max_chars)
        extras["query_chars"] = len(text)
        timeout = settings.embedding_timeout_s
        remaining = ctx.remaining_s()
        if remaining is not None:
            timeout = min(timeout, remaining - _MARGIN_S)
        if timeout <= 0:
            return self._fail("no_time", extras)
        try:
            vectors = await embedder.embed([text], timeout_s=timeout)
        except EmbeddingError as exc:
            return self._fail(f"embedding_{exc.kind}", extras)

        # 几百条 × 1024 维的矩阵乘法在毫秒级，不必放进线程
        cases = idx.search(vectors[0], top_k=settings.cases_top_k, threshold=settings.cases_similarity_threshold)
        best = idx.best_score(vectors[0])
        extras.update(best_similarity=None if best is None else round(best, 4), returned=len(cases))
        return AnalyzerOutput(status=AnalyzerStatus.DONE, similar_cases=cases, extras=extras)


def create() -> CasesAnalyzer:
    return CasesAnalyzer()
