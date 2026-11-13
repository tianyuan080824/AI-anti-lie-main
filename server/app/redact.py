"""服务端二次脱敏（契约第 2 节）。

接口已冻结：`redact_text(text) -> str`，纯函数、幂等。输入处理在入库、进大模型、写日志之前
对每一行调用它。

顺序固定：保护 URL（和已有占位符）→ 身份证号（18 位且校验位通过）→ 银行卡号（16–19 位连续数字
且 Luhn 通过）→ 手机号（大陆 11 位）→ 金额（带货币符号或 元/块/万 单位的数字）。
匹配前先把全角数字和全角加号换成 ASCII，夹在两个数字之间的全角小数点 `．` 换成 `.`，紧跟 17 位数字的
全角 `Ｘ`/`ｘ` 换成 `X`/`x`；之后只认 ASCII 数字，和 Kotlin 端 `java.util.regex` 的 `[0-9]` 一致。
全角逗号 `，` 不换，也不认万分位：「3000，5000元」更可能是列举两个数，合成一个数会把金额改错。
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

IMPLEMENTED = True

_URL_CHARS = r"[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]"
_DOMAIN = r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+[A-Za-z]{2,}"
_URL = (
    rf"(?:[A-Za-z][A-Za-z0-9+.\-]*://|www\.){_URL_CHARS}+"
    rf"|{_DOMAIN}(?::[0-9]{{1,5}}(?![0-9]))?/{_URL_CHARS}*"
    rf"|{_DOMAIN}:[0-9]{{1,5}}(?![0-9])"
)
_PLACEHOLDER = r"<(?:PHONE|CARD|ID)>|<AMOUNT:[0-9]+(?:\.[0-9]+)?>"
_PROTECT = rf"(?P<keep>{_PLACEHOLDER}|{_URL})"

_ID_RE = re.compile(rf"{_PROTECT}|(?<![0-9])(?P<hit>[0-9]{{17}}[0-9Xx])(?![0-9])")
_CARD_RE = re.compile(rf"{_PROTECT}|(?<![0-9])(?P<hit>[0-9]{{16,19}})(?![0-9])")
# 国家码 +86 / 0086 / +0086 连同号码一起替换，不留下「+86<PHONE>」；不带 + 或 00 的「86」不算国家码
_PHONE_RE = re.compile(
    rf"{_PROTECT}"
    rf"|(?:\+(?:00)?|(?<![0-9])00)86[ \-－\u3000]?1[3-9][0-9]{{9}}(?![0-9])"
    rf"|(?<![0-9])1[3-9][0-9]{{9}}(?![0-9])"
)

_NUMBER = r"[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?"
_CURRENCY = r"[¥￥$＄]|RMB|CNY"
_AMOUNT_RE = re.compile(
    rf"{_PROTECT}"
    rf"|(?:{_CURRENCY})[ ]?(?P<num1>{_NUMBER})(?P<mul1>[万亿千])?(?:元|块钱|块)?"
    rf"|(?<![0-9.])(?P<num2>{_NUMBER})[ ]?(?:(?P<mul2>[万亿])(?:元|块钱|块)?|(?P<mul3>千)(?:元|块钱|块)|元|块钱|块)"
)

_FULLWIDTH = str.maketrans({**{chr(0xFF10 + i): str(i) for i in range(10)}, "＋": "+"})
_FULLWIDTH_POINT = re.compile(r"(?<=[0-9])．(?=[0-9])")
_FULLWIDTH_ID_X = re.compile(r"(?<![0-9])([0-9]{17})([Ｘｘ])")

_MULTIPLIERS = {"万": 10_000, "亿": 100_000_000, "千": 1_000}
_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CHECK = "10X98765432"


def id_check_ok(value: str) -> bool:
    total = sum(int(c) * w for c, w in zip(value[:17], _ID_WEIGHTS))
    return _ID_CHECK[total % 11] == value[17].upper()


def luhn_ok(value: str) -> bool:
    total = 0
    for i, c in enumerate(reversed(value)):
        d = int(c)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _sub(regex: re.Pattern[str], text: str, replace) -> str:
    def repl(m: re.Match[str]) -> str:
        if m.group("keep") is not None:
            return m.group(0)
        return replace(m)

    return regex.sub(repl, text)


def _id(m: re.Match[str]) -> str:
    return "<ID>" if id_check_ok(m.group("hit")) else m.group(0)


def _card(m: re.Match[str]) -> str:
    return "<CARD>" if luhn_ok(m.group("hit")) else m.group(0)


def _amount(m: re.Match[str]) -> str:
    num = m.group("num1") or m.group("num2")
    mul = m.group("mul1") or m.group("mul2") or m.group("mul3")
    try:
        value = Decimal(num.replace(",", "")) * _MULTIPLIERS.get(mul or "", 1)
    except InvalidOperation:
        return m.group(0)
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return f"<AMOUNT:{text}>"


def _redact_once(text: str) -> str:
    text = _sub(_ID_RE, text, _id)
    text = _sub(_CARD_RE, text, _card)
    text = _sub(_PHONE_RE, text, lambda m: "<PHONE>")
    return _sub(_AMOUNT_RE, text, _amount)


def redact_text(text: str) -> str:
    text = _FULLWIDTH_POINT.sub(".", text.translate(_FULLWIDTH))
    text = _FULLWIDTH_ID_X.sub(lambda m: m.group(1) + ("X" if m.group(2) == "Ｘ" else "x"), text)
    # 每次改写至少消掉一个占位符以外的数字，所以一定收敛；迭代到不动点保证严格幂等。
    while True:
        new = _redact_once(text)
        if new == text:
            return text
        text = new
