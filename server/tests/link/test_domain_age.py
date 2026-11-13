"""域名注册时间：RDAP 解析、缓存、WHOIS 兜底、查询失败返回空。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.analyzers.link.domain_age import (
    DomainAge,
    DomainAgeLookup,
    count_cache,
    parse_rdap_created,
    parse_whois_created,
    purge_cache,
)
from app.analyzers.link.models import DomainCache
from app.db import utcnow
from tests.link.conftest import PUBLIC_IP, FakeResolver, FakeWhois, make_input

RDAP_COM = "https://rdap.verisign.com/com/v1/"
CNNIC_SAMPLE = """Domain Name: cnnic.cn
ROID: 20030310s10001s00012956-cn
Registration Time: 2003-03-10 19:06:34
Expiration Time: 2028-03-10 19:06:34
"""


def rdap_doc(created: str | None) -> dict:
    events = [{"eventAction": "expiration", "eventDate": "2030-01-01T00:00:00Z"}]
    if created:
        events.append({"eventAction": "registration", "eventDate": created})
    return {"objectClassName": "domain", "ldhName": "X.COM", "events": events}


def test_parse_rdap_created():
    assert parse_rdap_created(rdap_doc("1997-09-15T04:00:00Z")) == datetime(1997, 9, 15, 4, tzinfo=timezone.utc)
    assert parse_rdap_created(rdap_doc("2026-11-01T08:00:00+08:00")) == datetime(2026, 11, 1, 0, tzinfo=timezone.utc)
    assert parse_rdap_created(rdap_doc(None)) is None
    assert parse_rdap_created(rdap_doc("not a date")) is None
    assert parse_rdap_created({"events": "oops"}) is None
    assert parse_rdap_created([]) is None


def test_parse_whois_created():
    # CNNIC 的时间是北京时间
    assert parse_whois_created(CNNIC_SAMPLE) == datetime(2003, 3, 10, 11, 6, 34, tzinfo=timezone.utc)
    assert parse_whois_created("No matching record.") is None


@pytest.fixture
async def lookup(services):
    client = httpx.AsyncClient()
    yield DomainAgeLookup(
        {"com": (RDAP_COM,)}, client, rdap_timeout=1, whois_timeout=1, cache_days=7, whois=FakeWhois(CNNIC_SAMPLE)
    )
    await client.aclose()


async def test_rdap_lookup_and_cache(lookup, services, http):
    route = http.get(f"{RDAP_COM}domain/new-shop.com").mock(
        return_value=httpx.Response(200, json=rdap_doc((utcnow() - timedelta(days=6)).strftime("%Y-%m-%dT%H:%M:%SZ")))
    )
    age = await lookup.lookup("new-shop.com", db=services.db, budget=2)
    assert age is not None and age.source == "rdap" and age.age_days() == 6
    again = await lookup.lookup("new-shop.com", db=services.db, budget=2)
    assert again is not None and again.source == "cache" and again.age_days() == 6
    assert route.call_count == 1
    assert await count_cache(services.db) == 1


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404),
        httpx.Response(500),
        httpx.Response(200, text="<html>"),
        httpx.Response(200, json=rdap_doc(None)),
        httpx.ConnectError("offline"),
        httpx.ReadTimeout("slow"),
    ],
)
async def test_rdap_failures_return_none_and_are_not_cached(response, lookup, services, http):
    route = http.get(f"{RDAP_COM}domain/x.com")
    if isinstance(response, Exception):
        route.mock(side_effect=response)
    else:
        route.mock(return_value=response)
    assert await lookup.lookup("x.com", db=services.db, budget=2) is None
    assert await count_cache(services.db) == 0


async def test_unknown_tld_returns_none_without_request(lookup, services, http):
    assert await lookup.lookup("x.zz", db=services.db, budget=2) is None
    assert len(http.calls) == 0


async def test_whois_fallback_for_cn(lookup, services, http):
    age = await lookup.lookup("cnnic.cn", db=services.db, budget=2)
    assert age is not None and age.source == "whois"
    assert lookup.whois.calls == [("whois.cnnic.cn", "cnnic.cn")]
    assert len(http.calls) == 0


async def test_whois_failure_returns_none(services):
    async with httpx.AsyncClient() as client:
        lk = DomainAgeLookup({}, client, rdap_timeout=1, whois_timeout=1, cache_days=7, whois=FakeWhois(exc=OSError()))
        assert await lk.lookup("a.cn", db=services.db, budget=2) is None
        lk.whois = FakeWhois("No matching record.")
        assert await lk.lookup("a.cn", db=services.db, budget=2) is None


async def test_expired_cache_is_ignored_and_purged(lookup, services, http):
    old = utcnow() - timedelta(days=30)
    async with services.db.session() as session:
        session.add(DomainCache(domain="stale.com", created_at=old, source="rdap", checked_at=old))
        await session.commit()
    http.get(f"{RDAP_COM}domain/stale.com").mock(side_effect=httpx.ConnectError("offline"))
    assert await lookup.lookup("stale.com", db=services.db, budget=2) is None
    assert await purge_cache(services.db, 7) == 1
    assert await count_cache(services.db) == 0


async def test_cleanup_hook_purges(make_analyzer, services):
    old = utcnow() - timedelta(days=30)
    async with services.db.session() as session:
        session.add(DomainCache(domain="stale.com", created_at=old, source="rdap", checked_at=old))
        session.add(DomainCache(domain="fresh.com", created_at=old, source="rdap", checked_at=utcnow()))
        await session.commit()
    analyzer = await make_analyzer()
    await analyzer.cleanup(services)
    assert await count_cache(services.db) == 1


def test_age_days_never_negative():
    assert DomainAge(utcnow() + timedelta(days=2), "rdap").age_days() == 0


# ---- 接进分析器 ----


async def test_new_domain_adds_reason(make_analyzer, run_links, http):
    created = (utcnow() - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    http.get(f"{RDAP_COM}domain/fresh-shop.com").mock(return_value=httpx.Response(200, json=rdap_doc(created)))
    analyzer = await make_analyzer()
    out = await run_links(analyzer, make_input("看看 https://fresh-shop.com/x"))
    (link,) = out.links
    assert link.domain_age_days == 3 and link.reasons == ["domain_age_lt_30d"] and link.risk.value == "medium"


async def test_old_domain_no_reason(make_analyzer, run_links, http):
    http.get(f"{RDAP_COM}domain/old-shop.com").mock(return_value=httpx.Response(200, json=rdap_doc("2001-01-01T00:00:00Z")))
    analyzer = await make_analyzer()
    out = await run_links(analyzer, make_input("看看 https://old-shop.com/x"))
    (link,) = out.links
    assert link.domain_age_days > 30 and link.reasons == [] and link.risk.value == "low"


async def test_rdap_queries_registrable_domain_of_final_url(make_analyzer, run_links, http):
    created = (utcnow() - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    def site(request: httpx.Request) -> httpx.Response:
        if request.headers["host"] == "t.cn":
            return httpx.Response(302, headers={"location": "https://dl.brand-new.com/app"})
        return httpx.Response(200)

    http.route(host=PUBLIC_IP).mock(side_effect=site)
    rdap = http.get(f"{RDAP_COM}domain/brand-new.com").mock(return_value=httpx.Response(200, json=rdap_doc(created)))
    analyzer = await make_analyzer(resolver=FakeResolver(default=[PUBLIC_IP]))
    out = await run_links(analyzer, make_input("t.cn/a"))
    (link,) = out.links
    assert rdap.call_count == 1
    assert link.registrable_domain == "brand-new.com" and link.domain_age_days == 5


async def test_simulation_overrides_point_all_lookups_elsewhere(services, http):
    base = "http://127.0.0.1:9/rdap"
    route = http.get(f"{base}/domain/new-shop.com").mock(side_effect=httpx.ConnectError("refused"))
    whois = FakeWhois(exc=ConnectionRefusedError())
    async with httpx.AsyncClient() as client:
        lk = DomainAgeLookup(
            {"com": (RDAP_COM,), "net": (RDAP_COM,)},
            client,
            rdap_timeout=1,
            whois_timeout=1,
            cache_days=7,
            whois=whois,
            rdap_base_url=base,
            whois_server="127.0.0.1",
        )
        assert lk.rdap_services == {"com": (base + "/",), "net": (base + "/",)}
        assert await lk.lookup("new-shop.com", db=services.db, budget=2) is None
        assert await lk.lookup("cnnic.cn", db=services.db, budget=2) is None
    assert route.called
    assert whois.calls == [("127.0.0.1", "cnnic.cn")]


def test_no_override_keeps_real_servers():
    lk = DomainAgeLookup({"com": (RDAP_COM,)}, None, rdap_timeout=1, whois_timeout=1, cache_days=7)
    assert lk.rdap_services == {"com": (RDAP_COM,)}
    assert lk.whois_servers["cn"] == "whois.cnnic.cn"
