"""导入：逐行校验、可重复执行、什么时候重新算向量。"""

from __future__ import annotations

import numpy as np
from sqlalchemy import select

from app.analyzers.cases.importer import import_cases, parse_files
from app.analyzers.cases.index import vector_from_bytes
from app.analyzers.cases.models import Case
from app.paths import DATA_DIR
from app.services import open_services
from tests.cases.conftest import DIM, FakeEmbedder, case_row, write_jsonl


async def _rows(db) -> dict[str, Case]:
    async with db.session() as s:
        return {r.case_id: r for r in (await s.execute(select(Case))).scalars().all()}


async def test_import_is_idempotent(settings, cases_dir):
    f = write_jsonl(cases_dir / "a.jsonl", [case_row("c1", "刷单"), case_row("c2", "投资")])
    emb = FakeEmbedder()
    async with open_services(settings) as services:
        r1 = await import_cases(services.db, emb, [f])
        first = await _rows(services.db)
        r2 = await import_cases(services.db, emb, [f])
        second = await _rows(services.db)
    assert (r1.inserted, r1.updated, r1.unchanged, r1.embedded) == (2, 0, 0, 2)
    assert (r2.inserted, r2.updated, r2.unchanged, r2.embedded) == (0, 0, 2, 0)
    assert r1.ok and r2.ok
    assert len(emb.calls) == 1  # 第二次没调用向量接口
    assert {k: (v.title, v.embedding, v.updated_at) for k, v in first.items()} == {
        k: (v.title, v.embedding, v.updated_at) for k, v in second.items()
    }
    vec = vector_from_bytes(second["c1"].embedding, DIM)
    assert vec is not None and np.allclose(vec, np.ones(DIM))
    assert second["c1"].embedding_model == emb.model and second["c1"].embedding_dim == DIM


async def test_reembed_only_when_text_or_model_changes(settings, cases_dir):
    f = cases_dir / "a.jsonl"
    write_jsonl(f, [case_row("c1", "刷单", body="正文一"), case_row("c2", "投资")])
    emb = FakeEmbedder()
    async with open_services(settings) as services:
        await import_cases(services.db, emb, [f])

        # 只改摘要和来源相关字段：更新但不重新算向量
        write_jsonl(f, [case_row("c1", "刷单", body="正文一", snippet="新摘要"), case_row("c2", "投资")])
        emb.calls.clear()
        r = await import_cases(services.db, emb, [f])
        assert (r.updated, r.unchanged, r.embedded) == (1, 1, 0) and emb.calls == []
        assert (await _rows(services.db))["c1"].snippet == "新摘要"

        # 改正文：只重算这一条
        write_jsonl(f, [case_row("c1", "刷单", body="正文二", snippet="新摘要"), case_row("c2", "投资")])
        r = await import_cases(services.db, emb, [f])
        assert r.embedded == 1 and emb.embedded_texts == ["刷单\n正文二"]

        # 换向量型号：全部重算
        emb2 = FakeEmbedder(model="other-model")
        r = await import_cases(services.db, emb2, [f])
        assert r.embedded == 2 and r.updated == 2
        assert {row.embedding_model for row in (await _rows(services.db)).values()} == {"other-model"}

        # 换维度：全部重算
        emb3 = FakeEmbedder(model="other-model", dim=3)
        r = await import_cases(services.db, emb3, [f])
        assert r.embedded == 2


async def test_batches_follow_embedder_batch_size(settings, cases_dir):
    f = write_jsonl(cases_dir / "a.jsonl", [case_row(f"c{i}", f"标题{i}") for i in range(5)])
    emb = FakeEmbedder(batch_size=2)
    async with open_services(settings) as services:
        r = await import_cases(services.db, emb, [f])
    assert [len(c) for c in emb.calls] == [2, 2, 1] and r.embedded == 5


