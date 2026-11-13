"""持久化：启动时清理遗留任务、过期清理、分析器包的表自动创建、UTF-8 存取。"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import inspect as sa_inspect

from app.checks.service import orphan_error
from app.checks.store import CheckStore
from app.contract import AnalyzerState, AnalyzerStatus, AnalyzerName
from app.db import Base, utcnow
from app.services import open_services
from tests.conftest import AUTH


async def test_fail_orphans_marks_unfinished(settings):
    async with open_services(settings) as services:
        store = CheckStore(services.db, 7)
        await store.create(check_id="c_A", trace_id="t_A", device_id="d", request={}, mock=False)
        await store.create(check_id="c_B", trace_id="t_B", device_id="d", request={}, mock=False)
        await store.set_running("c_B")
        await store.set_progress(
            "c_B",
            [
                AnalyzerState(name=AnalyzerName.SCRIPT, status=AnalyzerStatus.DONE, elapsed_ms=5),
                AnalyzerState(name=AnalyzerName.LINK, status=AnalyzerStatus.RUNNING),
                AnalyzerState(name=AnalyzerName.CASES, status=AnalyzerStatus.PENDING),
                AnalyzerState(name=AnalyzerName.DEEPFAKE, status=AnalyzerStatus.SKIPPED),
                AnalyzerState(name=AnalyzerName.SUMMARY, status=AnalyzerStatus.PENDING),
            ],
        )
        await store.create(check_id="c_C", trace_id="t_C", device_id="d", request={}, mock=False)
        await store._update("c_C", status="succeeded")

        assert await store.fail_orphans(orphan_error) == 2
        a, b, c = [await store.get(x) for x in ("c_A", "c_B", "c_C")]
    assert a.status == b.status == "failed"
    assert c.status == "succeeded" and c.error is None
    assert b.error["code"] == "INTERNAL" and b.error["retryable"] is True and b.error["trace_id"] == "t_B"
    assert [s["status"] for s in b.analyzers] == ["done", "failed", "failed", "skipped", "failed"]
    assert b.analyzers[0]["elapsed_ms"] == 5


def test_orphans_failed_on_startup_and_visible_via_http(make_client, settings):
    import asyncio

    async def seed():
        async with open_services(settings) as services:
            store = CheckStore(services.db, 7)
            await store.create(check_id="c_ORPHAN", trace_id="t_X", device_id="d", request={}, mock=False)

    asyncio.run(seed())
    client = make_client(settings)
    body = client.get("/v1/checks/c_ORPHAN", headers=AUTH).json()
    assert body["status"] == "failed"
    assert body["error"]["code"] == "INTERNAL" and body["error"]["retryable"] is True
    assert client.app.state.startup_info["orphans_failed"] == 1


async def test_purge_expired(settings):
    async with open_services(settings) as services:
        store = CheckStore(services.db, 7)
        await store.create(check_id="c_OLD", trace_id="t", device_id=None, request={}, mock=False)
        await store.create(check_id="c_NEW", trace_id="t", device_id=None, request={}, mock=False)
        await store._update("c_OLD", expires_at=utcnow() - timedelta(seconds=1))
        assert await store.get("c_OLD") is None  # 过期即不可查
        assert await store.purge_expired() == 1
        assert await store.get("c_NEW") is not None


async def test_db_files_live_under_var_dir(settings):
    async with open_services(settings) as services:
        assert services.db.path == settings.var_dir / "antifraud.db"
        assert await services.db.ping()
    assert (settings.var_dir / "antifraud.db").exists()


async def test_chinese_text_roundtrip(settings):
    async with open_services(settings) as services:
        store = CheckStore(services.db, 7)
        req = {"items": [{"type": "chat", "lines": [{"line_id": "0-0", "speaker": "other", "text": "先垫付<AMOUNT:200>返🎁"}]}]}
        await store.create(check_id="c_ZH", trace_id="t", device_id=None, request=req, mock=False)
        assert (await store.get("c_ZH")).request == req


def _write_pkg(root, name: str, models_src: str) -> None:
    pkg = root / name
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "models.py").write_text(models_src, encoding="utf-8")


async def test_analyzer_package_tables_are_created(settings, monkeypatch, tmp_path):
    """分析器包在 models.py 里声明的表会被自动创建（用一个临时包模拟）。"""
    import app.analyzers as registry

    _write_pkg(
        tmp_path,
        "fakepkg_ok",
        "from sqlalchemy import Integer\n"
        "from sqlalchemy.orm import Mapped, mapped_column\n"
        "from app.db import Base\n\n"
        "class FakeTable(Base):\n"
        "    __tablename__ = 'fakepkg_items'\n"
        "    id: Mapped[int] = mapped_column(Integer, primary_key=True)\n",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setitem(registry.PACKAGES, AnalyzerName.CASES, "fakepkg_ok")
    try:
        async with open_services(settings) as services:
            assert services.table_errors == []
            async with services.db.engine.connect() as conn:
                tables = await conn.run_sync(lambda c: sa_inspect(c).get_table_names())
        assert "fakepkg_items" in tables and "checks" in tables
    finally:
        table = Base.metadata.tables.get("fakepkg_items")
        if table is not None:
            Base.metadata.remove(table)


async def test_broken_models_module_is_reported_not_fatal(settings, monkeypatch, tmp_path):
    import app.analyzers as registry

    _write_pkg(tmp_path, "fakepkg_bad", "raise RuntimeError('broken table definition')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setitem(registry.PACKAGES, AnalyzerName.LINK, "fakepkg_bad")
    async with open_services(settings) as services:
        assert services.table_errors == ["fakepkg_bad.models"]
        assert await services.db.ping()  # 服务照常可用


async def test_package_without_models_is_fine(settings):
    # 现有四个占位包都没有 models.py：不算错误
    async with open_services(settings) as services:
        assert services.table_errors == []


def test_expired_checks_purged_on_startup(make_client, settings):
    import asyncio

    async def seed():
        async with open_services(settings) as services:
            store = CheckStore(services.db, 7)
            await store.create(check_id="c_EXP", trace_id="t", device_id=None, request={}, mock=False)
            await store._update("c_EXP", status="succeeded", expires_at=utcnow() - timedelta(seconds=1))

    asyncio.run(seed())
    client = make_client(settings)
    assert client.app.state.startup_info["expired_purged"] == 1


async def test_cleanup_loop_purges_periodically(make_settings):
    import asyncio
    from types import SimpleNamespace

    from app.main import _cleanup_loop

    settings = make_settings(cleanup_interval_s=0.01)

    class Pipe:
        calls = 0

        async def cleanup(self):
            Pipe.calls += 1

    async with open_services(settings) as services:
        store = CheckStore(services.db, 7)
        await store.create(check_id="c_OLD", trace_id="t", device_id=None, request={}, mock=False)
        await store._update("c_OLD", status="succeeded", expires_at=utcnow() - timedelta(seconds=1))
        app = SimpleNamespace(state=SimpleNamespace(settings=settings, store=store, pipeline=Pipe()))
        task = asyncio.create_task(_cleanup_loop(app))
        try:
            for _ in range(200):
                await asyncio.sleep(0.01)
                if Pipe.calls >= 2:
                    break
        finally:
            task.cancel()
        assert Pipe.calls >= 2  # 出错不退出，一直按间隔跑
        assert await store.purge_expired() == 0


async def test_cleanup_loop_survives_errors(make_settings):
    import asyncio
    from types import SimpleNamespace

    from app.main import _cleanup_loop

    settings = make_settings(cleanup_interval_s=0.01)
    calls = []

    class BadStore:
        async def purge_expired(self):
            calls.append(1)
            raise RuntimeError("db locked")

    app = SimpleNamespace(state=SimpleNamespace(settings=settings, store=BadStore(), pipeline=None))
    task = asyncio.create_task(_cleanup_loop(app))
    for _ in range(200):
        await asyncio.sleep(0.01)
        if len(calls) >= 2:
            break
    task.cancel()
    assert len(calls) >= 2
