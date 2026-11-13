"""链接体检测试的夹具。

- `no_real_lookups`（自动生效）：系统 DNS 解析和 WHOIS 在本目录的任何测试里都会直接报错；
- `http`（自动生效）：respx 拦截所有 httpx 请求，没有打桩的请求会失败，`http.calls` 能断言「没有发出请求」；
- `make_analyzer(...)`：带假解析器、假 WHOIS 的分析器，已 startup；
- `run_links(...)`：直接调用 run()，可以指定预算。
"""

from __future__ import annotations

import asyncio
import ipaddress
from typing import Any, Callable

import pytest
import respx

from app.analyzers.base import AnalysisInput, AnalyzerContext, InputLine, UrlInput
from app.analyzers.link import analyzer as analyzer_mod
from app.analyzers.link.analyzer import LinkAnalyzer
from app.contract import Speaker
from app.services import open_services

#: 测试里当作「公网」的地址（respx 拦截，不会真的连接）
PUBLIC_IP = "93.184.215.14"


class FakeResolver:
    """按主机名返回固定地址，记录查询过的主机名。没登记的主机名按 DNS 失败处理。"""

    def __init__(self, table: dict[str, list[str]] | None = None, default: list[str] | None = None):
        self.table = dict(table or {})
        self.default = default
        self.calls: list[str] = []

    async def __call__(self, host: str):
        self.calls.append(host)
        ips = self.table.get(host, self.default)
        if ips is None:
            raise OSError(f"no such host {host}")
        return [ipaddress.ip_address(ip) for ip in ips]


class FakeWhois:
    def __init__(self, text: str | None = None, exc: Exception | None = None):
        self.text = text
        self.exc = exc
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, server: str, domain: str, timeout: float) -> str:
        self.calls.append((server, domain))
        if self.exc is not None:
            raise self.exc
        return self.text or ""


@pytest.fixture(autouse=True)
def no_real_lookups(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail_resolve(host: str):
        raise AssertionError(f"tests must not use real DNS (tried {host!r})")

    async def fail_whois(server: str, domain: str, timeout: float) -> str:
        raise AssertionError(f"tests must not use real WHOIS (tried {server!r})")

    monkeypatch.setattr(analyzer_mod, "system_resolve", fail_resolve)
    monkeypatch.setattr(analyzer_mod, "whois_query", fail_whois)


@pytest.fixture(autouse=True)
def http():
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        yield router


def make_input(*texts: str, urls: tuple[str, ...] = (), origin: str = "qrcode", speakers=None) -> AnalysisInput:
    lines = tuple(
        InputLine(
            line_id=f"0-{i}",
            speaker=(speakers[i] if speakers else Speaker.OTHER),
            text=t,
            item_index=0,
        )
        for i, t in enumerate(texts)
    )
    url_items = tuple(UrlInput(url=u, origin=origin, item_index=i + 1) for i, u in enumerate(urls))
    return AnalysisInput(check_id="c_test", trace_id="t_test", lines=lines, urls=url_items)


@pytest.fixture
async def services(settings):
    async with open_services(settings) as svc:
        yield svc


@pytest.fixture
async def make_analyzer(services):
    created: list[LinkAnalyzer] = []

    async def factory(resolver: Any = None, whois: Any = None, svc: Any = None, **kwargs: Any) -> LinkAnalyzer:
        analyzer = LinkAnalyzer(resolver=resolver or FakeResolver(), whois=whois or FakeWhois(), **kwargs)
        await analyzer.startup(svc or services)
        created.append(analyzer)
        return analyzer

    yield factory
    for analyzer in created:
        await analyzer.shutdown()


@pytest.fixture
def run_links() -> Callable:
    """直接调用 run()；用分析器 startup 时拿到的 services。"""

    async def runner(analyzer: LinkAnalyzer, inp: AnalysisInput, budget: float | None = 5.0):
        loop = asyncio.get_running_loop()
        ctx = AnalyzerContext(
            services=analyzer.services,
            check_id=inp.check_id,
            trace_id=inp.trace_id,
            deadline=None if budget is None else loop.time() + budget,
        )
        return await analyzer.run(inp, ctx)

    return runner