def test_row_validation_reports_file_and_line(cases_dir):
    f = write_jsonl(
        cases_dir / "bad.jsonl",
        [
            case_row("ok1", "合格"),
            "{不是 json",
            case_row("real1", "真实缺链接", synthetic=False, source_name="某公安局", source_url=None),
            case_row("syn1", "合成却有链接", source_url="https://example.gov.cn/1"),
            case_row("syn2", "合成来源写错", source_name="某公安局"),
            case_row("real2", "来源写合成示例却没标", synthetic=False, source_url="https://example.gov.cn/2"),
            case_row("ok1", "重复编号"),
            case_row("x3", "场景不对", scene="none"),
            case_row("x4", "日期不对", published_at="2026/05/12"),
            case_row("x5", "多余字段", typo_field=1),
            "[1, 2]",
            "",
            "// 注释行",
            case_row("real3", "真实合格", synthetic=False, source_name="某公安局", source_url="https://example.gov.cn/3"),
        ],
    )
    parsed = parse_files([f])
    assert [rec.case_id for _, _, rec in parsed.records] == ["ok1", "real3"]
    by_line = {e.line: e.message for e in parsed.errors}
    assert sorted(by_line) == [2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
    assert all(e.file == "bad.jsonl" for e in parsed.errors)
    assert "JSON" in by_line[2]
    assert "来源链接" in by_line[3]
    assert "source_url 必须为空" in by_line[4]
    assert "合成示例" in by_line[5]
    assert "synthetic: true" in by_line[6]
    assert "重复" in by_line[7]
    assert "none" in by_line[8]
    assert "YYYY-MM-DD" in by_line[9]
    assert "typo_field" in by_line[10]
    assert "JSON 对象" in by_line[11]
    assert str(parsed.errors[0]).startswith("bad.jsonl:2")


async def test_invalid_rows_are_skipped_and_report_not_ok(settings, cases_dir):
    f = write_jsonl(cases_dir / "a.jsonl", [case_row("c1", "合格"), case_row("c2", "坏的", scene="nope")])
    async with open_services(settings) as services:
        r = await import_cases(services.db, FakeEmbedder(), [f])
        rows = await _rows(services.db)
    assert set(rows) == {"c1"} and r.inserted == 1
    assert not r.ok and len(r.errors) == 1
    assert any("a.jsonl:2" in line for line in r.lines())


async def test_missing_key_writes_nothing_and_says_what_to_configure(settings, cases_dir):
    f = write_jsonl(cases_dir / "a.jsonl", [case_row("c1", "刷单")])
    emb = FakeEmbedder(configured=False)
    async with open_services(settings) as services:
        r = await import_cases(services.db, emb, [f])
        rows = await _rows(services.db)
    assert rows == {} and emb.calls == []
    assert r.missing_key and not r.ok
    assert "EMBEDDING_API_KEY" in "\n".join(r.lines())


async def test_missing_key_is_fine_when_nothing_needs_embedding(settings, cases_dir):
    f = write_jsonl(cases_dir / "a.jsonl", [case_row("c1", "刷单")])
    async with open_services(settings) as services:
        await import_cases(services.db, FakeEmbedder(), [f])
        write_jsonl(f, [case_row("c1", "刷单", snippet="改摘要")])
        r = await import_cases(services.db, FakeEmbedder(configured=False), [f])
    assert r.ok and r.updated == 1


async def test_embedding_failure_midway_keeps_done_batches_and_resumes(settings, cases_dir):
    f = write_jsonl(cases_dir / "a.jsonl", [case_row(f"c{i}", f"标题{i}") for i in range(4)])
    async with open_services(settings) as services:
        r = await import_cases(services.db, FakeEmbedder(batch_size=2, fail_after_calls=1), [f])
        assert r.embedding_error == "timeout" and r.embedded == 2 and r.pending_embedding == 2 and not r.ok
        assert len(await _rows(services.db)) == 2
        emb = FakeEmbedder(batch_size=2)
        r = await import_cases(services.db, emb, [f])
        assert r.embedded == 2 and r.unchanged == 2 and r.ok
        assert emb.embedded_texts == ["标题2\n正文", "标题3\n正文"]


async def test_stale_cases_are_reported_and_pruned_on_request(settings, cases_dir):
    f = write_jsonl(cases_dir / "a.jsonl", [case_row("c1", "一"), case_row("c2", "二")])
    async with open_services(settings) as services:
        await import_cases(services.db, FakeEmbedder(), [f])
        write_jsonl(f, [case_row("c1", "一")])
        r = await import_cases(services.db, FakeEmbedder(), [f])
        assert r.stale == ["c2"] and r.pruned == 0 and "c2" in await _rows(services.db)
        r = await import_cases(services.db, FakeEmbedder(), [f], prune=True)
        assert r.pruned == 1 and set(await _rows(services.db)) == {"c1"}


async def test_dry_run_writes_nothing_and_calls_no_embedding(settings, cases_dir):
    f = write_jsonl(cases_dir / "a.jsonl", [case_row("c1", "一")])
    emb = FakeEmbedder(configured=False)
    async with open_services(settings) as services:
        r = await import_cases(services.db, emb, [f], dry_run=True)
        assert await _rows(services.db) == {}
    assert r.inserted == 1 and emb.calls == [] and r.ok


def test_shipped_synthetic_samples_are_valid():
    files = sorted((DATA_DIR / "cases").glob("*.jsonl"))
    parsed = parse_files(files)
    assert parsed.errors == []
    records = [rec for _, _, rec in parsed.records]
    synthetic = [r for r in records if r.synthetic]
    assert 0 < len(synthetic) <= 10
    assert all(r.source_name == "合成示例" and r.source_url is None for r in synthetic)
