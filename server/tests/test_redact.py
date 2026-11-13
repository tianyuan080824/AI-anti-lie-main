"""服务端二次脱敏（契约第 2 节）。"""

from __future__ import annotations

import pytest

from app.redact import IMPLEMENTED, id_check_ok, luhn_ok, redact_text

VALID_ID = "11010519491231002X"
INVALID_ID = "110105194912310021"
VALID_CARD = "6222021234567890128"
INVALID_CARD = "6222021234567890123"


def test_implemented_flag():
    assert IMPLEMENTED is True


def test_check_digit_helpers():
    assert id_check_ok(VALID_ID) and id_check_ok(VALID_ID.lower())
    assert not id_check_ok(INVALID_ID)
    assert luhn_ok(VALID_CARD) and luhn_ok("4111111111111111")
    assert not luhn_ok(INVALID_CARD)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("我电话13800138000", "我电话<PHONE>"),
        ("打19912345678找我", "打<PHONE>找我"),
        (f"身份证{VALID_ID}", "身份证<ID>"),
        (f"卡号{VALID_CARD}", "卡号<CARD>"),
        ("卡号4111111111111111", "卡号<CARD>"),
        ("先垫付200元返260块", "先垫付<AMOUNT:200>返<AMOUNT:260>"),
        ("转￥1,500.50给我", "转<AMOUNT:1500.5>给我"),
        ("¥200", "<AMOUNT:200>"),
        ("$30", "<AMOUNT:30>"),
        ("RMB 99", "<AMOUNT:99>"),
        ("投3万就能赚", "投<AMOUNT:30000>就能赚"),
        ("0.5万元", "<AMOUNT:5000>"),
        ("1.5亿", "<AMOUNT:150000000>"),
        ("2块钱", "<AMOUNT:2>"),
        ("3千元", "<AMOUNT:3000>"),
    ],
)
def test_each_number_type(text, expected):
    assert redact_text(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        f"身份证{INVALID_ID}",  # 校验位不通过
        f"卡号{INVALID_CARD}",  # Luhn 不通过
        "订单号12345678901234",  # 14 位普通长数字
        "单号123456789012",
        "电话23800138000",  # 不是 1 开头
        "号码1380013800",  # 只有 10 位
        "等了3天",
        "排名第3",
        "3千个人",  # 千 后面没有货币单位，不算金额
    ],
)
def test_ordinary_numbers_untouched(text):
    assert redact_text(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "链接 https://a.cn/p?id=13800138000&amt=200元",
        f"http://x.cn/u/{VALID_ID}",
        f"https://pay.example.cc/{VALID_CARD}",
        "www.x.com/13800138000",
        "example.cn/1380013800012",
        "dl.example-app.cc:8080/a?u=13800138000",
    ],
)
def test_digits_inside_urls_untouched(text):
    assert redact_text(text) == text


def test_url_and_number_on_same_line():
    assert redact_text("看 www.x.com/13800138000 加 13900139000") == "看 www.x.com/13800138000 加 <PHONE>"


def test_order_id_before_card_before_phone():
    # 18 位且校验位通过的数字按身份证处理，不会再被当成卡号或手机号
    assert redact_text(VALID_ID) == "<ID>"
    # 手机号紧跟金额单位：先替换手机号，金额规则看不到这串数字
    assert redact_text("13800138000元") == "<PHONE>元"


@pytest.mark.parametrize(
    "text",
    [
        "已经<PHONE>和<AMOUNT:200>",
        "<CARD><ID><AMOUNT:1500.5>",
        "<AMOUNT:30000>元",
    ],
)
def test_placeholders_never_rewritten(text):
    assert redact_text(text) == text


@pytest.mark.parametrize(
    "text",
    [
        f"我电话13800138000，卡号{VALID_CARD}，身份证{VALID_ID}，先垫付200元返260块",
        "转￥1,500.50给我，链接 https://a.cn/p?id=13800138000",
        "投3万就能赚，0.5万元起步",
        "",
        "在吗",
    ],
)
def test_idempotent(text):
    once = redact_text(text)
    assert redact_text(once) == once


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("我电话１３８００１３８０００", "我电话<PHONE>"),
        ("¥１２０", "<AMOUNT:120>"),
        ("１２．５元", "<AMOUNT:12.5>"),
        ("12．5元", "<AMOUNT:12.5>"),
        ("身份证１１０１０５１９４９１２３１００２Ｘ。", "身份证<ID>。"),
        ("+8613800138000", "<PHONE>"),
        ("＋86 13800138000", "<PHONE>"),
        ("008613800138000", "<PHONE>"),
        ("+0086 13800138000", "<PHONE>"),
        ("＋８６－１３８００１３８０００", "<PHONE>"),
        ("12,345,678元", "<AMOUNT:12345678>"),
        ("订单186 13800138000", "订单186 <PHONE>"),
        ("我86年生的13800138000", "我86年生的<PHONE>"),  # 不带 + 或 00 的 86 不是国家码
    ],
)
def test_fullwidth_and_country_code(text, expected):
    assert redact_text(text) == expected
    assert redact_text(expected) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # 全角逗号不当分组：两个数不能合成一个更大的金额
        ("分别转了3000，5000元", "分别转了3000，<AMOUNT:5000>"),
        ("每单20，3000元封顶", "每单20，<AMOUNT:3000>封顶"),
        ("验证码1234，100元", "验证码1234，<AMOUNT:100>"),
        ("尾号1234,200元已到账", "尾号1234,<AMOUNT:200>已到账"),
        ("第1，2名", "第1，2名"),
        ("Ｘ光片", "Ｘ光片"),
    ],
)
def test_lists_are_not_merged_into_one_amount(text, expected):
    assert redact_text(text) == expected


def test_port_must_end_with_digits_boundary():
    # 不带协议的「域名:端口」：端口最多 5 位，后面还连着数字时不算端口，号码照常脱敏
    assert redact_text("example.cn:8080/13800138000") == "example.cn:8080/13800138000"
    assert redact_text("a.cn:13800138000") == "a.cn:<PHONE>"
    # 带协议的链接整体保护，不受这条限制
    assert redact_text("https://a.cn:13800138000/x") == "https://a.cn:13800138000/x"
