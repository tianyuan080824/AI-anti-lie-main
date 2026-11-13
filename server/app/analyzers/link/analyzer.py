"""链接体检 `link`。

每条链接并行处理，结果逐步写进 `_LinkState`：

1. 解析 + 词法特征 + 本地黑名单（不联网，立即可得）；
2. 公开短链服务的链接：防 SSRF 地逐跳展开（`net.py`）；
3. 最终地址的可注册域名：查注册时间（RDAP / WHOIS，带缓存）和已配置的外部情报，两者并行。

整路预算由流水线给（`ctx.remaining_s()`）。快到点时取消还没做完的步骤，用已经得到的结果出报告，
所以至少有词法特征和本地黑名单，这一路仍是 `done`。保留的示例域名（`.example`、`example.cn` 等）不做任何联网查询。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

import httpx

from app.analyzers.base import Analyzer, AnalysisInput, AnalyzerContext, AnalyzerOutput, SelfCheckItem
from app.analyzers.link import intel as intel_mod
from app.analyzers.link.data import LinkData, host_in
from app.analyzers.link.domain_age import DomainAge, DomainAgeLookup, WhoisQuery, purge_cache, whois_query
from app.analyzers.link.extract import Candidate, ParsedUrl, collect_links, has_links, parse_url
from app.analyzers.link.features import assess_risk, lexical_reasons, sort_reasons
from app.analyzers.link.net import ExpandTrace, Resolver, expand, make_transport, system_resolve
from app.contract import AnalyzerName, AnalyzerStatus, DegradedFlag, LinkIntel, LinkReason, LinkResult, LinkRisk
from app.db import Database
from app.services import Services

#: 留给组装结果的时间：内部截止 = 流水线截止 − 这个值
ASSEMBLY_MARGIN_S = 0.3
#: 跳转次数达到这个数（不含起点）算 redirect_chain。短链跳一次到目标是正常的。
REDIRECT_CHAIN_MIN = 2
DOMAIN_AGE_NEW_DAYS = 30


@dataclass
class _LinkState:
    cand: Candidate
    reasons: set[LinkReason] = field(default_factory=set)
    trace: ExpandTrace = field(default_factory=ExpandTrace)
    final: ParsedUrl | None = None
    age: DomainAge | None = None
    #: provider → verdict（含 local）
    verdicts: dict[str, str] = field(default_factory=dict)
    intel_failed: list[str] = field(default_factory=list)
    intel_pending: set[str] = field(default_factory=set)
    steps: list[str] = field(default_factory=list)

    @property
    def target(self) -> ParsedUrl | None:
        return self.final or self.cand.parsed

    def hosts(self) -> list[str]:
        out = [self.cand.parsed.host] if self.cand.parsed else []
        for url in self.trace.urls[1:]:
            parsed = parse_url(url)
            if parsed is not None:
                out.append(parsed.host)
        return out


class LinkAnalyzer(Analyzer):
    name = AnalyzerName.LINK
    failure_flags = (DegradedFlag.INTEL_UNAVAILABLE,)

    def __init__(
        self,
        *,
        resolver: Resolver | None = None,
        whois: WhoisQuery | None = None,
        data: LinkData | None = None,
        offline: bool = False,
    ) -> None:
        self.resolver: Resolver = resolver or system_resolve
        self.whois: WhoisQuery = whois or whois_query
        self.data: LinkData | None = data
        #: True：只做词法特征和本地黑名单，不发任何请求（命令行的 --offline）
        self.offline = offline
        self.services: Services | None = None
        self._fetch: httpx.AsyncBaseTransport | None = None
        self._api_client: httpx.AsyncClient | None = None
        self._external: list[intel_mod.DomainIntelProvider] = []
        self._ages: DomainAgeLookup | None = None

    # ---- 生命周期 ----

    async def startup(self, services: Services) -> None:
        self.services = services
        if self.data is None:
            self.data = await asyncio.to_thread(LinkData.load)
        settings = services.settings
        if self._fetch is None:
            self._fetch = make_transport()
        if self._api_client is None:
            self._api_client = httpx.AsyncClient(follow_redirects=False, headers={"User-Agent": "antifraud-link/1"})
        self._external = intel_mod.build_external(settings, self._api_client)
        self._ages = DomainAgeLookup(
            self.data.rdap_services,
            self._api_client,
            rdap_timeout=settings.link_rdap_timeout_s,
            whois_timeout=settings.link_whois_timeout_s,
            cache_days=settings.link_domain_cache_days,
            whois=self.whois,
            rdap_base_url=settings.link_rdap_base_url,
            whois_server=settings.link_whois_server,
        )

    async def shutdown(self) -> None:
        if self._fetch is not None:
            await self._fetch.aclose()
        if self._api_client is not None:
            await self._api_client.aclose()
        self._fetch = self._api_client = None

    async def cleanup(self, services: Services) -> None:
        await purge_cache(services.db, services.settings.link_domain_cache_days)

    def status_line(self) -> str:
        if self.data is None:
            return "未加载"
        configured = [k for k, v in intel_mod.configured_external(self.services.settings).items() if v] if self.services else []
        external = "、".join(configured) if configured else "未配置（只用本地黑名单）"
        return (
            f"黑名单 {len(self.data.blacklist)} 条 · 短链域名 {len(self.data.short_links)} 个 · "
            f"品牌 {len(self.data.brands)} 个 · 外部情报：{external}"
        )

    async def selfcheck(self, services: Services) -> list[SelfCheckItem]:
        if self.data is None or self._ages is None:
            await self.startup(services)
        assert self.data is not None and self._ages is not None
        items = [
            SelfCheckItem(
                "链接体检数据",
                not self.data.invalid_entries,
                f"黑名单 {len(self.data.blacklist)} 条，RDAP 服务表 {len(self.data.rdap_services)} 个后缀"
                f"（IANA 发布于 {self.data.rdap_publication or '未知'}）",
                None if not self.data.invalid_entries else f"data/link 里这些条目写错了：{', '.join(self.data.invalid_entries)}",
            )
        ]
        t0 = time.monotonic()
        try:
            created = await asyncio.wait_for(self._ages.rdap("example.com", services.settings.link_rdap_timeout_s), 5)
            ok = created is not None
            detail = f"RDAP 可用（{int((time.monotonic() - t0) * 1000)} ms）" if ok else "RDAP 返回里没有注册时间"
        except Exception as exc:  # 自检只报告，不抛
            ok, detail = False, f"RDAP 连不上（{type(exc).__name__}）"
        items.append(
            SelfCheckItem(
                "域名注册时间查询",
                ok,
                detail,
                None if ok else "检查网络。连不上时链接只用词法特征和本地黑名单，不影响出报告。",
            )
        )
        configured = intel_mod.configured_external(services.settings)
        for provider in self._external:
            try:
                await provider.query("example.com", services.settings.intel_timeout_s)
                items.append(SelfCheckItem(f"外部情报 {provider.name}", True, "可以调通"))
            except intel_mod.IntelError as exc:
                items.append(
                    SelfCheckItem(
                        f"外部情报 {provider.name}", False, f"调用失败（{exc}）", "检查密钥和网络；失败时报告会带 intel_unavailable。"
                    )
                )
        for name in intel_mod.NOT_IMPLEMENTED:
            if configured[name]:
                items.append(
                    SelfCheckItem(f"外部情报 {name}", False, "已配置密钥，但本版本还没有接入", "这一项不会被调用，可以先删掉密钥。")
                )
        return items

    # ---- 分析 ----

    def should_run(self, inp: AnalysisInput) -> bool:
        return has_links(inp)

    async def run(self, inp: AnalysisInput, ctx: AnalyzerContext) -> AnalyzerOutput:
        if self.data is None or self._ages is None or self._fetch is None:
            await self.startup(ctx.services)
        assert self.data is not None
        settings = ctx.settings
        loop = asyncio.get_running_loop()
        remaining = ctx.remaining_s()
        budget = settings.timeout_link_s if remaining is None else remaining
        deadline = loop.time() + max(0.0, budget - ASSEMBLY_MARGIN_S)

        candidates, total = collect_links(inp)
        states = [self._initial_state(c) for c in candidates]
        tasks = [
            asyncio.create_task(self._analyze(state, ctx, deadline), name=f"link-{i}")
            for i, state in enumerate(states)
            if state.cand.parsed is not None
        ]
        timed_out = False
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=max(0.0, deadline - loop.time()))
            if pending:
                timed_out = True
                for task in pending:
                    task.cancel()
                await asyncio.wait(pending, timeout=0.05)

        links: list[LinkResult] = []
        details: list[dict] = []
        intel_unavailable = False
        for state in states:
            if timed_out:
                self._mark_unqueried_intel(state, settings.link_expand_short_links)
            result, why = self._result(state)
            links.append(result)
            if state.intel_failed or state.intel_pending:
                intel_unavailable = True
            details.append(
                {
                    "risk": result.risk.value,
                    "risk_why": why,
                    "from_text": state.cand.from_text,
                    "requests": state.trace.requests,
                    "redirects": state.trace.redirects,
                    "expand_stopped": state.trace.stopped,
                    "age_source": state.age.source if state.age else None,
                    "intel": dict(state.verdicts),
                    "intel_failed": state.intel_failed + sorted(state.intel_pending),
                    "steps": state.steps,
                }
            )
        ctx.logger.info("link.done links=%d found=%d timed_out=%s", len(links), total, timed_out)
        return AnalyzerOutput(
            status=AnalyzerStatus.DONE,
            links=links,
            degraded=[DegradedFlag.INTEL_UNAVAILABLE] if intel_unavailable else [],
            extras={
                "found": total,
                "truncated": total > len(links),
                "timed_out": timed_out,
                "external_intel": [p.name for p in self._external],
                "details": details,
            },
            note=f"links={len(links)} found={total} timed_out={timed_out}",
        )

    def _initial_state(self, cand: Candidate) -> _LinkState:
        assert self.data is not None
        state = _LinkState(cand)
        if cand.parsed is not None:
            state.reasons = lexical_reasons(cand.parsed, self.data)
            state.verdicts["local"] = intel_mod.MALICIOUS if LinkReason.BLACKLIST_HIT in state.reasons else intel_mod.UNKNOWN
            state.steps.append("lexical")
        return state

    async def _analyze(self, state: _LinkState, ctx: AnalyzerContext, deadline: float) -> None:
        """联网步骤的意外错误只记在 steps 里：词法结果已经在 state 里，照样出报告。"""
        try:
            await self._analyze_network(state, ctx, deadline)
        except Exception as exc:
            state.steps.append(f"error:{type(exc).__name__}")

    async def _analyze_network(self, state: _LinkState, ctx: AnalyzerContext, deadline: float) -> None:
        assert self.data is not None and self._ages is not None and self._fetch is not None
        settings = ctx.settings
        start = state.cand.parsed
        assert start is not None
        if self.offline:
            state.steps.append("offline")
            return
        if start.reserved:
            state.steps.append("reserved_no_network")
            return
        if settings.link_expand_short_links and LinkReason.SHORT_LINK in state.reasons:
            await expand(
                start,
                state.trace,
                transport=self._fetch,
                resolver=self.resolver,
                max_hops=settings.link_max_hops,
                hop_timeout=settings.link_hop_timeout_s,
                deadline=deadline,
                stop_before=self._stop_before,
            )
            state.steps.append("expand")
            self._apply_final(state)
        target = state.target
        if target is None or target.reserved or target.registrable_domain is None:
            return
        if host_in(target.host, self.data.short_links):
            return  # 没展开成功，还停在短链服务本身：查它的注册时间和情报没有意义
        domain = target.registrable_domain
        await asyncio.gather(
            self._lookup_age(state, domain, ctx.db, deadline),
            self._query_intel(state, domain, settings.intel_timeout_s, deadline),
        )

    def _stop_before(self, parsed: ParsedUrl) -> str | None:
        assert self.data is not None
        if host_in(parsed.host, self.data.blacklist):
            return "blacklisted"
        if parsed.reserved:
            return "reserved"
        return None

    def _apply_final(self, state: _LinkState) -> None:
        """把展开结果并进特征。幂等：到点被取消时，组装结果前会用已经走过的几跳再调一次。"""
        assert self.data is not None
        if not state.trace.urls:
            return
        final_url = state.trace.final_url
        if final_url is not None:
            final = parse_url(final_url)
            if final is not None:
                state.final = final
                state.reasons |= lexical_reasons(final, self.data)
        if state.trace.redirects >= REDIRECT_CHAIN_MIN:
            state.reasons.add(LinkReason.REDIRECT_CHAIN)
        if any(host_in(h, self.data.blacklist) for h in state.hosts()):
            state.reasons.add(LinkReason.BLACKLIST_HIT)
            state.verdicts["local"] = intel_mod.MALICIOUS

    async def _lookup_age(self, state: _LinkState, domain: str, db: Database, deadline: float) -> None:
        assert self._ages is not None
        budget = deadline - asyncio.get_running_loop().time()
        if budget <= 0.05:
            return
        try:
            age = await asyncio.wait_for(self._ages.lookup(domain, db=db, budget=budget), timeout=budget)
        except asyncio.TimeoutError:
            return
        state.steps.append("age")
        if age is not None:
            state.age = age
            if age.age_days() < DOMAIN_AGE_NEW_DAYS:
                state.reasons.add(LinkReason.DOMAIN_AGE_LT_30D)

    async def _query_intel(self, state: _LinkState, domain: str, timeout_s: float, deadline: float) -> None:
        if not self._external:
            return

        async def one(provider: intel_mod.DomainIntelProvider) -> None:
            state.intel_pending.add(provider.name)
            budget = min(timeout_s, deadline - asyncio.get_running_loop().time())
            try:
                if budget <= 0.05:
                    raise intel_mod.IntelError("no_time")
                verdict = await asyncio.wait_for(provider.query(domain, budget), timeout=budget)
            except (intel_mod.IntelError, asyncio.TimeoutError):
                state.intel_failed.append(provider.name)
                return
            except asyncio.CancelledError:
                state.intel_failed.append(provider.name)  # 整路到点：已配置的情报没查完也算不可用
                raise
            except Exception:
                state.intel_failed.append(provider.name)
                return
            finally:
                state.intel_pending.discard(provider.name)
            state.verdicts[provider.name] = verdict
            if verdict == intel_mod.MALICIOUS:
                state.reasons.add(LinkReason.BLACKLIST_HIT)

        await asyncio.gather(*(one(p) for p in self._external))
        state.steps.append("intel")

    def _mark_unqueried_intel(self, state: _LinkState, expand_enabled: bool) -> None:
        """到点时还没轮到查外部情报的链接（比如卡在短链展开上）：已配置的情报算不可用。"""
        assert self.data is not None
        start = state.cand.parsed
        if not self._external or self.offline or start is None or start.reserved or "intel" in state.steps:
            return
        self._apply_final(state)
        target = state.target
        still_expanding = expand_enabled and LinkReason.SHORT_LINK in state.reasons and "expand" not in state.steps
        queryable = (
            target is not None
            and not target.reserved
            and target.registrable_domain is not None
            and not host_in(target.host, self.data.short_links)
        )
        if not (still_expanding or queryable):
            return
        done = set(state.verdicts) | set(state.intel_failed) | state.intel_pending
        state.intel_failed.extend(p.name for p in self._external if p.name not in done)

    def _result(self, state: _LinkState) -> tuple[LinkResult, str]:
        cand = state.cand
        if cand.parsed is None:
            risk, why = assess_risk(set(), analyzed=False)
            return LinkResult(url=cand.url, origin=cand.origin, risk=risk), why
        self._apply_final(state)
        reasons = set(state.reasons)
        risk, why = assess_risk(reasons)
        target = state.target
        assert target is not None
        malicious = [p for p, v in state.verdicts.items() if v == intel_mod.MALICIOUS]
        answered = [p for p in state.verdicts if p != "local"]
        provider = malicious[0] if malicious else (answered[0] if answered else "local")
        return (
            LinkResult(
                url=cand.url,
                final_url=state.trace.final_url,
                registrable_domain=target.registrable_domain,
                origin=cand.origin,
                risk=risk,
                reasons=sort_reasons(reasons),
                domain_age_days=state.age.age_days() if state.age else None,
                intel=LinkIntel(provider=provider, verdict=state.verdicts.get(provider, intel_mod.UNKNOWN)),
            ),
            why,
        )


def create() -> LinkAnalyzer:
    return LinkAnalyzer()
