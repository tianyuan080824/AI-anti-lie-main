"""链接体检的命令行入口（在 `server/` 目录下执行）：

    uv run python -m app.analyzers.link check https://t.example.cn/a1
    uv run python -m app.analyzers.link check "加我 abc.cc/x 领红包"     # 也可以给一段文字，提取其中的链接
    uv run python -m app.analyzers.link check --offline http://1.2.3.4:8080/a.apk
    uv run python -m app.analyzers.link selfcheck

走的是和服务里完全一样的分析流程（同一个 create()、同一份 data/link 数据、同一个数据库缓存）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid

from app.analyzers.base import AnalysisInput, AnalyzerContext, InputLine, UrlInput
from app.analyzers.link import LinkAnalyzer, create
from app.config import get_settings
from app.contract import Speaker
from app.services import open_services

RISK_TEXT = {
    "high": "高风险",
    "medium": "有风险特征",
    "low": "没发现风险特征（不代表安全）",
    "unknown": "没能分析",
}
REASON_TEXT = {
    "blacklist_hit": "命中黑名单或威胁情报",
    "domain_age_lt_30d": "域名注册不到 30 天",
    "brand_lookalike": "域名仿冒银行 / 支付 / 电商品牌",
    "suspicious_tld": "冷门顶级域",
    "ip_host": "用 IP 地址直连",
    "nonstandard_port": "非标准端口",
    "punycode": "国际化域名（可能用形近字仿冒）",
    "short_link": "公开短链服务",
    "redirect_chain": "多次跳转",
    "apk_direct_link": "直接下载安装包",
}


def _force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _build_input(targets: list[str]) -> AnalysisInput:
    urls: list[UrlInput] = []
    lines: list[InputLine] = []
    for i, target in enumerate(targets):
        if any(ch.isspace() for ch in target.strip()) or not target.strip():
            lines.append(InputLine(line_id=f"0-{len(lines)}", speaker=Speaker.UNKNOWN, text=target, item_index=0))
        else:
            urls.append(UrlInput(url=target, origin="typed", item_index=i + 1))
    check_id = f"cli_{uuid.uuid4().hex[:8]}"
    return AnalysisInput(check_id=check_id, trace_id=check_id, lines=tuple(lines), urls=tuple(urls))


async def _check(targets: list[str], offline: bool, as_json: bool) -> int:
    settings = get_settings()
    analyzer = LinkAnalyzer(offline=True) if offline else create()
    async with open_services(settings) as services:
        await analyzer.startup(services)
        try:
            inp = _build_input(targets)
            if not analyzer.should_run(inp):
                print("没有找到链接。")
                return 1
            loop = asyncio.get_running_loop()
            ctx = AnalyzerContext(
                services=services,
                check_id=inp.check_id,
                trace_id=inp.trace_id,
                deadline=loop.time() + settings.timeout_link_s,
            )
            t0 = loop.time()
            out = await analyzer.run(inp, ctx)
            elapsed = int((loop.time() - t0) * 1000)
        finally:
            await analyzer.shutdown()

    if as_json:
        print(json.dumps(out.model_dump(mode="json"), ensure_ascii=False, indent=2))
        return 0

    details = out.extras.get("details", [])
    for i, link in enumerate(out.links):
        d = details[i] if i < len(details) else {}
        print(f"链接：{link.url}")
        label, why = RISK_TEXT[link.risk.value], d.get("risk_why", "")
        print(f"  风险：{link.risk.value}（{label}）" + (f"— {why}" if why and why != label else ""))
        if link.reasons:
            for reason in link.reasons:
                print(f"  · {reason.value}：{REASON_TEXT[reason.value]}")
        else:
            print("  · 没有命中任何风险特征")
        if link.final_url:
            print(f"  最终地址：{link.final_url}（跳转 {d.get('redirects', 0)} 次）")
        if d.get("expand_stopped") and d.get("expand_stopped") != "end":
            print(f"  短链展开停止原因：{d['expand_stopped']}")
        print(f"  可注册域名：{link.registrable_domain or '—'}")
        age = f"{link.domain_age_days} 天（来源 {d.get('age_source')}）" if link.domain_age_days is not None else "查不到"
        print(f"  域名注册时长：{age}")
        if link.intel:
            print(f"  情报：{link.intel.provider} → {link.intel.verdict}")
        if d.get("intel_failed"):
            print(f"  外部情报调用失败：{', '.join(d['intel_failed'])}")
        if link.origin:
            print(f"  来源：{link.origin}")
        print()
    flags = ", ".join(f.value for f in out.degraded) or "无"
    extra = "；结果已截断" if out.extras.get("truncated") else ""
    print(f"状态 {out.status.value} · 降级标记 {flags} · 耗时 {elapsed} ms{'（到点返回了部分结果）' if out.extras.get('timed_out') else ''}{extra}")
    return 0


async def _selfcheck() -> int:
    analyzer = create()
    async with open_services(get_settings()) as services:
        try:
            items = await analyzer.selfcheck(services)
        finally:
            await analyzer.shutdown()
    ok = True
    for item in items:
        ok &= item.ok
        print(f"[{'通过' if item.ok else '失败'}] {item.name}：{item.detail}")
        if not item.ok and item.hint:
            print(f"       怎么办：{item.hint}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    parser = argparse.ArgumentParser(prog="python -m app.analyzers.link")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="体检一条或几条链接（也可以给一段文字）")
    check.add_argument("targets", nargs="+", help="链接，或者包含链接的一段文字（带空格时请加引号）")
    check.add_argument("--offline", action="store_true", help="不联网：只看词法特征和本地黑名单")
    check.add_argument("--json", action="store_true", help="输出原始 JSON")
    sub.add_parser("selfcheck", help="检查数据文件和 RDAP 能否调通（会联网）")
    args = parser.parse_args(argv)
    if args.command == "check":
        return asyncio.run(_check(args.targets, args.offline, args.json))
    if args.command == "selfcheck":
        return asyncio.run(_selfcheck())
    return 2


if __name__ == "__main__":
    sys.exit(main())
