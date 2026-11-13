"""词法特征（不联网）和风险等级映射。

`assess_risk()` 是 reasons → risk 的唯一出处。契约第 6 节允许一条 `high` 链接单独把整份报告判成
`high_risk`，所以 `high` 只给强信号：黑名单（含外部情报判恶意）命中，或者下面 `STRONG_COMBOS`
里能说清理由的组合。任何单个弱特征（只是短链、只是冷门后缀……）最多是 `medium`。
"""

from __future__ import annotations

from rapidfuzz.distance import OSA

from app.analyzers.link.data import LinkData, host_in
from app.analyzers.link.extract import ParsedUrl
from app.contract import LinkReason, LinkRisk

R = LinkReason

#: 能单独判 high 的组合，以及理由（进 extras，方便人审）。
STRONG_COMBOS: tuple[tuple[frozenset[LinkReason], str], ...] = (
    (frozenset({R.BRAND_LOOKALIKE, R.DOMAIN_AGE_LT_30D}), "注册不到 30 天的域名仿冒品牌"),
    (frozenset({R.BRAND_LOOKALIKE, R.PUNYCODE}), "用形近字（国际化域名）仿冒品牌"),
    (frozenset({R.BRAND_LOOKALIKE, R.APK_DIRECT_LINK}), "仿冒品牌的域名直接分发安装包"),
    (frozenset({R.APK_DIRECT_LINK, R.DOMAIN_AGE_LT_30D}), "注册不到 30 天的域名直接分发安装包"),
    (frozenset({R.APK_DIRECT_LINK, R.IP_HOST}), "用 IP 直连地址分发安装包"),
)

#: 契约 reasons 的固定顺序（输出稳定，测试和人审都好对照）。
REASON_ORDER: tuple[LinkReason, ...] = tuple(LinkReason)


def assess_risk(reasons: set[LinkReason], analyzed: bool = True) -> tuple[LinkRisk, str]:
    """reasons → (risk, 理由)。`analyzed=False` 表示连链接本身都没能解析。"""
    if R.BLACKLIST_HIT in reasons:
        return LinkRisk.HIGH, "命中黑名单或威胁情报"
    for combo, why in STRONG_COMBOS:
        if combo <= reasons:
            return LinkRisk.HIGH, why
    if reasons:
        return LinkRisk.MEDIUM, "有风险特征，但不足以单独判为高风险"
    if not analyzed:
        return LinkRisk.UNKNOWN, "没能分析这条链接"
    return LinkRisk.LOW, "没发现风险特征（不代表安全）"


def sort_reasons(reasons: set[LinkReason]) -> list[LinkReason]:
    return [r for r in REASON_ORDER if r in reasons]


# ---------------------------------------------------------------------------
# 品牌仿冒
# ---------------------------------------------------------------------------

#: 形近字 → 拉丁字母（只收常见的西里尔、希腊字母和数字替换）。
_CONFUSABLES = str.maketrans(
    {
        "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "і": "i", "ј": "j",
        "ԁ": "d", "ӏ": "l", "ѕ": "s", "һ": "h", "ԛ": "q", "ԝ": "w", "к": "k", "м": "m", "т": "t",
        "ο": "o", "α": "a", "ν": "v", "ι": "i", "κ": "k", "ρ": "p", "υ": "u", "ε": "e",
        "0": "o", "1": "l",
    }
)


def _unicode_label(label: str) -> str:
    if label.startswith("xn--"):
        try:
            return label.encode("ascii").decode("idna")
        except UnicodeError:
            return label
    return label


def skeleton(label: str) -> str:
    return _unicode_label(label).lower().translate(_CONFUSABLES).replace("rn", "m").replace("vv", "w")


def brand_lookalike(parsed: ParsedUrl, data: LinkData) -> str | None:
    """返回被仿冒的品牌名；不像任何品牌、或者本身就是官方域名时返回 None。

    - 品牌名作为一个完整的词出现在主机里（按点和连字符切分）：`icbc.com.cn.evil.top`、`alipay-verify.cc`；
    - 形近字或数字替换后和品牌名相同：`аpple.com`（西里尔 а）、`app1e.top`；
    - 6 个字符以上的品牌，可注册部分和品牌名的编辑距离不超过 1（9 个字符以上不超过 2）：`aliapy.com`。
      短品牌不做模糊匹配，否则 `small` 会被当成 `tmall`。
    """
    if parsed.ip is not None or parsed.registrable_domain is None:
        return None
    if parsed.registrable_domain in data.official_domains:
        return None
    labels = [_unicode_label(l) for l in parsed.subdomain.split(".") if l]
    reg = _unicode_label(parsed.domain)
    all_tokens = {skeleton(t) for label in labels + [reg] for t in label.split("-") if t}
    reg_tokens = {skeleton(t) for t in reg.split("-") if t}
    reg_joined = skeleton(reg.replace("-", ""))
    for brand in data.brands:
        if len(brand) <= 3:
            if brand in reg_tokens or brand == reg_joined:
                return brand
            continue
        if brand in all_tokens or brand == reg_joined:
            return brand
        if len(brand) >= 6:
            limit = 2 if len(brand) >= 9 else 1
            if any(OSA.distance(c, brand) <= limit for c in reg_tokens | {reg_joined} if len(c) >= 4):
                return brand
    return None


# ---------------------------------------------------------------------------
# 词法特征
# ---------------------------------------------------------------------------


def is_punycode(parsed: ParsedUrl) -> bool:
    return parsed.ip is None and any(label.startswith("xn--") for label in parsed.host.split("."))


def is_apk(parsed: ParsedUrl) -> bool:
    return parsed.path.lower().rstrip("/").endswith(".apk")


def is_blacklisted(parsed: ParsedUrl, data: LinkData) -> bool:
    return host_in(parsed.host, data.blacklist)


def lexical_reasons(parsed: ParsedUrl, data: LinkData) -> set[LinkReason]:
    """一个地址自身的词法特征（含本地黑名单）。不联网。"""
    reasons: set[LinkReason] = set()
    if is_blacklisted(parsed, data):
        reasons.add(R.BLACKLIST_HIT)
    if parsed.ip is not None:
        reasons.add(R.IP_HOST)
    if parsed.effective_port not in (80, 443):
        reasons.add(R.NONSTANDARD_PORT)
    if is_punycode(parsed):
        reasons.add(R.PUNYCODE)
    if parsed.ip is None and parsed.host.rsplit(".", 1)[-1] in data.suspicious_tlds:
        reasons.add(R.SUSPICIOUS_TLD)
    if host_in(parsed.host, data.short_links):
        reasons.add(R.SHORT_LINK)
    if brand_lookalike(parsed, data) is not None:
        reasons.add(R.BRAND_LOOKALIKE)
    if is_apk(parsed):
        reasons.add(R.APK_DIRECT_LINK)
    return reasons
