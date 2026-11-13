"""域名注册时间：RDAP 优先（走 HTTPS），`.cn` 没有 RDAP 服务，用 CNNIC 的 WHOIS（43 端口）兜底。

- RDAP 服务地址取自随代码分发的 IANA 表（`data/link/rdap_dns.json`），不在请求时现取；
- 查到注册时间才写缓存（`link_domain_cache`，保留 `link_domain_cache_days` 天）；
- 查不到、超时、断网一律返回 None，不估计。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

import httpx
from sqlalchemy import delete, select

from app.analyzers.link.models import DomainCache
from app.db import Database, utcnow

#: 没有 RDAP 服务、改用 WHOIS 的顶级域
WHOIS_SERVERS = {"cn": "whois.cnnic.cn"}
_WHOIS_MAX_BYTES = 64 * 1024
_WHOIS_TIME = re.compile(r"^Registration Time:\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*$", re.M)
#: CNNIC 的 WHOIS 时间是北京时间
_CST = timezone(timedelta(hours=8))

WhoisQuery = Callable[[str, str, float], Awaitable[str]]


@dataclass(frozen=True)
class DomainAge:
    created_at: datetime
    source: str  # rdap / whois / cache

    def age_days(self, now: datetime | None = None) -> int:
        return max(0, ((now or utcnow()) - self.created_at).days)


def parse_rdap_created(doc: object) -> datetime | None:
    """从 RDAP 域名对象里取 `registration` 事件的时间。"""
    if not isinstance(doc, dict):
        return None
    for event in doc.get("events") or []:
        if not isinstance(event, dict) or event.get("eventAction") != "registration":
            continue
        raw = event.get("eventDate")
        if not isinstance(raw, str):
            continue
        try:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return None


def parse_whois_created(text: str) -> datetime | None:
    m = _WHOIS_TIME.search(text)
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=_CST).astimezone(timezone.utc)


async def whois_query(server: str, domain: str, timeout: float) -> str:
    async def query() -> str:
        reader, writer = await asyncio.open_connection(server, 43)
        try:
            writer.write(domain.encode("ascii") + b"\r\n")
            await writer.drain()
            data = await reader.read(_WHOIS_MAX_BYTES)
            chunks = [data]
            while data and sum(map(len, chunks)) < _WHOIS_MAX_BYTES:
                data = await reader.read(_WHOIS_MAX_BYTES)
                chunks.append(data)
            return b"".join(chunks).decode("utf-8", "replace")
        finally:
            writer.close()

    return await asyncio.wait_for(query(), timeout=timeout)


class DomainAgeLookup:
    def __init__(
        self,
        rdap_services: dict[str, tuple[str, ...]],
        client: httpx.AsyncClient,
        *,
        rdap_timeout: float,
        whois_timeout: float,
        cache_days: float,
        whois: WhoisQuery = whois_query,
        rdap_base_url: str | None = None,
        whois_server: str | None = None,
    ) -> None:
        """`rdap_base_url` / `whois_server`：覆盖所有 RDAP 服务地址和 WHOIS 服务器（断网模拟用）。"""
        if rdap_base_url:
            base = rdap_base_url if rdap_base_url.endswith("/") else rdap_base_url + "/"
            rdap_services = {tld: (base,) for tld in rdap_services}
        self.whois_servers = {tld: whois_server for tld in WHOIS_SERVERS} if whois_server else dict(WHOIS_SERVERS)
        self.rdap_services = rdap_services
        self.client = client
        self.rdap_timeout = rdap_timeout
        self.whois_timeout = whois_timeout
        self.cache_days = cache_days
        self.whois = whois

    async def cached(self, db: Database | None, domain: str) -> DomainAge | None:
        if db is None:
            return None
        cutoff = utcnow() - timedelta(days=self.cache_days)
        async with db.session() as session:
            row = await session.get(DomainCache, domain)
        if row is None or row.created_at is None or row.checked_at < cutoff:
            return None
        return DomainAge(row.created_at, "cache")

    async def store(self, db: Database | None, domain: str, age: DomainAge) -> None:
        if db is None:
            return
        async with db.session() as session:
            await session.merge(
                DomainCache(domain=domain, created_at=age.created_at, source=age.source, checked_at=utcnow())
            )
            await session.commit()

    async def rdap(self, domain: str, timeout: float) -> datetime | None:
        tld = domain.rsplit(".", 1)[-1]
        for base in self.rdap_services.get(tld, ())[:1]:
            response = await self.client.get(
                f"{base}domain/{domain}",
                headers={"Accept": "application/rdap+json, application/json"},
                timeout=httpx.Timeout(timeout, connect=min(1.5, timeout)),
            )
            if response.status_code != 200:
                return None
            return parse_rdap_created(response.json())
        return None

    async def lookup(self, domain: str, *, db: Database | None, budget: float) -> DomainAge | None:
        """查一个可注册域名的注册时间。任何失败都返回 None，不抛异常（取消除外）。"""
        try:
            hit = await self.cached(db, domain)
        except Exception:
            hit = None
        if hit is not None:
            return hit
        tld = domain.rsplit(".", 1)[-1]
        created: datetime | None = None
        source = ""
        try:
            if tld in self.rdap_services:
                created, source = await self.rdap(domain, min(self.rdap_timeout, budget)), "rdap"
            elif tld in self.whois_servers:
                text = await self.whois(self.whois_servers[tld], domain, min(self.whois_timeout, budget))
                created, source = parse_whois_created(text), "whois"
        except Exception:
            return None
        if created is None:
            return None
        age = DomainAge(created, source)
        try:
            await self.store(db, domain, age)
        except Exception:
            pass
        return age


async def purge_cache(db: Database, cache_days: float) -> int:
    cutoff = utcnow() - timedelta(days=cache_days)
    async with db.session() as session:
        result = await session.execute(delete(DomainCache).where(DomainCache.checked_at < cutoff))
        await session.commit()
    return result.rowcount or 0


async def count_cache(db: Database) -> int:
    async with db.session() as session:
        return len((await session.execute(select(DomainCache.domain))).all())
