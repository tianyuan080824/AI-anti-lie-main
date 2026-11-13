"""防 SSRF：被拒绝的地址一律不发请求；跳转、DNS 结果也逐跳检查；跳数上限、端口限制、不下载正文。

所有请求都经过 respx（`http` 夹具），`http.calls` 记录了实际发出的请求。
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.analyzers.link.extract import parse_url
from app.analyzers.link.net import ExpandTrace, expand, is_public_ip, make_transport
from tests.link.conftest import PUBLIC_IP, FakeResolver, make_input


async def run_expand(url: str, resolver=None, max_hops: int = 5, hop_timeout: float = 1.0, budget: float = 3.0):
    trace = ExpandTrace()
    async with make_transport() as transport:
        await expand(
            parse_url(url),
            trace,
            transport=transport,
            resolver=resolver or FakeResolver(default=[PUBLIC_IP]),
            max_hops=max_hops,
            hop_timeout=hop_timeout,
            deadline=asyncio.get_running_loop().time() + budget,
        )
    return trace


def redirects(table: dict[str, str]):
    """按「Host 头 + 路径」返回 302；不在表里的返回 200。"""

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.headers["host"] + request.url.path
        if key in table:
            return httpx.Response(302, headers={"location": table[key]})
        return httpx.Response(200, text="ok")

    return handler


BLOCKED_TARGETS = [
    "http://127.0.0.1/",
    "http://127.1.2.3/",
    "http://0.0.0.0/",
    "http://10.0.0.1/",
    "http://172.16.0.1/",
    "http://172.31.255.255/",
    "http://192.168.1.1/",
    "http://100.64.0.1/",  # 运营商级 NAT
    "http://169.254.1.1/",  # 链路本地
    "http://169.254.169.254/latest/meta-data/",  # AWS / 腾讯云等的元数据地址
    "http://100.100.100.200/latest/meta-data/",  # 阿里云元数据地址
    "http://[::1]/",
    "http://[fe80::1]/",
    "http://[fc00::1]/",
    "http://[fd00:ec2::254]/",  # AWS IPv6 元数据地址
    "http://[::ffff:127.0.0.1]/",  # 映射到 IPv4 的 IPv6
    "http://[::ffff:169.254.169.254]/",
    "http://[::ffff:7f00:1]/",
    "http://[::127.0.0.1]/",  # 已废弃的 IPv4 兼容写法
    "http://[64:ff9b::a00:1]/",  # NAT64 里嵌着 10.0.0.1
    "http://[2002:a00:1::]/",  # 6to4 里嵌着 10.0.0.1
    "http://2130706433/",  # 十进制的 127.0.0.1
    "http://0x7f000001/",  # 十六进制
    "http://0x7f.0.0.1/",
    "http://0177.0.0.1/",  # 八进制
    "http://127.1/",  # 缩写
    "http://3232235777/",  # 十进制的 192.168.1.1
    "http://2852039166/",  # 十进制的 169.254.169.254
    "http://localhost/",
    "http://metadata.google.internal/",
    "http://router.lan/",
    "http://intranet/",
]


@pytest.mark.parametrize("url", BLOCKED_TARGETS)
async def test_blocked_targets_send_no_request(url, http):
    trace = await run_expand(url)
    assert trace.requests == 0
    assert trace.stopped in ("blocked_address", "blocked_host")
    assert len(http.calls) == 0


@pytest.mark.parametrize("target", BLOCKED_TARGETS)
async def test_redirect_to_blocked_target_is_not_followed(target, http):
    http.route().mock(side_effect=redirects({"t.cn/a": target}))
    trace = await run_expand("https://t.cn/a")
    assert trace.requests == 1  # 只请求了短链本身
    assert [c.request.headers["host"] for c in http.calls] == ["t.cn"]
    assert trace.stopped in ("blocked_address", "blocked_host")
    assert trace.final_url == parse_url(target).to_url()


@pytest.mark.parametrize("ips", [["10.0.0.5"], ["127.0.0.1"], ["169.254.169.254"], ["::1"], [PUBLIC_IP, "192.168.0.2"]])
async def test_dns_resolving_to_private_address_is_blocked(ips, http):
    resolver = FakeResolver({"evil.cc": ips, "t.cn": [PUBLIC_IP]})
    trace = await run_expand("http://evil.cc/x", resolver=resolver)
    assert trace.stopped == "blocked_address" and trace.requests == 0
    # 跳转到解析出内网地址的域名也一样
    http.route().mock(side_effect=redirects({"t.cn/a": "http://evil.cc/x"}))
    trace = await run_expand("https://t.cn/a", resolver=resolver)
    assert trace.stopped == "blocked_address" and trace.requests == 1
    assert [c.request.headers["host"] for c in http.calls] == ["t.cn"]


async def test_dns_failure_sends_nothing(http):
    trace = await run_expand("https://t.cn/a", resolver=FakeResolver())
    assert trace.stopped == "dns_error" and len(http.calls) == 0


async def test_connection_is_pinned_to_resolved_ip(http):
    http.route().mock(return_value=httpx.Response(200))
    await run_expand("https://t.cn/a?x=1", resolver=FakeResolver({"t.cn": [PUBLIC_IP]}))
    (call,) = http.calls
    assert call.request.url.host == PUBLIC_IP  # 连的是解析出的 IP，不会再解析一次（防 DNS 重绑定）
    assert call.request.url.path == "/a" and call.request.url.query == b"x=1"
    assert call.request.headers["host"] == "t.cn"
    assert call.request.extensions["sni_hostname"] == "t.cn"  # TLS 证书仍按原域名校验


async def test_ipv6_target_is_pinned_with_brackets(http):
    http.route().mock(return_value=httpx.Response(200))
    await run_expand("http://t.cn/a", resolver=FakeResolver({"t.cn": ["2606:2800:220:1:248:1893:25c8:1946"]}))
    assert http.calls[0].request.url.host == "2606:2800:220:1:248:1893:25c8:1946"


@pytest.mark.parametrize("url", ["http://t.cn:8080/a", "https://t.cn:22/a", "http://1.1.1.1:3306/"])
async def test_nonstandard_port_rejected(url, http):
    trace = await run_expand(url)
    assert trace.stopped == "blocked_port" and len(http.calls) == 0


async def test_redirect_to_nonstandard_port_rejected(http):
    http.route().mock(side_effect=redirects({"t.cn/a": "http://next.cc:6379/"}))
    trace = await run_expand("https://t.cn/a")
    assert trace.stopped == "blocked_port" and trace.requests == 1


@pytest.mark.parametrize("location", ["ftp://a.cc/x", "file:///etc/passwd", "gopher://a.cc:70/", "javascript:alert(1)"])
async def test_redirect_to_other_scheme_rejected(location, http):
    http.route().mock(side_effect=redirects({"t.cn/a": location}))
    trace = await run_expand("https://t.cn/a")
    assert trace.stopped == "blocked_scheme" and trace.requests == 1


async def test_userinfo_rejected(http):
    trace = await run_expand("http://user:pw@t.cn/a")
    assert trace.stopped == "blocked_userinfo" and len(http.calls) == 0


async def test_hop_limit(http):
    def loop_forever(request: httpx.Request) -> httpx.Response:
        n = int(request.url.path.strip("/") or 0)
        return httpx.Response(302, headers={"location": f"/{n + 1}"})

    http.route().mock(side_effect=loop_forever)
    trace = await run_expand("https://t.cn/0", max_hops=5)
    assert trace.requests == 5 and len(http.calls) == 5
    assert trace.stopped == "max_hops"
    assert trace.redirects == 5


async def test_relative_redirect_and_final_url(http):
    http.route().mock(side_effect=redirects({"t.cn/a": "https://mid.cc/r", "mid.cc/r": "/landing?id=7"}))
    trace = await run_expand("https://t.cn/a")
    assert trace.urls == ["https://t.cn/a", "https://mid.cc/r", "https://mid.cc/landing?id=7"]
    assert trace.stopped == "end" and trace.redirects == 2


async def test_apk_target_is_never_requested(http):
    http.route().mock(side_effect=redirects({"t.cn/a": "https://dl.evil.cc/app.apk"}))
    trace = await run_expand("https://t.cn/a")
    assert trace.stopped == "apk" and trace.requests == 1
    assert trace.final_url == "https://dl.evil.cc/app.apk"


class _TrackedBody(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.read = False
        self.closed = False

    async def __aiter__(self):
        self.read = True
        yield b"x" * 1_000_000

    async def aclose(self) -> None:
        self.closed = True


async def test_body_is_not_downloaded(http):
    body = _TrackedBody()
    http.route().mock(return_value=httpx.Response(200, stream=body))
    trace = await run_expand("https://t.cn/a")
    assert trace.stopped == "end" and trace.requests == 1
    assert body.read is False and body.closed is True


async def test_slow_hop_respects_timeout(http):
    async def slow(request):
        await asyncio.sleep(5)
        return httpx.Response(200)

    http.route().mock(side_effect=slow)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    trace = await run_expand("https://t.cn/a", hop_timeout=0.3)
    assert trace.stopped == "timeout"
    assert loop.time() - t0 < 1.0


async def test_redirect_to_blacklisted_host_is_not_requested(make_analyzer, run_links, http):
    http.route().mock(side_effect=redirects({"t.cn/a": "http://dl.scam-demo.example/page"}))
    analyzer = await make_analyzer(resolver=FakeResolver(default=[PUBLIC_IP]))
    out = await run_links(analyzer, make_input("点 t.cn/a"))
    (link,) = out.links
    assert link.risk.value == "high" and "blacklist_hit" in link.reasons
    assert [c.request.headers["host"] for c in http.calls] == ["t.cn"]


async def test_analyzer_never_requests_private_short_link(make_analyzer, run_links, http):
    resolver = FakeResolver({"t.cn": ["127.0.0.1"]})
    analyzer = await make_analyzer(resolver=resolver)
    out = await run_links(analyzer, make_input("点 https://t.cn/a 和 http://127.0.0.1/x 和 http://169.254.169.254/"))
    assert out.status.value == "done"
    assert len(http.calls) == 0
    assert [l.url for l in out.links] == ["https://t.cn/a", "http://127.0.0.1/x", "http://169.254.169.254/"]
    assert out.extras["details"][0]["expand_stopped"] == "blocked_address"
    assert "ip_host" in out.links[1].reasons


def test_is_public_ip():
    import ipaddress

    assert is_public_ip(ipaddress.ip_address(PUBLIC_IP))
    assert is_public_ip(ipaddress.ip_address("2606:4700::1111"))
    assert not is_public_ip(ipaddress.ip_address("224.0.0.1"))
    assert not is_public_ip(ipaddress.ip_address("::ffff:10.1.1.1"))
