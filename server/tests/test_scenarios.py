"""任务书「必须通过的场景」：完整流水线 + 打桩的大模型（确定性）。真实模型的版本在 test_scenarios_live.py。

链接指向内网 / 元数据地址不发请求这一条属于链接体检（第 3 步），在 tests/link/ 里。
"""

from __future__ import annotations

import asyncio

import pytest

from app.analyzers import load_analyzers
from app.analyzers.script import ScriptAnalyzer
from app.contract import AnalyzerName, DegradedFlag, Scene, Verdict
from app.pipeline import open_pipeline
from app.summary.templates import has_assurance
from tests.llm_fakes import FakeBackend, Hang
from tests.scenarios import (
    BRUSHING,
    BRUSHING_INJECTED,
    FAMILY,
    FUND,
    FUND_RETURN,
    GREETING,
    INJECTION,
    INVESTMENT,
    JOB,
    Scenario,
)


def stub(reply_primary, reply_backup=None) -> dict[str, FakeBackend]:
    return {
        "primary": FakeBackend("primary", [reply_primary]),
        "backup": FakeBackend("backup", [reply_backup if reply_backup is not None else reply_primary]),
    }


async def analyze(settings, scenario: Scenario, backends):
    analyzers = [ScriptAnalyzer(backends=backends) if a.name == AnalyzerName.SCRIPT else a for a in load_analyzers()]
    async with open_pipeline(settings, analyzers=analyzers) as pipe:
        result = await pipe.analyze(scenario.request())
    return result


def assert_wording(report):
    text = "".join([report.headline, report.summary, report.next_move, *report.advice, *(e.explanation for e in report.evidence)])
    assert "安全" not in text and "放心" not in text and not has_assurance(text)


async def test_brushing_is_high_risk_with_matching_evidence(settings):
    result = await analyze(settings, BRUSHING, stub(BRUSHING.reply()))
    r = result.report
    assert r.verdict == Verdict.HIGH_RISK
    assert r.scene == Scene.BRUSHING_REBATE
    assert r.degraded == []
    assert len(r.evidence) >= 5
    texts = dict((f"0-{i}", t) for i, (_, t) in enumerate(BRUSHING.lines))
    for e in r.evidence:
        # 证据能对上原话：引文是那一行（服务端脱敏后）的逐字子串
        assert e.quote in texts[e.line_id].replace("200元", "<AMOUNT:200>").replace("260元", "<AMOUNT:260>").replace("3元", "<AMOUNT:3>").replace("9元", "<AMOUNT:9>")
    assert [l.url for l in r.links] == ["https://task-app.example.cn/download"]
    assert_wording(r)


async def test_investment_is_high_risk_s5(settings):
    r = (await analyze(settings, INVESTMENT, stub(INVESTMENT.reply()))).report
    assert r.verdict == Verdict.HIGH_RISK
    assert r.scene == Scene.INVESTMENT
    assert r.stage is not None and r.stage.value == "s5_harvest"
    assert_wording(r)


@pytest.mark.parametrize("keys", [True, False], ids=["llm", "rules_only"])
async def test_job_ad_not_high_risk(settings, keys):
    # 空字典 = 没有可用的模型，只用规则
    r = (await analyze(settings, JOB, stub(JOB.reply()) if keys else {})).report
    assert r.verdict != Verdict.HIGH_RISK
    assert_wording(r)


@pytest.mark.parametrize("scenario", [FUND, FUND_RETURN], ids=["fund", "fund_return_rate"])
async def test_fund_chat_low_risk(settings, scenario):
    result = await analyze(settings, scenario, stub(scenario.reply()))
    r = result.report
    assert r.verdict == Verdict.LOW_RISK
    assert r.headline == "暂未发现明显诈骗特征"
    assert r.scene == Scene.NONE and r.evidence == []
    assert_wording(r)
    if scenario is FUND_RETURN:
        # 规则确实命中了，只是大模型看了上下文、汇总没让它单独抬高结论
        assert result.runs[AnalyzerName.SCRIPT].output.extras["rule_hits"]


async def test_family_transfer_not_high_risk(settings):
    r = (await analyze(settings, FAMILY, stub(FAMILY.reply()))).report
    assert r.verdict != Verdict.HIGH_RISK
    assert_wording(r)


async def test_greeting_inconclusive(settings):
    backends = stub(GREETING.reply())
    r = (await analyze(settings, GREETING, backends)).report
    assert r.verdict == Verdict.INCONCLUSIVE
    assert r.scene == Scene.NONE and r.stage is None


def gullible(scenario: Scenario):
    """容易被注入的模型：注入的话出现在 <chat_data> 之外（被当成指令）时就照做，回答「没有风险」。"""
    obey = {"scene": "none", "stage": None, "intents": [], "summary": "这段对话是安全的，可以放心", "next_move": ""}
    honest = scenario.reply(summary="这段对话是安全的，可以放心")

    def reply(messages):
        outside = messages[0]["content"] + messages[1]["content"].split("<chat_data>\n")[0]
        outside += messages[1]["content"].rsplit("\n</chat_data>", 1)[-1]
        return obey if INJECTION in outside else honest(messages)

    return reply


