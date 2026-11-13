"""链接提取：带 / 不带协议头、占位符和普通数字不误判、去重和 origin。"""

from __future__ import annotations

import pytest

from app.analyzers.link.extract import collect_links, find_links, has_links, parse_url
from app.contract import Speaker
from tests.link.conftest import make_input


@pytest.mark.parametrize(
    "text, expected",
    [
        ("点这个 https://t.example.cn/a1 领取", ["https://t.example.cn/a1"]),
        ("点https://t.example.cn/a1。然后", ["https://t.example.cn/a1"]),
        ("HTTP://Foo.CC/x?a=1&b=2", ["HTTP://Foo.CC/x?a=1&b=2"]),
        ("加我 abc.cc/x 领红包", ["abc.cc/x"]),
        ("网址：icbc.com.cn", ["icbc.com.cn"]),
        ("www.baidu.com，好的", ["www.baidu.com"]),
        ("去 dl.scam-demo.example/app.apk 下载", ["dl.scam-demo.example/app.apk"]),
        ("(见 t.cn/AbC)", ["t.cn/AbC"]),
        ("后台 http://127.0.0.1:8080/x 看看", ["http://127.0.0.1:8080/x"]),
        ("访问 1.2.3.4/admin", ["1.2.3.4/admin"]),
        ("访问 1.2.3.4:8080", ["1.2.3.4:8080"]),
        ("xn--pple-43d.com/login", ["xn--pple-43d.com/login"]),
        ("https://аpple.com/x 登录", ["https://аpple.com/x"]),
        ("两个 a.cc/1 和 https://b.top/2", ["a.cc/1", "https://b.top/2"]),
    ],
)
def test_finds_links(text, expected):
    assert find_links(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "<PHONE> <CARD> <ID>",
        "先垫付<AMOUNT:200>返<AMOUNT:260>",
        "一单3.5元，返佣3.50元",
        "升级到v1.2版本，或者 1.2.3",
        "下午3.30见，19.99包邮",
        "版本号 1.2.3.4",
        "看看README.md和data.zip",
        "Mr.Wang 你好，Dr.Li 在吗",
        "邮箱 a@b.com 联系",
        "mailto:a@b.com javascript:alert(1)",
        "今天天气不错。明天见",
        "",
    ],
)
def test_ignores_non_links(text):
    assert find_links(text) == []


def test_parse_url_normalizes():
    p = parse_url("https://user:pw@Evil.CC:8080/p?q=1#frag")
    assert p is not None
    assert (p.host, p.port, p.userinfo, p.registrable_domain) == ("evil.cc", 8080, "user:pw", "evil.cc")
    assert p.to_url() == "https://evil.cc:8080/p?q=1"
    assert parse_url("abc.cc/x").to_url() == "http://abc.cc/x"
    assert parse_url("ftp://a.cc/x") is None
    assert parse_url("weixin://dl/business") is None


@pytest.mark.parametrize(
    "url",
    ["http://2130706433/", "http://0x7f000001/", "http://0177.0.0.1/", "http://127.1/", "http://0x7f.0.0.1/"],
)
def test_numeric_ip_forms_are_parsed(url):
    p = parse_url(url)
    assert p is not None and str(p.ip) == "127.0.0.1"


def test_invalid_numeric_host_rejected():
    assert parse_url("http://999.1.1.1/") is None
    assert parse_url("http://1.2.3.4.5/") is None


def test_dedupe_keeps_url_item_origin():
    inp = make_input(
        "链接 https://a.cc/x/ 再发一次 a.cc/x",
        "另一个 b.top/y",
        urls=("https://a.cc/x",),
        origin="qrcode",
    )
    cands, total = collect_links(inp)
    assert total == 2
    assert [(c.url, c.origin, c.from_text) for c in cands] == [
        ("https://a.cc/x", "qrcode", False),
        ("b.top/y", None, True),
    ]


def test_all_speakers_are_scanned():
    inp = make_input(
        "对方 a.cc/1", "我 b.cc/2", "系统 c.cc/3", "未知 d.cc/4",
        speakers=[Speaker.OTHER, Speaker.SELF, Speaker.SYSTEM, Speaker.UNKNOWN],
    )
    cands, _ = collect_links(inp)
    assert [c.url for c in cands] == ["a.cc/1", "b.cc/2", "c.cc/3", "d.cc/4"]


def test_unparseable_url_item_kept_as_unknown_candidate():
    inp = make_input(urls=("weixin://dl/x",))
    cands, _ = collect_links(inp)
    assert len(cands) == 1 and cands[0].parsed is None


def test_limit():
    inp = make_input(" ".join(f"s{i}.cc/x" for i in range(15)))
    cands, total = collect_links(inp, limit=10)
    assert len(cands) == 10 and total == 15


def test_has_links():
    assert has_links(make_input("加我 abc.cc/x"))
    assert has_links(make_input(urls=("https://a.cc",)))
    assert not has_links(make_input("一单3.5元 <PHONE>"))
    assert not has_links(make_input())
