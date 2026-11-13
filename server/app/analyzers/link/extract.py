"""链接的解析与提取：请求里的 url 条目 + 文本行里夹带的链接（含不带协议头的 `abc.cc/x`）。

文本已经脱敏，会有 `<PHONE>`、`<AMOUNT:200>` 这类占位符，也会有「3.5元」「v1.2」「下午3.30」
这类带点的普通文字：不带协议头的候选必须以字母顶级域结尾、并且是公共后缀表里的后缀才算链接。
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import tldextract

from app.analyzers.base import AnalysisInput

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

#: 一次最多体检的链接数（url 条目在前，正文提取的在后）。
MAX_LINKS = 10

#: RFC 2606 / 6761 保留的顶级域。`example.<任意后缀>` 也当作示例域名（文档和测试里常用 example.cn）。
RESERVED_SUFFIXES = ("example", "test", "invalid", "localhost")

# 只用随包自带的后缀表快照，不联网、不写缓存目录。
_TLD = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None, extra_suffixes=RESERVED_SUFFIXES)

_SCHEME_PREFIX = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")
_HOST_PORT_PREFIX = re.compile(r"^[^:/?#@]+:\d+(?:[/?#]|$)")
_HOST_OK = re.compile(r"[a-z0-9_](?:[a-z0-9_-]*[a-z0-9_])?(?:\.[a-z0-9_](?:[a-z0-9_-]*[a-z0-9_])?)*")
_NUMERIC_LABEL = re.compile(r"(?:0x[0-9a-f]*|\d+)", re.I)


class InvalidHost(ValueError):
    pass


@dataclass(frozen=True)
class ParsedUrl:
    raw: str
    scheme: str
    #: ASCII 主机名（国际化域名转成 Punycode）；IP 是规范写法（不带方括号）
    host: str
    #: URL 里显式写出的端口
    port: int | None
    path: str
    query: str
    userinfo: str
    ip: IPAddress | None
    subdomain: str = ""
    domain: str = ""
    suffix: str = ""

    @property
    def registrable_domain(self) -> str | None:
        if self.ip is not None or not self.domain or not self.suffix:
            return None
        return f"{self.domain}.{self.suffix}"

    @property
    def default_port(self) -> int:
        return 443 if self.scheme == "https" else 80

    @property
    def effective_port(self) -> int:
        return self.port if self.port is not None else self.default_port

    @property
    def url_host(self) -> str:
        return f"[{self.host}]" if isinstance(self.ip, ipaddress.IPv6Address) else self.host

    @property
    def host_header(self) -> str:
        if self.port is not None and self.port != self.default_port:
            return f"{self.url_host}:{self.port}"
        return self.url_host

    def to_url(self, host: str | None = None) -> str:
        """规范化后的地址：去掉用户名密码和片段。`host` 用来把主机换成已解析的 IP。"""
        netloc = host or self.url_host
        if self.port is not None and self.port != self.default_port:
            netloc += f":{self.port}"
        query = f"?{self.query}" if self.query else ""
        return f"{self.scheme}://{netloc}{self.path or '/'}{query}"

    @property
    def key(self) -> tuple:
        """去重用：忽略协议头、默认端口、片段和结尾的斜杠。"""
        port = None if self.port in (None, 80, 443) else self.port
        return (self.host, port, self.path.rstrip("/") or "/", self.query)

    @property
    def reserved(self) -> bool:
        """保留的示例域名：不做任何联网查询，只看词法特征和本地黑名单。"""
        if self.ip is not None:
            return False
        last = self.host.rsplit(".", 1)[-1]
        return last in RESERVED_SUFFIXES or last == "local" or self.domain == "example"


def _parse_legacy_ipv4(host: str) -> ipaddress.IPv4Address:
    """按 WHATWG URL 标准解析 IPv4：支持十进制整数、十六进制、八进制和缩写（127.1）。

    不用 `socket.inet_aton`：它在各系统上的宽松程度不一样。
    """
    parts = host.split(".")
    if parts and parts[-1] == "":
        parts.pop()
    if not 1 <= len(parts) <= 4:
        raise InvalidHost(host)
    numbers: list[int] = []
    for part in parts:
        if not part:
            raise InvalidHost(host)
        if part[:2].lower() == "0x":
            base, digits = 16, part[2:] or "0"
        elif len(part) > 1 and part[0] == "0":
            base, digits = 8, part[1:]
        else:
            base, digits = 10, part
        try:
            numbers.append(int(digits, base))
        except ValueError as exc:
            raise InvalidHost(host) from exc
    if any(n > 255 for n in numbers[:-1]) or numbers[-1] >= 256 ** (5 - len(numbers)):
        raise InvalidHost(host)
    value = numbers[-1]
    for i, n in enumerate(numbers[:-1]):
        value += n << (8 * (3 - i))
    return ipaddress.IPv4Address(value)


def parse_ip(host: str) -> IPAddress | None:
    """主机是 IP（任何写法）时返回地址；是域名时返回 None；是不合法的数字主机时抛 InvalidHost。"""
    try:
        return ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        pass
    last = host.rstrip(".").rsplit(".", 1)[-1]
    if _NUMERIC_LABEL.fullmatch(last):
        return _parse_legacy_ipv4(host)
    return None


def _to_ascii(host: str) -> str | None:
    if host.isascii():
        ascii_host = host.lower()
    else:
        try:
            ascii_host = host.encode("idna").decode("ascii").lower()
        except UnicodeError:
            return None
    if len(ascii_host) > 253 or not _HOST_OK.fullmatch(ascii_host):
        return None
    return ascii_host


def parse_url(raw: str) -> ParsedUrl | None:
    """解析一个链接。不是 http/https、或者主机不合法时返回 None（风险记为 unknown）。"""
    text = raw.strip()
    if not text or any(ch.isspace() for ch in text):
        return None
    if "://" not in text:
        if _SCHEME_PREFIX.match(text) and not _HOST_PORT_PREFIX.match(text):
            return None  # mailto:、javascript:、weixin: 之类
        text = "http://" + text.lstrip("/")
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.hostname:
        return None
    hostname = parts.hostname.rstrip(".")
    if not hostname:
        return None
    userinfo = parts.netloc.rpartition("@")[0]
    try:
        ip = parse_ip(hostname)
    except InvalidHost:
        return None
    if ip is not None:
        return ParsedUrl(raw.strip(), scheme, str(ip), port, parts.path, parts.query, userinfo, ip)
    host = _to_ascii(hostname)
    if host is None:
        return None
    ext = _TLD(host)
    return ParsedUrl(
        raw.strip(), scheme, host, port, parts.path, parts.query, userinfo, None, ext.subdomain, ext.domain, ext.suffix
    )


# ---------------------------------------------------------------------------
# 从文本里提取
# ---------------------------------------------------------------------------

_PATH = r"A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%"
# 主机里允许拉丁扩展、希腊、西里尔字母（形近字仿冒），不允许中日韩文字和全角标点，避免把后面的中文吞进来。
_HOST_UNICODE = r"À-ɏͰ-ϿЀ-ӿ"
_WITH_SCHEME = re.compile(
    rf"(?<![A-Za-z0-9])https?://[A-Za-z0-9\-._~%!$&'*+,;=:@\[\]{_HOST_UNICODE}]+(?:[/?#][{_PATH}]*)?",
    re.IGNORECASE,
)
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_BARE = re.compile(
    rf"(?<![A-Za-z0-9@._%+\-/:\\])"
    rf"(?:{_LABEL}\.)+(?:[A-Za-z]{{2,63}}|xn--[A-Za-z0-9-]{{1,59}})"
    rf"(?::\d{{1,5}})?(?![A-Za-z0-9_@-])"
    rf"(?:[/?#][{_PATH}]*)?"
)
# 不带协议头的 IP 链接：必须带端口或路径，否则「1.2.3.4」这类版本号也会被当成链接。
_BARE_IP = re.compile(
    rf"(?<![A-Za-z0-9.:/@])(?:\d{{1,3}}\.){{3}}\d{{1,3}}(?::\d{{1,5}}(?:[/?#][{_PATH}]*)?|[/?#][{_PATH}]*)"
)

#: 不带协议头、也没有路径时，像文件名的「域名」不算链接（这些后缀恰好也是顶级域）。
_FILE_LIKE_TLDS = frozenset({"md", "py", "sh", "rs", "pl", "so", "zip", "mov", "ps", "rar", "pdf", "doc", "csv", "sql"})
#: 「Mr.Wang」「Dr.Li」这类称呼。
_HONORIFICS = frozenset({"mr", "mrs", "ms", "dr", "miss", "prof", "st", "no", "vol", "fig"})

_TRAILING = ".,;:!?'\"*>]}"


def _strip_trailing(candidate: str) -> str:
    while candidate:
        last = candidate[-1]
        if last in _TRAILING:
            candidate = candidate[:-1]
        elif last == ")" and candidate.count("(") < candidate.count(")"):
            candidate = candidate[:-1]
        else:
            break
    return candidate


def _bare_is_link(candidate: str, parsed: ParsedUrl) -> bool:
    if parsed.ip is not None:
        return True
    if not parsed.domain or not parsed.suffix:
        return False
    has_more = parsed.port is not None or parsed.path not in ("", "/") or bool(parsed.query) or candidate.endswith("/")
    if has_more or parsed.host.startswith("www."):
        return True
    if parsed.suffix.rsplit(".", 1)[-1] in _FILE_LIKE_TLDS:
        return False
    if not parsed.subdomain and parsed.domain in _HONORIFICS:
        return False
    return True


def find_links(text: str) -> list[str]:
    """按出现顺序返回文本里的链接（原样，去掉结尾标点）。"""
    found: list[tuple[int, str]] = []
    taken: list[tuple[int, int]] = []
    for m in _WITH_SCHEME.finditer(text):
        candidate = _strip_trailing(m.group(0))
        if parse_url(candidate) is not None:
            found.append((m.start(), candidate))
            taken.append((m.start(), m.start() + len(m.group(0))))

    def overlaps(start: int, end: int) -> bool:
        return any(start < t_end and t_start < end for t_start, t_end in taken)

    for pattern in (_BARE_IP, _BARE):
        for m in pattern.finditer(text):
            if overlaps(m.start(), m.end()):
                continue
            candidate = _strip_trailing(m.group(0))
            parsed = parse_url(candidate)
            if parsed is not None and _bare_is_link(candidate, parsed):
                found.append((m.start(), candidate))
                taken.append((m.start(), m.end()))
    found.sort(key=lambda item: item[0])
    return [c for _, c in found]


@dataclass(frozen=True)
class Candidate:
    url: str
    origin: str | None
    parsed: ParsedUrl | None
    from_text: bool


def collect_links(inp: AnalysisInput, limit: int = MAX_LINKS) -> tuple[list[Candidate], int]:
    """url 条目在前、正文链接在后，去重（保留 url 条目的 origin），最多 `limit` 条。返回 (候选, 去重后总数)。"""
    seen: set = set()
    out: list[Candidate] = []

    def add(url: str, origin: str | None, from_text: bool) -> None:
        parsed = parse_url(url)
        key = parsed.key if parsed is not None else ("raw", url.strip())
        if key in seen:
            return
        seen.add(key)
        out.append(Candidate(url=url.strip(), origin=origin, parsed=parsed, from_text=from_text))

    for item in inp.urls:
        add(item.url, item.origin, False)
    for line in inp.lines:
        for url in find_links(line.text):
            add(url, None, True)
    return out[:limit], len(out)


def has_links(inp: AnalysisInput) -> bool:
    return bool(inp.urls) or any(find_links(line.text) for line in inp.lines)
