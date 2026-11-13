"""检索、空库/失败的区分、启动加载与维度不符、status_line 和 selfcheck。"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from app.analyzers.base import AnalyzerContext
from app.analyzers.cases import CasesAnalyzer
from app.analyzers.cases.analyzer import query_text
from app.analyzers.cases.importer import import_cases
from app.analyzers.cases.index import CaseIndex, CaseMeta
from app.contract import AnalyzerStatus, DegradedFlag
from app.services import open_services
from tests.cases.conftest import DIM, FakeEmbedder, case_row, make_input, unit, write_jsonl

# 四个案例在 4 维空间里的方向；查询向量 Q 与它们的余弦相似度分别约为 0.97 / 0.80 / 0.60 / 0.0
VECTORS = {
    "刷单": [1.0, 0.0, 0.0, 0.0],
    "垫付": [1.0, 0.75, 0.0, 0.0],
    "返利": [0.6, 0.8, 0.0, 0.0],
    "冒充": [0.0, 0.0, 1.0, 0.0],
}
CASES = [case_row("a_brush", "刷单"), case_row("b_advance", "垫付"), case_row("c_rebate", "返利"), case_row("d_fake", "冒充")]


async def _seed(services, cases_dir, rows=CASES, **emb_kwargs):
    f = write_jsonl(cases_dir / "cases.jsonl", rows)
    await import_cases(services.db, FakeEmbedder(VECTORS, **emb_kwargs), [f])


async def _started(services, embedder) -> CasesAnalyzer:
    analyzer = CasesAnalyzer(embedder_factory=lambda s: embedder)
    await analyzer.startup(services)
    return analyzer


def _ctx(services, deadline_s: float | None = 3.0) -> AnalyzerContext:
    loop = asyncio.get_running_loop()
    return AnalyzerContext(
        services=services,
        check_id="c_test",
        trace_id="t_test",
        deadline=None if deadline_s is None else loop.time() + deadline_s,
    )


# ---- 索引检索（纯函数）----


def _index(vectors: dict[str, list[float]]) -> CaseIndex:
    metas = [
        CaseMeta(case_id=k, title=k, scene="other", snippet=k, source_name="合成示例", source_url=None, published_at=None, synthetic=True)
        for k in vectors
    ]
    matrix = np.vstack([unit(*v) for v in vectors.values()])
    return CaseIndex(model="m", dim=DIM, total=len(metas), metas=metas, matrix=matrix)


def test_search_top_k_threshold_and_order():
    idx = _index(VECTORS)
    q = np.array([1.0, 0.25, 0.0, 0.0], dtype=np.float32)  # 不需要事先归一化
    # 手算：|q|=1.0308；刷单 1/1.0308=0.9701；垫付 (1+0.1875)/(1.25·1.0308)=0.9216；返利 (0.6+0.2)/1.0308=0.7761；冒充 0
    all_hits = idx.search(q, top_k=10, threshold=0.0)
    assert [h.case_id for h in all_hits] == ["刷单", "垫付", "返利", "冒充"]
    assert [h.similarity for h in all_hits[:3]] == pytest.approx([0.9701, 0.9216, 0.7761], abs=1e-4)
    assert [h.case_id for h in idx.search(q, top_k=2, threshold=0.0)] == ["刷单", "垫付"]
    assert [h.case_id for h in idx.search(q, top_k=10, threshold=0.8)] == ["刷单", "垫付"]
    assert idx.search(q, top_k=10, threshold=0.99) == []
    assert idx.search(np.zeros(DIM), top_k=3, threshold=0.0) == []
    assert idx.search(np.ones(DIM + 1), top_k=3, threshold=0.0) == []
    assert all(h.synthetic and h.source_url is None for h in all_hits)


def test_query_text_takes_counterpart_and_unknown_and_truncates():
    inp = make_input(("other", "对方说的话"), ("self", "我说的话"), ("unknown", "粘贴的话"), ("system", "昨天 10:00"))
    assert query_text(inp, 1500) == "对方说的话\n粘贴的话"
    assert query_text(inp, 7) == "对方说的话\n粘"


# ---- 分析器 ----


async def test_run_returns_top_k_above_threshold_desc(make_settings, cases_dir):
    settings = make_settings(cases_top_k=2, cases_similarity_threshold=0.7)
    async with open_services(settings) as services:
        await _seed(services, cases_dir)
        emb = FakeEmbedder({"刷单": [1.0, 0.25, 0.0, 0.0]})
        analyzer = await _started(services, emb)
        inp = make_input(("other", "刷单做任务"), ("self", "我自己的话不进查询"))
        assert analyzer.should_run(inp)
        out = await analyzer.run(inp, _ctx(services))
    assert out.status == AnalyzerStatus.DONE and out.degraded == []
    assert [c.case_id for c in out.similar_cases] == ["a_brush", "b_advance"]
    sims = [c.similarity for c in out.similar_cases]
    assert sims == sorted(sims, reverse=True) and min(sims) >= 0.7
    assert emb.calls == [["刷单做任务"]]  # 只调用一次，查询文本只含对方
    assert out.extras["returned"] == 2 and out.extras["cases_usable"] == 4
    assert out.extras["best_similarity"] == pytest.approx(0.9701, abs=1e-4)


async def test_query_is_truncated_to_config(make_settings, cases_dir):
    settings = make_settings(cases_query_max_chars=5)
    async with open_services(settings) as services:
        await _seed(services, cases_dir)
        emb = FakeEmbedder()
        analyzer = await _started(services, emb)
        await analyzer.run(make_input(("unknown", "一二三四五六七八")), _ctx(services))
    assert emb.calls == [["一二三四五"]]


async def test_empty_library_is_skipped_without_request(settings):
    async with open_services(settings) as services:
        emb = FakeEmbedder()
        analyzer = await _started(services, emb)
    assert not analyzer.should_run(make_input(("other", "刷单返利")))
    assert emb.calls == []
    assert "为空" in analyzer.status_line()


async def test_no_counterpart_text_is_skipped(settings, cases_dir):
    async with open_services(settings) as services:
        await _seed(services, cases_dir)
        analyzer = await _started(services, FakeEmbedder())
    assert not analyzer.should_run(make_input(("self", "只有我自己说的话")))


@pytest.mark.parametrize("kind", ["timeout", "connect_error", "http_500", "bad_response"])
async def test_embedding_failure_is_failed_with_flag(settings, cases_dir, kind):
    async with open_services(settings) as services:
        await _seed(services, cases_dir)
        analyzer = await _started(services, FakeEmbedder(fail=kind))
        out = await analyzer.run(make_input(("other", "刷单")), _ctx(services))
    assert out.status == AnalyzerStatus.FAILED
    assert out.degraded == [DegradedFlag.CASES_UNAVAILABLE]
    assert out.similar_cases == [] and out.note == f"embedding_{kind}"


async def test_no_key_is_failed_without_request(settings, cases_dir):
    async with open_services(settings) as services:
        await _seed(services, cases_dir)
        emb = FakeEmbedder(configured=False)
        analyzer = await _started(services, emb)
        assert analyzer.should_run(make_input(("other", "刷单")))
        out = await analyzer.run(make_input(("other", "刷单")), _ctx(services))
    assert out.status == AnalyzerStatus.FAILED and out.degraded == [DegradedFlag.CASES_UNAVAILABLE]
    assert out.note == "no_api_key" and emb.calls == []
    assert "密钥" in analyzer.status_line()


async def test_no_time_left_fails_fast(settings, cases_dir):
    async with open_services(settings) as services:
        await _seed(services, cases_dir)
        emb = FakeEmbedder()
        analyzer = await _started(services, emb)
        out = await analyzer.run(make_input(("other", "刷单")), _ctx(services, deadline_s=0.0))
    assert out.status == AnalyzerStatus.FAILED and out.note == "no_time" and emb.calls == []


async def test_not_started_fails():
    out = await CasesAnalyzer().run(make_input(("other", "刷单")), None)  # type: ignore[arg-type]
    assert out.status == AnalyzerStatus.FAILED and out.degraded == [DegradedFlag.CASES_UNAVAILABLE]


# ---- 启动 ----


async def test_startup_loads_vectors_without_network(settings, cases_dir):
    async with open_services(settings) as services:
        await _seed(services, cases_dir)
        emb = FakeEmbedder()
        analyzer = await _started(services, emb)
    assert emb.calls == []  # 启动不调用向量接口（block_network 也保证不联网）
    idx = analyzer.index
    assert idx is not None and idx.usable == 4 and idx.matrix.shape == (4, DIM) and idx.matrix.dtype == np.float32
    assert np.allclose(np.linalg.norm(idx.matrix, axis=1), 1.0)
    assert analyzer.status_line().startswith("案例 4 条可用")


async def test_dimension_or_model_mismatch_is_excluded_and_reported(settings, cases_dir):
    async with open_services(settings) as services:
        f = write_jsonl(cases_dir / "cases.jsonl", CASES)
        await import_cases(services.db, FakeEmbedder(dim=3), [f])  # 库里是 3 维
        emb = FakeEmbedder()  # 当前配置 4 维
        analyzer = await _started(services, emb)
        assert analyzer.index.total == 4 and analyzer.index.usable == 0 and analyzer.index.mismatched == 4
        assert "重新导入" in analyzer.status_line()
        # 库不空但不可用：照常运行，报失败降级而不是静默跳过
        assert analyzer.should_run(make_input(("other", "刷单")))
        out = await analyzer.run(make_input(("other", "刷单")), _ctx(services))
        assert out.status == AnalyzerStatus.FAILED and out.note == "index_unusable" and emb.calls == []

        items = await CasesAnalyzer(embedder_factory=lambda s: FakeEmbedder()).selfcheck(services)
    by_name = {i.name: i for i in items}
    assert not by_name["案例库"].ok
    assert not by_name["案例向量与配置一致"].ok and "重新运行" in by_name["案例向量与配置一致"].hint


async def test_model_mismatch_counts_too(settings, cases_dir):
    async with open_services(settings) as services:
        f = write_jsonl(cases_dir / "cases.jsonl", CASES[:2])
        await import_cases(services.db, FakeEmbedder(model="old-model"), [f])
        analyzer = await _started(services, FakeEmbedder())
    assert analyzer.index.mismatched == 2 and analyzer.index.usable == 0


# ---- 自检 ----


async def test_selfcheck_all_ok(settings, cases_dir):
    async with open_services(settings) as services:
        await _seed(services, cases_dir)
        emb = FakeEmbedder()
        items = await CasesAnalyzer(embedder_factory=lambda s: emb).selfcheck(services)
    assert [i.ok for i in items] == [True, True, True]
    assert emb.calls == [["自检"]] and emb.closed


async def test_selfcheck_reports_empty_library_missing_key_and_failure(settings, cases_dir):
    async with open_services(settings) as services:
        items = await CasesAnalyzer(embedder_factory=lambda s: FakeEmbedder(configured=False)).selfcheck(services)
        by_name = {i.name: i for i in items}
        assert not by_name["案例库"].ok and "import" in by_name["案例库"].hint
        assert not by_name["向量接口"].ok and "EMBEDDING_API_KEY" in by_name["向量接口"].hint

        await _seed(services, cases_dir)
        items = await CasesAnalyzer(embedder_factory=lambda s: FakeEmbedder(fail="connect_error")).selfcheck(services)
    by_name = {i.name: i for i in items}
    assert by_name["案例库"].ok and not by_name["向量接口"].ok and "connect_error" in by_name["向量接口"].detail