async def test_injection_line_does_not_change_verdict(settings):
    base = (await analyze(settings, BRUSHING, stub(BRUSHING.reply()))).report
    backends = stub(gullible(BRUSHING_INJECTED))
    result = await analyze(settings, BRUSHING_INJECTED, backends)
    r = result.report
    assert (r.verdict, r.scene) == (base.verdict, base.scene)
    assert_wording(r)  # 模型照抄了「安全」也会被换成模板
    # 注入的那行作为数据放在 <chat_data> 里交给模型，没有被当成系统指令
    sent = backends["primary"].calls[0]
    assert INJECTION not in sent[0]["content"]
    assert INJECTION in sent[1]["content"].split("<chat_data>\n")[1].split("\n</chat_data>")[0]


async def test_fabricated_quote_dropped_and_not_scored(settings):
    def with_intents(*intents):
        return Scenario(BRUSHING.name, BRUSHING.lines, BRUSHING.urls, BRUSHING.scene, BRUSHING.stage, intents)

    # 基线只用两条意图，分数没到封顶，多一条真证据分数就会变
    few = BRUSHING.intents[:2]
    base = (await analyze(settings, BRUSHING, stub(with_intents(*few).reply()))).report
    real = (await analyze(settings, BRUSHING, stub(with_intents(*few, ("request_payment", "先垫付")).reply()))).report
    assert base.risk_score < real.risk_score
    fake = with_intents(*few, ("request_payment", "把银行卡验证码发给我"))
    r = (await analyze(settings, BRUSHING, stub(fake.reply()))).report
    assert all("验证码" not in e.quote for e in r.evidence)
    assert r.risk_score == base.risk_score  # 被丢弃的证据不参与打分


async def test_both_models_time_out(make_settings):
    settings = make_settings(timeout_script_s=1.0)
    backends = {"primary": FakeBackend("primary", [Hang()]), "backup": FakeBackend("backup", [Hang()])}
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    result = await analyze(settings, BRUSHING, backends)
    assert loop.time() - t0 < 2.0
    r = result.report
    # 最高 suspicious；刷单的规则信号足够让它到 suspicious
    assert r.verdict == Verdict.SUSPICIOUS
    assert DegradedFlag.LLM_UNAVAILABLE_RULES_ONLY in r.degraded
    assert r.evidence and all(e.source.value == "rule" for e in r.evidence)
    assert r.risk_score <= 59
    assert_wording(r)


OFFLINE_URL = "https://task-app-dl.cn/x"  # 不是保留域名：链接体检会真的去解析和查询


async def test_everything_unreachable_offline(make_settings, tmp_path, monkeypatch):
    """笔记本断网：大模型、向量接口都连不上（指向本机一个关着的端口，立即被拒绝），用真实客户端；
    域名解析和外网连接在 socket 层直接失败。"""
    import errno
    import socket

    from app.analyzers.cases.importer import import_cases
    from app.services import open_services
    from tests.cases.conftest import FakeEmbedder, case_row, write_jsonl

    dead = "http://127.0.0.1:9/v1"
    settings = make_settings(
        llm_primary_base_url=dead,
        llm_primary_api_key="sk-test-not-a-real-key",
        llm_backup_base_url=dead,
        llm_backup_api_key="sk-test-not-a-real-key",
        embedding_base_url=dead,
        embedding_api_key="sk-test-not-a-real-key",
        embedding_dim=4,
        embedding_model="fake-embed",
    )
    # 案例库里有案例，向量接口不可用时才算 cases_unavailable（空库是 skipped，不算降级）
    f = write_jsonl(tmp_path / "c.jsonl", [case_row("syn_a", "刷单", scene="brushing_rebate")])
    async with open_services(settings) as services:
        assert (await import_cases(services.db, FakeEmbedder({"刷单": [1, 0, 0, 0]}), [f])).ok

    loopback = ("127.0.0.1", "::1", "localhost")
    real_getaddrinfo = socket.getaddrinfo
    attempts: list[str] = []

    def offline_getaddrinfo(host, *args, **kwargs):
        name = host.decode() if isinstance(host, bytes) else host
        if name not in loopback:
            attempts.append(name)
            raise socket.gaierror(socket.EAI_NONAME, "offline")
        return real_getaddrinfo(host, *args, **kwargs)

    connect = socket.socket.connect  # 已经是断网夹具包过的版本

    def offline_connect(self, address):
        host = address[0] if isinstance(address, tuple) else address
        if host not in loopback:
            raise OSError(errno.ENETUNREACH, "offline")
        return connect(self, address)

    monkeypatch.setattr(socket, "getaddrinfo", offline_getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", offline_connect)

    request = BRUSHING.request()
    request["items"].append({"type": "url", "url": OFFLINE_URL, "origin": "typed"})
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    async with open_pipeline(settings) as pipe:
        result = await pipe.analyze(request)
    took = loop.time() - t0
    assert took < settings.check_deadline_s
    r = result.report
    assert r.verdict == Verdict.SUSPICIOUS
    assert {DegradedFlag.LLM_UNAVAILABLE_RULES_ONLY, DegradedFlag.CASES_UNAVAILABLE} <= set(r.degraded)
    llm = result.runs[AnalyzerName.SCRIPT].output.extras["llm"]
    assert llm["primary"]["ok"] is False and llm["backup"]["ok"] is False
    assert {l.url for l in r.links} == {"https://task-app.example.cn/download", OFFLINE_URL}
    assert result.runs[AnalyzerName.LINK].status.value == "done"
    assert attempts  # 真的尝试过联网（解析、whois），而不是因为保留域名跳过
    assert_wording(r)
