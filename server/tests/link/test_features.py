"""词法特征：每种 reasons 取值各有命中和不命中的样例；reasons → risk 映射。"""

from __future__ import annotations

import pytest

from app.analyzers.link.data import LinkData
from app.analyzers.link.extract import parse_url
from app.analyzers.link.features import STRONG_COMBOS, assess_risk, brand_lookalike, lexical_reasons
from app.contract import LinkReason as R
from app.contract import LinkRisk


@pytest.fixture(scope="module")
def data() -> LinkData:
    return LinkData.load()


def reasons_of(url: str, data: LinkData) -> set[R]:
    parsed = parse_url(url)
    assert parsed is not None, url
    return lexical_reasons(parsed, data)


# reason, 命中样例, 不命中样例
LEXICAL_CASES = [
    (R.BLACKLIST_HIT, ["http://scam-demo.example/", "https://dl.scam-demo.example/x", "http://FAKE-BANK-DEMO.test"],
     ["http://notscam-demo.example/", "https://www.icbc.com.cn/"]),
    (R.IP_HOST, ["http://1.2.3.4/", "http://2130706433/", "http://[2001:db8::1]/"],
     ["https://a1.cc/", "https://1.2.3.4.example.cn/"]),
    (R.NONSTANDARD_PORT, ["http://a.cc:8080/", "https://a.cc:8443/", "a.cc:3000/x"],
     ["http://a.cc:80/", "https://a.cc:443/", "https://a.cc/"]),
    (R.PUNYCODE, ["https://xn--pple-43d.com/", "https://аpple.com/", "http://中文.cc/"],
     ["https://apple.com/", "https://xn.cc/"]),
    (R.SUSPICIOUS_TLD, ["https://a.top/", "https://b.xyz/", "https://c.vip/x"],
     ["https://a.com/", "https://b.cn/", "https://top.com/"]),
    (R.SHORT_LINK, ["https://t.cn/abc", "https://bit.ly/x", "https://www.dwz.cn/a"],
     ["https://nott.cn/abc", "https://t.cn.evil.com/"]),
    (R.BRAND_LOOKALIKE, ["http://icbc.com.cn.evil.top/", "https://alipay-verify.cc/", "https://aliapy.com/",
                         "https://аpple.com/", "https://app1e.top/", "https://jd-pay.cc/"],
     ["https://www.icbc.com.cn/", "https://icbc.com/", "https://small.com/", "https://jdd.com/",
      "https://www.alipay.com/", "https://applesauce-recipes.com/"]),
    (R.APK_DIRECT_LINK, ["https://a.cc/app.apk", "https://a.cc/dl/APP.APK", "https://a.cc/x.apk/"],
     ["https://a.cc/app.apk.html", "https://a.cc/?f=app.apk", "https://apk.cc/"]),
]


@pytest.mark.parametrize("reason, hits, misses", LEXICAL_CASES, ids=[c[0].value for c in LEXICAL_CASES])
def test_lexical_reason(reason, hits, misses, data):
    for url in hits:
        assert reason in reasons_of(url, data), url
    for url in misses:
        assert reason not in reasons_of(url, data), url


def test_every_lexical_reason_covered():
    network_only = {R.DOMAIN_AGE_LT_30D, R.REDIRECT_CHAIN}
    assert {c[0] for c in LEXICAL_CASES} | network_only == set(R)


def test_brand_lookalike_names_the_brand(data):
    assert brand_lookalike(parse_url("http://icbc.com.cn.evil.top/"), data) == "icbc"
    assert brand_lookalike(parse_url("https://aliapy.com/"), data) == "alipay"
    assert brand_lookalike(parse_url("http://1.2.3.4/"), data) is None


def test_ordinary_link_has_no_reasons(data):
    assert reasons_of("https://www.baidu.com/s?wd=天气", data) == set()


# ---- 风险映射 ----


@pytest.mark.parametrize("weak", [R.SHORT_LINK, R.SUSPICIOUS_TLD, R.PUNYCODE, R.IP_HOST, R.NONSTANDARD_PORT,
                                  R.REDIRECT_CHAIN, R.DOMAIN_AGE_LT_30D, R.BRAND_LOOKALIKE, R.APK_DIRECT_LINK])
def test_single_weak_signal_is_never_high(weak):
    assert assess_risk({weak})[0] == LinkRisk.MEDIUM


def test_common_weak_pairs_are_not_high():
    for pair in ({R.SHORT_LINK, R.SUSPICIOUS_TLD}, {R.SHORT_LINK, R.REDIRECT_CHAIN},
                 {R.IP_HOST, R.NONSTANDARD_PORT}, {R.SUSPICIOUS_TLD, R.DOMAIN_AGE_LT_30D},
                 {R.SHORT_LINK, R.APK_DIRECT_LINK}):
        assert assess_risk(pair)[0] == LinkRisk.MEDIUM, pair


def test_blacklist_alone_is_high():
    assert assess_risk({R.BLACKLIST_HIT})[0] == LinkRisk.HIGH


@pytest.mark.parametrize("combo", [c for c, _ in STRONG_COMBOS], ids=lambda c: "+".join(sorted(r.value for r in c)))
def test_strong_combos_are_high(combo):
    risk, why = assess_risk(set(combo))
    assert risk == LinkRisk.HIGH and why


def test_low_and_unknown():
    assert assess_risk(set())[0] == LinkRisk.LOW
    assert assess_risk(set(), analyzed=False)[0] == LinkRisk.UNKNOWN


def test_data_files_load_cleanly(data):
    assert data.invalid_entries == ()
    assert "scam-demo.example" in data.blacklist
    assert "com" in data.rdap_services and "cn" not in data.rdap_services
    # 黑名单只能放保留的示例域名
    for host in data.blacklist:
        assert host.rsplit(".", 1)[-1] in ("example", "test", "invalid"), host
