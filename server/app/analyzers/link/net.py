"""防 SSRF 的短链展开。

展开短链是替用户访问一个攻击者控制的地址，发起访问的是队员的笔记本，所以每一跳都：

- 只允许 http / https，只访问 80 和 443 端口；
- 先解析 DNS，只要有一个结果是内网、回环、链路本地、云厂商元数据等非公网地址就拒绝；
- 连接到刚才解析出的那个 IP（URL 里换成 IP，Host 头和 TLS 的 SNI / 证书校验仍用原域名），防止 DNS 重绑定；
- 不跟随跳转（自己逐跳处理），不读正文，不走系统代理，不复用连接；
- 最多 `max_hops` 次请求，每次不超过 `hop_timeout` 秒，也不超过整体截止时间。

被拒绝的地址只记下来（它仍会出现在 final_url 里做词法分析），不发请求。
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass, field
from typing import Awaitable, Callable
from urllib.parse import urljoin

import httpx

from app.analyzers.link.extract import IPAddress, ParsedUrl, parse_url
from app.analyzers.link.features import is_apk

Resolver = Callable[[str], Awaitable[list[IPAddress]]]

ALLOWED_PORTS = (80, 443)
USER_AGENT = "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Mobile Safari/537.36"
MAX_LOCATION_CHARS = 2048
#: 连接超时比读取超时短：连不上就尽快放弃。
CONNECT_TIMEOUT_S = 1.5

_NAT64 = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"))
_TUNNELS = (ipaddress.ip_network("2002::/16"), ipaddress.ip_network("2001::/32"))
#: 不解析就直接拒绝的主机名（内网命名、元数据服务的名字）。
_BLOCKED_HOST_SUFFIXES = ("localhost", "local", "internal", "intranet", "lan", "home", "corp", "localdomain", "arpa")


def is_public_ip(ip: IPAddress) -> bool:
    """只有公网单播地址返回 True。云厂商元数据（169.254.169.254、100.100.100.200、fd00:ec2::254）都不是公网地址。"""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return is_public_ip(ip.ipv4_mapped)
        if int(ip) >> 32 == 0:  # ::a.b.c.d（已废弃的 IPv4 兼容地址）和 ::
            return False
        if any(ip in net for net in _NAT64 + _TUNNELS):  # 里面嵌的 IPv4 可能是内网
            return False
    return ip.is_global and not ip.is_multicast


def host_is_blocked_name(host: str) -> bool:
    if "." not in host:
        return True  # 单段主机名只会是内网名字
    return host.rsplit(".", 1)[-1] in _BLOCKED_HOST_SUFFIXES


async def system_resolve(host: str) -> list[IPAddress]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    out: list[IPAddress] = []
    for family, _, _, _, sockaddr in infos:
        if family in (socket.AF_INET, socket.AF_INET6):
            ip = ipaddress.ip_address(str(sockaddr[0]).split("%", 1)[0])
            if ip not in out:
                out.append(ip)
    return out


def make_transport() -> httpx.AsyncHTTPTransport:
    """直接用传输层发请求：`AsyncClient.send` 即使不跟随跳转也会解析 Location，遇到怪异写法会直接抛异常。"""
    return httpx.AsyncHTTPTransport(
        trust_env=False,  # 不走系统代理：走代理就没法把连接钉在解析出的 IP 上
        limits=httpx.Limits(max_keepalive_connections=0),
        retries=0,
    )


class Blocked(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class ExpandTrace:
    """展开过程。逐步写入，被截止时间打断时已经拿到的部分仍然可用。"""

    #: 每一跳的地址（第 0 个是起点；之后是各次跳转的 Location）
    urls: list[str] = field(default_factory=list)
    #: 实际发出的请求数
    requests: int = 0
    #: 停止原因：end（不再跳转）、max_hops、blocked_*、dns_error、network_error、timeout、apk、bad_location
    stopped: str | None = None

    @property
    def redirects(self) -> int:
        return max(0, len(self.urls) - 1)

    @property
    def final_url(self) -> str | None:
        return self.urls[-1] if len(self.urls) > 1 else None


async def check_target(parsed: ParsedUrl, resolver: Resolver, timeout: float) -> IPAddress:
    """校验一跳的目标，返回要连接的 IP。不合格时抛 Blocked。"""
    if parsed.scheme not in ("http", "https"):
        raise Blocked("blocked_scheme")
    if parsed.effective_port not in ALLOWED_PORTS:
        raise Blocked("blocked_port")
    if parsed.userinfo:
        raise Blocked("blocked_userinfo")
    if parsed.ip is not None:
        if not is_public_ip(parsed.ip):
            raise Blocked("blocked_address")
        return parsed.ip
    if host_is_blocked_name(parsed.host):
        raise Blocked("blocked_host")
    try:
        ips = await asyncio.wait_for(resolver(parsed.host), timeout=max(0.01, timeout))
    except (OSError, asyncio.TimeoutError, UnicodeError, ValueError) as exc:
        raise Blocked("dns_error") from exc
    if not ips:
        raise Blocked("dns_error")
    if not all(is_public_ip(ip) for ip in ips):
        raise Blocked("blocked_address")
    return ips[0]


def pinned_request(parsed: ParsedUrl, ip: IPAddress, timeout: float) -> httpx.Request:
    """URL 里的主机换成已解析的 IP；Host 头和 TLS 的 SNI（证书也按它校验）仍是原域名。"""
    ip_host = f"[{ip}]" if isinstance(ip, ipaddress.IPv6Address) else str(ip)
    extensions: dict = {"timeout": httpx.Timeout(timeout, connect=min(CONNECT_TIMEOUT_S, timeout)).as_dict()}
    if parsed.scheme == "https" and parsed.ip is None:
        extensions["sni_hostname"] = parsed.host
    return httpx.Request(
        "GET",
        parsed.to_url(host=ip_host),
        headers={"Host": parsed.host_header, "User-Agent": USER_AGENT, "Accept": "*/*"},
        extensions=extensions,
    )


async def expand(
    start: ParsedUrl,
    trace: ExpandTrace,
    *,
    transport: httpx.AsyncBaseTransport,
    resolver: Resolver,
    max_hops: int,
    hop_timeout: float,
    deadline: float,
    stop_before: Callable[[ParsedUrl], str | None] | None = None,
) -> ExpandTrace:
    """逐跳展开 `start`，结果写进 `trace`。不抛异常（取消除外）。

    `stop_before(url)` 返回非空字符串时不访问这一跳（例如已经命中黑名单，没必要再去碰它）。
    """
    loop = asyncio.get_running_loop()
    trace.urls.append(start.to_url())
    current = start
    while True:
        if is_apk(current):
            trace.stopped = "apk"  # 已经知道是安装包了，不去碰它
            return trace
        if stop_before is not None and trace.urls[1:]:
            reason = stop_before(current)
            if reason:
                trace.stopped = reason
                return trace
        if trace.requests >= max_hops:
            trace.stopped = "max_hops"
            return trace
        timeout = min(hop_timeout, deadline - loop.time())
        if timeout <= 0.05:
            trace.stopped = "timeout"
            return trace
        try:
            ip = await check_target(current, resolver, timeout)
        except Blocked as exc:
            trace.stopped = exc.reason
            return trace
        timeout = min(hop_timeout, deadline - loop.time())
        if timeout <= 0.05:
            trace.stopped = "timeout"
            return trace
        trace.requests += 1
        try:
            response = await asyncio.wait_for(
                transport.handle_async_request(pinned_request(current, ip, timeout)), timeout=timeout
            )
        except asyncio.TimeoutError:
            trace.stopped = "timeout"
            return trace
        except httpx.TimeoutException:
            trace.stopped = "timeout"
            return trace
        except httpx.HTTPError:
            trace.stopped = "network_error"
            return trace
        try:
            location = response.headers.get("location") if response.is_redirect else None
        finally:
            await response.aclose()  # 只读响应头，不读正文
        if not location:
            trace.stopped = "end"
            return trace
        if len(location) > MAX_LOCATION_CHARS:
            trace.stopped = "bad_location"
            return trace
        target = urljoin(current.to_url(), location.strip())
        parsed = parse_url(target)
        if parsed is None:
            trace.urls.append(target[:MAX_LOCATION_CHARS])
            trace.stopped = "blocked_scheme"
            return trace
        trace.urls.append(parsed.to_url())
        current = parsed
