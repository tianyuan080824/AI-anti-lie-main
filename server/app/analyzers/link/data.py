"""随代码分发的数据：本地黑名单、短链域名、冷门顶级域、品牌官方域名、RDAP 服务地址表。

全部在 `startup()` 里读一次，不联网。文件格式见 `data/link/README.md`。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from app.analyzers.link.extract import parse_ip, parse_url
from app.paths import DATA_DIR

LINK_DATA_DIR = DATA_DIR / "link"


def _read_list(path: Path) -> list[str]:
    items: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            items.append(line)
    return items


def _normalize_host(entry: str) -> str | None:
    """黑名单条目：域名（可以是中文）或 IP，转成和 ParsedUrl.host 一样的写法。"""
    parsed = parse_url(entry if "://" in entry else f"http://{entry}")
    return parsed.host if parsed is not None else None


@dataclass
class LinkData:
    blacklist: frozenset[str] = frozenset()
    short_links: frozenset[str] = frozenset()
    suspicious_tlds: frozenset[str] = frozenset()
    #: 品牌名（官方域名的主体部分）→ 该品牌的官方可注册域名
    brands: dict[str, frozenset[str]] = field(default_factory=dict)
    #: 所有品牌的官方可注册域名
    official_domains: frozenset[str] = frozenset()
    #: 顶级域 → RDAP 服务地址
    rdap_services: dict[str, tuple[str, ...]] = field(default_factory=dict)
    rdap_publication: str | None = None
    #: 黑名单里写错、解析不了的行（启动时提示）
    invalid_entries: tuple[str, ...] = ()

    @classmethod
    def load(cls, directory: Path = LINK_DATA_DIR) -> "LinkData":
        invalid: list[str] = []

        blacklist: set[str] = set()
        for entry in _read_list(directory / "blacklist.txt"):
            host = _normalize_host(entry)
            if host is None:
                invalid.append(entry)
            else:
                blacklist.add(host)

        short_links = {h for h in (_normalize_host(e) for e in _read_list(directory / "short_links.txt")) if h}
        tlds = {e.lower().lstrip(".") for e in _read_list(directory / "suspicious_tlds.txt")}

        brands: dict[str, set[str]] = {}
        official: set[str] = set()
        for line in _read_list(directory / "brands.txt"):
            parsed_line = []
            for domain in line.lower().split():
                parsed = parse_url(f"http://{domain}")
                if parsed is None or parsed.registrable_domain is None:
                    invalid.append(domain)
                else:
                    parsed_line.append(parsed)
            # 同一行的官方域名属于同一个品牌（icbc.com.cn 和 icbc.com 都是工行的）
            regs = {p.registrable_domain for p in parsed_line}
            official |= regs
            for parsed in parsed_line:
                brands.setdefault(parsed.domain, set()).update(regs)

        rdap: dict[str, tuple[str, ...]] = {}
        publication = None
        rdap_file = directory / "rdap_dns.json"
        if rdap_file.exists():
            doc = json.loads(rdap_file.read_text(encoding="utf-8"))
            publication = doc.get("publication")
            for tlds_of_service, urls in doc.get("services", []):
                https = tuple(u if u.endswith("/") else u + "/" for u in urls if u.startswith("https://"))
                if not https:
                    continue
                for tld in tlds_of_service:
                    rdap[tld.lower()] = https

        return cls(
            blacklist=frozenset(blacklist),
            short_links=frozenset(short_links),
            suspicious_tlds=frozenset(tlds),
            brands={k: frozenset(v) for k, v in brands.items()},
            official_domains=frozenset(official),
            rdap_services=rdap,
            rdap_publication=publication,
            invalid_entries=tuple(invalid),
        )


def host_in(host: str, entries: frozenset[str]) -> bool:
    """`host` 本身或它的某个上级域名在 `entries` 里。IP 只做精确匹配。"""
    if host in entries:
        return True
    try:
        if parse_ip(host) is not None:
            return False
    except ValueError:
        return False
    parts = host.split(".")
    return any(".".join(parts[i:]) in entries for i in range(1, len(parts)))
