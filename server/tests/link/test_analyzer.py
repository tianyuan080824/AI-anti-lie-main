"""分析器整体：黑名单、外部情报失败、全断网时在预算内返回、到点返回部分结果、端到端、命令行。"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.analyzers.link import create
from app.analyzers.link import __main__ as cli
from app.analyzers.link.intel import ThreatBookProvider
from app.contract import AnalyzerName, AnalyzerStatus, DegradedFlag
from app.pipeline import open_pipeline
from tests.conftest import chat_request
from tests.link.conftest import PUBLIC_IP, FakeResolver, FakeWhois, make_input

TB = ThreatBookProvider.URL


def test_create_and_should_run():
    analyzer = create()
    assert analyzer.name == AnalyzerName.LINK
    assert analyzer.should_run(make_input("加我 abc.cc/x"))
    assert analyzer.should_run(make_input(urls=("https://a.cc",)))
    assert not analyzer.should_run(make_input("一单3.5元 <PHONE> <AMOUNT:200>"))


async def test_blacklist_hit_is_high(make_analyzer, run_links, http):
    analyzer = await make_analyzer()
    out = await run_links(analyzer, make_input("下载 http://dl.scam-demo.example/a"))
    (link,) = out.links
    assert link.risk.value == "high" and link.reasons == ["blacklist_hit"]
    assert link.intel.provider == "local" and link.intel.verdict == "malicious"
    assert out.status == AnalyzerStatus.DONE and out.degraded == []
    assert len(http.calls) == 0  # 保留域名不联网


async def test_low_means_no_features_found(make_analyzer, run_links, http):
    http.route(url__startswith="https://rdap.verisign.com/").mock(
        return_value=httpx.Response(200, json={"events": [{"eventAction": "registration", "eventDate": "2000-01-01T00:00:00Z"}]})
    )
    analyzer = await make_analyzer()
    out = await run_links(analyzer, make_input(urls=("https://www.normal-shop.com/item/1",)))
    (link,) = out.links
    assert link.risk.value == "low" and link.reasons == [] and link.origin == "qrcode"
    assert link.intel.provider == "local" and link.intel.verdict == "unknown"


async def test_unparseable_link_is_unknown(make_analyzer, run_links):
    analyzer = await make_analyzer()
    out = await run_links(analyzer, make_input(urls=("weixin://dl/business",)))
    assert out.links[0].risk.value == "unknown" and out.status == AnalyzerStatus.DONE


async def test_short_link_only_is_medium_even_if_expansion_fails(make_analyzer, run_links, http):
    analyzer = await make_analyzer(resolver=FakeResolver())  # DNS 全失败
    out = await run_links(analyzer, make_input("看看 t.cn/abc"))
    (link,) = out.links
    assert link.risk.value == "medium" and link.reasons == ["short_link"]
    assert link.final_url is None and out.degraded == []
    assert len(http.calls) == 0


async def test_redirect_chain_reason(make_analyzer, run_links, http):
    def site(request):
        host = request.headers["host"]
        if host == "t.cn":
            return httpx.Response(302, headers={"location": "https://hop.example-relay.com/r"})
        if host == "hop.example-relay.com":
            return httpx.Response(302, headers={"location": "https://land.normal-site.com/"})
        return httpx.Response(200)

    http.route(host=PUBLIC_IP).mock(side_effect=site)
    http.route(url__startswith="https://rdap.verisign.com/").mock(return_value=httpx.Response(404))
    analyzer = await make_analyzer(resolver=FakeResolver(default=[PUBLIC_IP]))
    out = await run_links(analyzer, make_input("t.cn/a"))
    (link,) = out.links
    assert link.final_url == "https://land.normal-site.com/"
    assert set(link.reasons) == {"short_link", "redirect_chain"} and link.risk.value == "medium"


async def test_one_redirect_is_not_a_chain(make_analyzer, run_links, http):
    def site(request):
        if request.headers["host"] == "t.cn":
            return httpx.Response(302, headers={"location": "https://land.normal-site.com/"})
        return httpx.Response(200)

    http.route(host=PUBLIC_IP).mock(side_effect=site)
    http.route(url__startswith="https://rdap.verisign.com/").mock(return_value=httpx.Response(404))
    analyzer = await make_analyzer(resolver=FakeResolver(default=[PUBLIC_IP]))
    out = await run_links(analyzer, make_input("t.cn/a"))
    assert "redirect_chain" not in out.links[0].reasons


async def test_expansion_disabled_by_setting(make_settings, make_analyzer, run_links, http):
    from app.services import open_services

    async with open_services(make_settings(link_expand_short_links=False)) as svc:
        analyzer = await make_analyzer(resolver=FakeResolver(default=[PUBLIC_IP]), svc=svc)
        out = await run_links(analyzer, make_input("t.cn/a"))
    assert out.links[0].final_url is None and len(http.calls) == 0


# ---- 外部情报 ----


@pytest.fixture
async def intel_services(make_settings):
    from app.services import open_services

    async with open_services(make_settings(intel_threatbook_api_key="test-key-not-real")) as svc:
        yield svc


async def test_external_intel_malicious(intel_services, make_analyzer, run_links, http):
    route = http.post(TB).mock(
        return_value=httpx.Response(200, json={"response_code": 0, "data": {"domains": {"bad-shop.com": {"is_malicious": True}}}})
    )
    http.route(url__startswith="https://rdap.verisign.com/").mock(return_value=httpx.Response(404))
    analyzer = await make_analyzer(svc=intel_services)
    out = await run_links(analyzer, make_input("https://www.bad-shop.com/pay?id=123"))
    (link,) = out.links
    assert link.risk.value == "high" and "blacklist_hit" in link.reasons
    assert link.intel.provider == "threatbook" and link.intel.verdict == "malicious"
    # 只发可注册域名，不带子域名、路径和参数
    body = route.calls[0].request.content.decode()
    assert "resource=bad-shop.com" in body and "pay" not in body and "www" not in body
    assert out.degraded == []


@pytest.mark.parametrize(
    "response",
    [httpx.ConnectError("offline"), httpx.Response(500), httpx.Response(200, json={"response_code": -1}), httpx.Response(200, text="x")],
)
async def test_external_intel_failure_is_done_with_flag(response, intel_services, make_analyzer, run_links, http):
    route = http.post(TB)
    route.mock(side_effect=response) if isinstance(response, Exception) else route.mock(return_value=response)
    http.route(url__startswith="https://rdap.verisign.com/").mock(return_value=httpx.Response(404))
    analyzer = await make_analyzer(svc=intel_services)
    out = await run_links(analyzer, make_input("https://shop.cc.com/x"))
    assert out.status == AnalyzerStatus.DONE
    assert out.degraded == [DegradedFlag.INTEL_UNAVAILABLE]
    assert out.links[0].risk.value == "low"


async def test_intel_not_queried_without_key(make_analyzer, run_links, http):
    http.route(url__startswith="https://rdap.verisign.com/").mock(return_value=httpx.Response(404))
    tb = http.post(TB)
    analyzer = await make_analyzer()
    out = await run_links(analyzer, make_input("https://shop.cc.com/x"))
    assert tb.call_count == 0 and out.degraded == []


# ---- 断网和超时 ----


async def test_everything_offline_returns_lexical_within_budget(intel_services, make_analyzer, run_links, http):
    http.route().mock(side_effect=httpx.ConnectError("offline"))
    analyzer = await make_analyzer(resolver=FakeResolver(), whois=FakeWhois(exc=OSError("offline")), svc=intel_services)
    inp = make_input(
        "t.cn/abc 和 http://1.2.3.4:8080/a.apk 和 https://alipay-verify.cc/login 和 shop.cn/x 和 dl.scam-demo.example/1"
    )
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    out = await run_links(analyzer, inp, budget=5.0)
    assert loop.time() - t0 < 1.0  # 连不上就立刻放弃，不等满预算
    assert out.status == AnalyzerStatus.DONE
    assert out.degraded == [DegradedFlag.INTEL_UNAVAILABLE]
    risks = {l.url: (l.risk.value, l.reasons) for l in out.links}
    assert risks["t.cn/abc"] == ("medium", ["short_link"])
    assert risks["http://1.2.3.4:8080/a.apk"][0] == "high"
    assert risks["https://alipay-verify.cc/login"][0] == "medium"
    assert risks["shop.cn/x"] == ("low", [])
    assert risks["dl.scam-demo.example/1"][0] == "high"
    assert all(l.domain_age_days is None for l in out.links)


async def test_hanging_network_returns_partial_before_deadline(intel_services, make_analyzer, run_links, http):
    async def hang(*args, **kwargs):
        await asyncio.sleep(30)

    class SlowWhois(FakeWhois):
        async def __call__(self, server, domain, timeout):
            await asyncio.sleep(30)

    http.route().mock(side_effect=hang)
    resolver = FakeResolver(default=[PUBLIC_IP])
    analyzer = await make_analyzer(resolver=resolver, whois=SlowWhois(), svc=intel_services)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    out = await run_links(analyzer, make_input("t.cn/a 和 https://x.com/p 和 y.cn/q"), budget=1.0)
    elapsed = loop.time() - t0
    assert elapsed < 1.0
    assert out.status == AnalyzerStatus.DONE
    assert [l.reasons for l in out.links][0] == ["short_link"]
    assert DegradedFlag.INTEL_UNAVAILABLE in out.degraded  # 已配置的情报没查完
    assert out.extras["timed_out"] is True or all(d["expand_stopped"] == "timeout" for d in out.extras["details"][:1])


async def test_zero_budget_still_returns_lexical(make_analyzer, run_links):
    analyzer = await make_analyzer()
    out = await run_links(analyzer, make_input("http://1.2.3.4/a.apk"), budget=0.0)
    assert out.status == AnalyzerStatus.DONE and out.links[0].risk.value == "high"


# ---- 端到端 ----


async def test_e2e_via_open_pipeline(settings):
    req = chat_request("刷单返利，下载 dl.scam-demo.example/app.apk", "也可以看 https://t.example.cn/a1", urls=("https://t.example.cn/a1",))
    async with open_pipeline(settings) as pipe:
        result = await pipe.analyze(req)
    run = result.runs[AnalyzerName.LINK]
    assert run.status == AnalyzerStatus.DONE
    links = {l.url: l for l in run.output.links}
    assert set(links) == {"https://t.example.cn/a1", "dl.scam-demo.example/app.apk"}
    assert links["https://t.example.cn/a1"].origin == "typed"
    assert links["dl.scam-demo.example/app.apk"].origin is None
    assert links["dl.scam-demo.example/app.apk"].risk.value == "high"
    assert {l.url for l in result.report.links} == set(links)


async def test_e2e_text_only_link_runs(settings):
    async with open_pipeline(settings) as pipe:
        result = await pipe.analyze(chat_request("加我 scam-demo.example/x 领红包"))
    assert result.runs[AnalyzerName.LINK].status == AnalyzerStatus.DONE
    assert result.runs[AnalyzerName.LINK].output.links[0].risk.value == "high"


async def test_e2e_no_links_skipped(settings):
    async with open_pipeline(settings) as pipe:
        result = await pipe.analyze(chat_request("一单3.5元，下午3.30见"))
    assert result.runs[AnalyzerName.LINK].status == AnalyzerStatus.SKIPPED


# ---- 生命周期和命令行 ----


async def test_status_line_and_shutdown(make_analyzer):
    analyzer = await make_analyzer()
    assert "黑名单 3 条" in analyzer.status_line()
    await analyzer.shutdown()
    await analyzer.shutdown()  # 可以重复调用


async def test_selfcheck_offline_reports_failure_not_raise(make_analyzer, services, http):
    http.route().mock(side_effect=httpx.ConnectError("offline"))
    analyzer = await make_analyzer()
    items = await analyzer.selfcheck(services)
    by_name = {i.name: i for i in items}
    assert by_name["链接体检数据"].ok
    assert not by_name["域名注册时间查询"].ok and by_name["域名注册时间查询"].hint


def test_cli_offline(capsys, monkeypatch, make_settings):
    monkeypatch.setattr(cli, "get_settings", lambda: make_settings())
    code = cli.main(["check", "--offline", "http://1.2.3.4:8080/a.apk", "加我 t.cn/abc 领3.5元"])
    out = capsys.readouterr().out
    assert code == 0
    assert "风险：high" in out and "ip_host" in out and "t.cn/abc" in out and "short_link" in out
    assert "状态 done" in out


def test_cli_no_links(capsys, monkeypatch, make_settings):
    monkeypatch.setattr(cli, "get_settings", lambda: make_settings())
    assert cli.main(["check", "--offline", "一单 3.5元"]) == 1
    assert "没有找到链接" in capsys.readouterr().out


async def test_settings_overrides_reach_domain_age_lookup(make_settings, make_analyzer):
    from app.services import open_services

    settings = make_settings(link_rdap_base_url="http://127.0.0.1:9/rdap", link_whois_server="127.0.0.1")
    async with open_services(settings) as svc:
        analyzer = await make_analyzer(svc=svc)
        ages = analyzer._ages
        assert ages is not None
        assert set(ages.rdap_services.values()) == {("http://127.0.0.1:9/rdap/",)}
        assert set(ages.whois_servers.values()) == {"127.0.0.1"}


async def test_timeout_before_intel_marks_intel_unavailable(intel_services, make_analyzer, run_links, http):
    async def hang(*args, **kwargs):
        await asyncio.sleep(30)

    http.route().mock(side_effect=hang)
    tb = http.post(TB)
    analyzer = await make_analyzer(resolver=FakeResolver(default=[PUBLIC_IP]), svc=intel_services)
    out = await run_links(analyzer, make_input("t.cn/a"), budget=0.6)  # 卡在短链展开上，没轮到查情报
    assert tb.call_count == 0
    assert out.extras["timed_out"] is True
    assert out.degraded == [DegradedFlag.INTEL_UNAVAILABLE]
    assert out.extras["details"][0]["intel_failed"] == ["threatbook"]


async def test_timeout_without_external_intel_has_no_flag(make_analyzer, run_links, http):
    async def hang(*args, **kwargs):
        await asyncio.sleep(30)

    http.route().mock(side_effect=hang)
    analyzer = await make_analyzer(resolver=FakeResolver(default=[PUBLIC_IP]))
    out = await run_links(analyzer, make_input("t.cn/a"), budget=0.6)
    assert out.extras["timed_out"] is True and out.degraded == []
