"""通过 open_pipeline(settings) 的端到端测试：用的是包里 create() 返回的实例，向量接口用 respx 打桩。"""

from __future__ import annotations

import json

import httpx
import respx

from app.analyzers.cases.importer import import_cases
from app.contract import AnalyzerName, AnalyzerStatus, DegradedFlag
from app.pipeline import open_pipeline
from app.services import open_services
from tests.cases.conftest import FakeEmbedder, case_row, write_jsonl
from tests.conftest import chat_request

BASE = "https://embed.example.test/v1"
DIM = 4


def _settings(make_settings, **kw):
    values = dict(embedding_base_url=BASE, embedding_dim=DIM, embedding_model="fake-embed", cases_similarity_threshold=0.5)
    values.update(kw)
    return make_settings(**values)


async def _seed(settings, cases_dir):
    f = write_jsonl(
        cases_dir / "c.jsonl",
        [case_row("syn_a", "刷单", scene="brushing_rebate"), case_row("syn_b", "冒充", scene="impersonation_service")],
    )
    emb = FakeEmbedder({"刷单": [1, 0, 0, 0], "冒充": [0, 0, 1, 0]})
    async with open_services(settings) as services:
        report = await import_cases(services.db, emb, [f])
    assert report.ok


def _respond(req: httpx.Request) -> httpx.Response:
    texts = json.loads(req.content)["input"]
    return httpx.Response(200, json={"data": [{"index": i, "embedding": [0.9, 0.1, 0.0, 0.0]} for i in range(len(texts))]})


async def test_e2e_similar_cases_reach_report(make_settings, cases_dir):
    settings = _settings(make_settings, embedding_api_key="sk-test")
    await _seed(settings, cases_dir)
    with respx.mock:
        route = respx.post(f"{BASE}/embeddings").mock(side_effect=_respond)
        async with open_pipeline(settings) as pipe:
            result = await pipe.analyze(chat_request("日结兼职，点赞截图一单3元", "先垫付返利"))
    run = result.runs[AnalyzerName.CASES]
    assert run.status == AnalyzerStatus.DONE, run
    assert route.call_count == 1
    assert [c.case_id for c in run.output.similar_cases] == ["syn_a"]
    assert result.report.similar_cases[0].synthetic is True
    assert result.report.similar_cases[0].source_url is None
    assert DegradedFlag.CASES_UNAVAILABLE not in result.report.degraded


async def test_e2e_empty_library_skipped_no_request(make_settings):
    settings = _settings(make_settings, embedding_api_key="sk-test")
    with respx.mock(assert_all_called=False):
        route = respx.post(f"{BASE}/embeddings").mock(side_effect=_respond)
        async with open_pipeline(settings) as pipe:
            result = await pipe.analyze(chat_request("日结兼职，点赞截图一单3元"))
    assert result.runs[AnalyzerName.CASES].status == AnalyzerStatus.SKIPPED
    assert not route.called
    assert DegradedFlag.CASES_UNAVAILABLE not in result.report.degraded


async def test_e2e_no_key_fails_with_flag(make_settings, cases_dir):
    settings = _settings(make_settings)  # 没有任何密钥
    await _seed(settings, cases_dir)
    async with open_pipeline(settings) as pipe:
        result = await pipe.analyze(chat_request("日结兼职，点赞截图一单3元"))
    run = result.runs[AnalyzerName.CASES]
    assert run.status == AnalyzerStatus.FAILED and run.error is None
    assert DegradedFlag.CASES_UNAVAILABLE in result.report.degraded


async def test_e2e_network_down_fails_with_flag(make_settings, cases_dir):
    settings = _settings(make_settings, embedding_api_key="sk-test")
    await _seed(settings, cases_dir)
    with respx.mock:
        respx.post(f"{BASE}/embeddings").mock(side_effect=httpx.ConnectError("down"))
        async with open_pipeline(settings) as pipe:
            result = await pipe.analyze(chat_request("日结兼职，点赞截图一单3元"))
    run = result.runs[AnalyzerName.CASES]
    assert run.status == AnalyzerStatus.FAILED and run.output.note == "embedding_connect_error"
    assert DegradedFlag.CASES_UNAVAILABLE in result.report.degraded
    assert DegradedFlag.PARTIAL_TIMEOUT not in result.report.degraded


def test_cli_status_and_import(make_settings, capsys, cases_dir):
    from app.analyzers.cases.__main__ import main
    from app.config import override_settings

    settings = make_settings()
    write_jsonl(cases_dir / "c.jsonl", [case_row("syn_a", "刷单"), case_row("bad", "坏", scene="x")])
    with override_settings(var_dir=settings.var_dir):
        assert main(["import", "--dir", str(cases_dir)]) == 1  # 没有密钥
        out = capsys.readouterr().out
        assert "EMBEDDING_API_KEY" in out and "c.jsonl:2" in out
        assert main(["import", "--dir", str(cases_dir), "--dry-run"]) == 1  # 有不合格行
        assert main(["status"]) == 0
        out = capsys.readouterr().out
        assert "数据库" in out and "案例：0 条" in out
