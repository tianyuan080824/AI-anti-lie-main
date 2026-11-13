"""启动前自检命令、自检结果交给健康检查、局域网地址提示。全部打桩，不连外网。"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from app import selfcheck as sc
from app.analyzers.base import Analyzer, SelfCheckItem
from app.analyzers.script.analyzer import ScriptAnalyzer
from app.contract import AnalyzerName
from app.db import utcnow
from app.llm import LlmError, LlmHealth
from app.netinfo import lan_lines, listens_on_all
from app.services import open_services
from tests.conftest import AUTH
from tests.llm_fakes import FakeBackend


class StubAnalyzer(Analyzer):
    name = AnalyzerName.LINK

    def __init__(self, items=None, exc: BaseException | None = None, hang: bool = False):
        self.items = items or []
        self.exc = exc
        self.hang = hang
        self.shut = False

    async def run(self, inp, ctx):  # pragma: no cover - 自检不跑分析
        raise NotImplementedError

    async def selfcheck(self, services):
        if self.hang:
            await asyncio.sleep(3600)
        if self.exc:
            raise self.exc
        return list(self.items)

    async def shutdown(self):
        self.shut = True


def names(result) -> dict[str, SelfCheckItem]:
    return {i.name: i for i in result.items}


# ---- 标签与汇总 ----


def test_labels_and_failure_counting():
    assert sc.label(SelfCheckItem("a", True)) == "通过"
    assert sc.label(SelfCheckItem("a", False)) == "失败"
    nc = SelfCheckItem("a", False, state="not_configured")
    hint = SelfCheckItem("a", False, state="hint")
    assert sc.label(nc) == "未配置" and sc.counts_as_failure(nc)
    assert sc.label(hint) == "提示" and not sc.counts_as_failure(hint)
    assert sc.SelfCheckResult([SelfCheckItem("a", True), hint]).ok
    assert not sc.SelfCheckResult([SelfCheckItem("a", True), nc]).ok


async def test_no_keys_shows_not_configured_and_fails(settings, tmp_path):
    result = await sc.run_selfcheck(settings, analyzers=[ScriptAnalyzer()], env_file=tmp_path / "none.env")
    items = names(result)
    for label in ("主模型", "备用模型"):
        assert items[label].state == "not_configured"
        assert "未配置密钥" in items[label].detail
    assert items["规则"].ok and "条" in items["规则"].detail
    assert items["数据库"].ok
    assert items["配置文件"].state == "hint"
    assert not result.ok
    text = "\n".join(sc.format_result(result))
    assert "[未配置] 主模型" in text and "[通过]" not in [line.split(" ")[0] for line in text.splitlines() if "主模型" in line]
    assert "LLM_PRIMARY_API_KEY" in text
    assert "最高 suspicious" in text
    # 没配置密钥：健康检查保持 unknown，不会被读成通过
    async with open_services(settings) as services:
        assert not await sc.load_llm_health(services.db, services.llm_health)
        assert services.llm_health.status == "unknown"


async def test_llm_probe_ok_is_recorded_for_health(settings, tmp_path):
    backends = {"primary": FakeBackend("primary", [{"ok": True}]), "backup": FakeBackend("backup", [LlmError("timeout")])}
    result = await sc.run_selfcheck(settings, analyzers=[ScriptAnalyzer(backends=backends)], env_file=tmp_path / "x")
    items = names(result)
    assert items["主模型"].ok and "调用成功" in items["主模型"].detail
    assert not items["备用模型"].ok and "timeout" in items["备用模型"].detail
    assert "连不上" in items["备用模型"].hint
    async with open_services(settings) as services:
        assert await sc.load_llm_health(services.db, services.llm_health)
        assert services.llm_health.status == "ok"
        assert services.llm_health.source == "selfcheck"
        assert services.llm_health.model == "fake-primary"


async def test_llm_probe_all_down_is_recorded(settings, tmp_path):
    backends = {"primary": FakeBackend("primary", [LlmError("status_401")])}
    result = await sc.run_selfcheck(settings, analyzers=[ScriptAnalyzer(backends=backends)], env_file=tmp_path / "x")
    items = names(result)
    assert "密钥不对" in items["主模型"].hint
    assert items["备用模型"].state == "not_configured"
    async with open_services(settings) as services:
        assert await sc.load_llm_health(services.db, services.llm_health)
        assert services.llm_health.status == "down"


async def test_analyzer_exception_and_timeout_become_failed_items(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "ANALYZER_SELFCHECK_TIMEOUT_S", 0.05)
    boom, hang, good = StubAnalyzer(exc=RuntimeError("x")), StubAnalyzer(hang=True), StubAnalyzer([SelfCheckItem("好", True)])
    result = await sc.run_selfcheck(settings, analyzers=[boom, hang, good], env_file=tmp_path / "x")
    failed = [i for i in result.items if i.name == "link 自检"]
    assert len(failed) == 2
    assert {("RuntimeError" in i.detail, "秒内没有完成" in i.detail) for i in failed} == {(True, False), (False, True)}
    assert names(result)["好"].ok
    assert boom.shut and hang.shut and good.shut


async def test_ffmpeg_missing_is_only_a_hint(make_settings, tmp_path, monkeypatch):
    monkeypatch.setattr(sc.shutil, "which", lambda _: None)
    settings = make_settings(ffmpeg_path=str(tmp_path / "nope"))
    item = sc.ffmpeg_item(settings)
    assert item.state == "hint" and not sc.counts_as_failure(item)
    assert "FFMPEG_PATH" in item.detail
    result = await sc.run_selfcheck(settings, analyzers=[StubAnalyzer([SelfCheckItem("x", True)])], env_file=tmp_path / "x")
    assert sum("ffmpeg" in i.name.lower() for i in result.items) == 1


async def test_analyzer_ffmpeg_item_replaces_generic_one(settings, tmp_path):
    own = StubAnalyzer([SelfCheckItem("ffmpeg（媒体）", True, "/x/ffmpeg")])
    result = await sc.run_selfcheck(settings, analyzers=[own], env_file=tmp_path / "x")
    assert [i.name for i in result.items if "ffmpeg" in i.name.lower()] == ["ffmpeg（媒体）"]


async def test_all_pass_message(make_settings, tmp_path):
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    settings = make_settings(host="127.0.0.1", ffmpeg_path=None)
    result = await sc.run_selfcheck(settings, analyzers=[StubAnalyzer([SelfCheckItem("x", True)])], env_file=env)
    lines = sc.format_result(result)
    assert result.ok, lines
    assert lines[-1].startswith("全部通过")


def test_mock_mode_fails(make_settings, tmp_path):
    items = {i.name: i for i in sc._config_items(make_settings(mock_mode=True), tmp_path / "x")}
    assert sc.counts_as_failure(items["模拟模式"])


def test_main_exit_code(monkeypatch, settings, capsys):
    monkeypatch.setattr(sc, "get_settings", lambda: settings)

    async def fake_run(_settings):
        return sc.SelfCheckResult([SelfCheckItem("主模型", False, "q：未配置密钥", "填密钥", state="not_configured")])

    monkeypatch.setattr(sc, "run_selfcheck", fake_run)
    assert sc.main([]) == 1
    out = capsys.readouterr().out
    assert "[未配置] 主模型" in out and "怎么办：填密钥" in out


# ---- 自检结果 → 健康检查 ----


async def test_load_ignores_stale_and_newer_live_results(settings):
    async with open_services(settings) as services:
        old = LlmHealth()
        old.record(True, model="m", source="selfcheck", at=utcnow() - timedelta(hours=13))
        await sc.save_llm_health(services.db, old)
        fresh = LlmHealth()
        assert not await sc.load_llm_health(services.db, fresh)
        assert fresh.status == "unknown"

        recent = LlmHealth()
        recent.record(False, model="m", detail="timeout", source="selfcheck", at=utcnow() - timedelta(minutes=5))
        await sc.save_llm_health(services.db, recent)
        live = LlmHealth()
        live.record(True, model="m")  # 服务里已有更新的真实调用
        assert not await sc.load_llm_health(services.db, live)
        assert live.status == "ok" and live.source == "call"


@pytest.mark.parametrize("status", ["ok", "down"])
def test_health_endpoint_uses_selfcheck_result(make_client, settings, status):
    async def seed():
        async with open_services(settings) as services:
            h = LlmHealth()
            h.record(status == "ok", model="m", source="selfcheck")
            await sc.save_llm_health(services.db, h)

    asyncio.run(seed())
    client = make_client(settings)
    assert client.get("/v1/health", headers=AUTH).json()["llm"] == status
    assert client.app.state.startup_info["llm_from_selfcheck"] is True


def test_health_endpoint_unknown_without_selfcheck(make_client, settings):
    client = make_client(settings)
    assert client.get("/v1/health", headers=AUTH).json()["llm"] == "unknown"
    assert client.app.state.startup_info["llm_from_selfcheck"] is False


# ---- 局域网地址 ----


def test_listen_all_and_lan_lines(monkeypatch):
    assert listens_on_all("0.0.0.0") and not listens_on_all("::") and not listens_on_all("127.0.0.1")
    import app.netinfo as ni

    monkeypatch.setattr(ni, "lan_addresses", lambda: ["192.168.43.10"])
    lines = lan_lines(8000)
    assert "http://192.168.43.10:8000" in lines[0]
    assert "防火墙" in lines[1]
    monkeypatch.setattr(ni, "lan_addresses", lambda: [])
    assert "未找到" in lan_lines(8000)[0]


def test_lan_addresses_excludes_loopback(monkeypatch):
    import app.netinfo as ni

    class FakeSock:
        def __init__(self, *a):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def connect(self, addr):
            pass

        def getsockname(self):
            return ("192.168.1.7", 5000)

    monkeypatch.setattr(ni.socket, "socket", FakeSock)
    monkeypatch.setattr(
        ni.socket, "getaddrinfo", lambda *a: [(None, None, None, "", ("127.0.0.1", 0)), (None, None, None, "", ("10.0.0.2", 0))]
    )
    assert ni.lan_addresses() == ["10.0.0.2", "192.168.1.7"]

    def offline(*a):
        raise OSError("no route")

    monkeypatch.setattr(FakeSock, "connect", offline)
    monkeypatch.setattr(ni.socket, "getaddrinfo", offline)
    assert ni.lan_addresses() == []


def test_listen_item_on_all_interfaces_is_hint(make_settings, monkeypatch):
    monkeypatch.setattr(sc, "lan_addresses", lambda: ["10.0.0.5"])
    item = sc._listen_item(make_settings(host="0.0.0.0", port=8123))
    assert item.state == "hint" and "http://10.0.0.5:8123" in item.detail


def test_base_url_and_ipv6_listen_item(make_settings):
    from app.netinfo import base_url

    assert base_url("127.0.0.1", 8000) == "http://127.0.0.1:8000"
    assert base_url("::1", 8000) == "http://[::1]:8000"
    item = sc._listen_item(make_settings(host="::", port=8000))
    assert item.state == "hint" and "[::]" in item.detail and "0.0.0.0" in item.hint
    local = sc._listen_item(make_settings(host="::1", port=8000))
    assert local.ok and "http://[::1]:8000" in local.detail


def test_lan_addresses_survives_unicode_error(monkeypatch):
    import app.netinfo as ni

    def bad(*a):
        raise UnicodeError("label too long")

    monkeypatch.setattr(ni.socket, "socket", lambda *a: (_ for _ in ()).throw(OSError("x")))
    monkeypatch.setattr(ni.socket, "getaddrinfo", bad)
    assert ni.lan_addresses() == []


# ---- 评审补充：失败路径 ----


async def test_broken_analyzer_package_fails_selfcheck(settings, tmp_path):
    from app.analyzers import BrokenAnalyzer

    broken = BrokenAnalyzer(AnalyzerName.CASES, "ImportError: x")
    result = await sc.run_selfcheck(settings, analyzers=[broken], env_file=tmp_path / "x")
    item = names(result)["cases 加载"]
    assert not item.ok and "加载失败" in item.detail and not result.ok


async def test_leftover_write_probe_row_still_passes(settings, tmp_path):
    from app.db.tables import StateRecord

    async with open_services(settings) as services:
        async with services.db.session() as session:
            session.add(StateRecord(key="selfcheck_write", value={"ok": True}, updated_at=utcnow()))
            await session.commit()
    result = await sc.run_selfcheck(settings, analyzers=[], env_file=tmp_path / "x")
    assert names(result)["数据库"].ok


async def test_database_open_failure_becomes_item(settings, tmp_path, monkeypatch):
    def broken_open(_settings):
        raise PermissionError("read-only")

    monkeypatch.setattr(sc, "open_services", broken_open)
    stub = StubAnalyzer([SelfCheckItem("x", True)])
    result = await sc.run_selfcheck(settings, analyzers=[stub], env_file=tmp_path / "x")
    items = names(result)
    assert not items["数据库"].ok and "PermissionError" in items["数据库"].detail
    assert "写权限" in items["数据库"].hint
    assert not items["分析器自检"].ok
    assert "监听地址" in items and any("ffmpeg" in n.lower() for n in items)
    assert not result.ok


def test_main_reports_invalid_env_value(monkeypatch, capsys):
    from pydantic import ValidationError

    from app.config import Settings

    def bad_settings():
        Settings(_env_file=None, port="not-a-port")

    monkeypatch.setattr(sc, "get_settings", bad_settings)
    with pytest.raises(ValidationError):
        bad_settings()
    assert sc.main([]) == 1
    out = capsys.readouterr().out
    assert "[失败] 配置文件" in out and "PORT" in out and "怎么办" in out


async def test_no_key_selfcheck_overrides_earlier_ok(settings, tmp_path):
    async with open_services(settings) as services:
        h = LlmHealth()
        h.record(True, model="m", source="selfcheck")
        await sc.save_llm_health(services.db, h)
    await sc.run_selfcheck(settings, analyzers=[ScriptAnalyzer()], env_file=tmp_path / "x")
    async with open_services(settings) as services:
        assert not await sc.load_llm_health(services.db, services.llm_health)
        assert services.llm_health.status == "unknown"


async def test_primary_down_backup_ok_records_ok(settings, tmp_path):
    backends = {"primary": FakeBackend("primary", [LlmError("timeout")]), "backup": FakeBackend("backup", [{"ok": True}])}
    await sc.run_selfcheck(settings, analyzers=[ScriptAnalyzer(backends=backends)], env_file=tmp_path / "x")
    async with open_services(settings) as services:
        assert await sc.load_llm_health(services.db, services.llm_health)
        assert services.llm_health.status == "ok" and services.llm_health.model == "fake-backup"


async def test_both_down_records_down(settings, tmp_path):
    backends = {"primary": FakeBackend("primary", [LlmError("connect")]), "backup": FakeBackend("backup", [LlmError("status_429")])}
    result = await sc.run_selfcheck(settings, analyzers=[ScriptAnalyzer(backends=backends)], env_file=tmp_path / "x")
    items = names(result)
    assert not items["主模型"].ok and not items["备用模型"].ok
    async with open_services(settings) as services:
        assert await sc.load_llm_health(services.db, services.llm_health)
        assert services.llm_health.status == "down"


def test_live_call_result_is_saved_on_shutdown(make_client, settings):
    client = make_client(settings)
    health = client.app.state.services.llm_health
    health.record(False, model="fake-primary", detail="timeout")  # 运行中一次真实调用失败
    client.__exit__(None, None, None)

    async def read():
        async with open_services(settings) as services:
            fresh = LlmHealth()
            assert await sc.load_llm_health(services.db, fresh)
            return fresh

    fresh = asyncio.run(read())
    assert fresh.status == "down" and fresh.source == "call" and fresh.model == "fake-primary"


def test_selfcheck_during_service_run_is_not_overwritten_on_shutdown(make_client, settings, tmp_path):
    client = make_client(settings)
    client.app.state.services.llm_health.record(False, model="fake-primary", detail="timeout")  # 运行中调用失败
    backends = {"primary": FakeBackend("primary", [{"ok": True}])}
    # 服务还在跑，另一个终端里跑自检，主模型调通了（比那次失败的调用更新）
    asyncio.run(sc.run_selfcheck(settings, analyzers=[ScriptAnalyzer(backends=backends)], env_file=tmp_path / "x"))
    client.__exit__(None, None, None)

    restarted = make_client(settings)
    assert restarted.get("/v1/health", headers=AUTH).json()["llm"] == "ok"
    assert restarted.app.state.services.llm_health.source == "selfcheck"


async def test_save_only_if_newer_keeps_newer_stored_row(settings):
    async with open_services(settings) as services:
        newer = LlmHealth()
        newer.record(True, model="m", source="selfcheck")
        await sc.save_llm_health(services.db, newer)
        older = LlmHealth()
        older.record(False, model="m", at=utcnow() - timedelta(minutes=1))
        assert not await sc.save_llm_health(services.db, older, only_if_newer=True)
        fresh = LlmHealth()
        assert await sc.load_llm_health(services.db, fresh)
        assert fresh.status == "ok"
        later = LlmHealth()
        later.record(False, model="m", at=utcnow() + timedelta(seconds=1))
        assert await sc.save_llm_health(services.db, later, only_if_newer=True)


def test_selfcheck_only_run_does_not_resave(make_client, settings):
    client = make_client(settings)
    client.__exit__(None, None, None)

    async def read():
        async with open_services(settings) as services:
            return await sc.load_llm_health(services.db, LlmHealth())

    assert asyncio.run(read()) is False
