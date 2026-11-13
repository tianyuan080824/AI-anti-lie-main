"""对外 ID：前缀 + 26 位 ULID（Crockford Base32，时间有序）。"""

from __future__ import annotations

import os
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def ulid() -> str:
    value = (int(time.time() * 1000) << 80) | int.from_bytes(os.urandom(10), "big")
    chars = []
    for _ in range(26):
        chars.append(_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(chars))


def new_check_id() -> str:
    return "c_" + ulid()


def new_trace_id() -> str:
    return "t_" + ulid()


def new_media_id() -> str:
    return "m_" + ulid()
