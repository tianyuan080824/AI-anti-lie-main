"""「必须通过的场景」的真实模型版本：读 server/.env 里的密钥，真的调用主备大模型。

只在 `RUN_LIVE=1` 时运行（见 conftest.py），需要网络和真实密钥：

    RUN_LIVE=1 uv run --frozen pytest -m live tests/test_scenarios_live.py
"""

from __future__ import annotations

import socket

import pytest

from app.config import Settings
from app.contract import AnalyzerName, DegradedFlag, Scene, Verdict
from app.pipeline import open_pipeline
from tests.scenarios import BRUSHING, BRUSHING_INJECTED, FAMILY, FUND, GREETING, INVESTMENT, JOB

pytestmark = pytest.mark.live

# 模块在收集阶段导入，早于自动生效的断网夹具，这里拿到的是真实的 connect
_REAL_CONNECT = socket.socket.connect


@pytest.fixture
def live_settings(tmp_path, monkeypatch) -> Settings:
    # 解除默认的断网夹具：这一组要访问真实接口
    monkeypatch.setattr(socket.socket, "connect", _REAL_CONNECT)
    settings = Settings(var_dir=tmp_path / "var")
    if not (settings.llm_primary_api_key or settings.llm_backup_api_key):
        pytest.skip("server/.env 里没有大模型密钥")
    return settings


async def _report(settings, scenario):
    async with open_pipeline(settings) as pipe:
        result = await pipe.analyze(scenario.request())
    # 必须真的用上了大模型：只用规则也可能碰巧得到同样的结论
    assert DegradedFlag.LLM_UNAVAILABLE_RULES_ONLY not in result.report.degraded
    assert any(v["ok"] for v in result.runs[AnalyzerName.SCRIPT].output.extras["llm"].values())
    return result.report


async def test_live_brushing(live_settings):
    r = await _report(live_settings, BRUSHING)
    assert r.verdict == Verdict.HIGH_RISK and r.scene == Scene.BRUSHING_REBATE
    assert r.evidence


async def test_live_investment(live_settings):
    r = await _report(live_settings, INVESTMENT)
    assert r.verdict == Verdict.HIGH_RISK and r.scene == Scene.INVESTMENT
    assert r.stage is not None and r.stage.value == "s5_harvest"


@pytest.mark.parametrize("scenario", [JOB, FAMILY], ids=["job", "family"])
async def test_live_not_high_risk(live_settings, scenario):
    assert (await _report(live_settings, scenario)).verdict != Verdict.HIGH_RISK


async def test_live_fund_low_risk(live_settings):
    assert (await _report(live_settings, FUND)).verdict == Verdict.LOW_RISK


async def test_live_greeting(live_settings):
    assert (await _report(live_settings, GREETING)).verdict == Verdict.INCONCLUSIVE


async def test_live_injection_same_verdict(live_settings):
    a = await _report(live_settings, BRUSHING)
    b = await _report(live_settings, BRUSHING_INJECTED)
    assert (a.verdict, a.scene) == (b.verdict, b.scene)
