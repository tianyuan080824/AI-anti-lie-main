"""进程内令牌桶限流（按设备标识）。"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    def __init__(self, per_minute: float, burst: int, *, clock=time.monotonic, max_keys: int = 10_000) -> None:
        self.rate = per_minute / 60.0
        self.burst = float(max(1, burst))
        self.clock = clock
        self.max_keys = max_keys
        self._buckets: dict[str, _Bucket] = {}

    def acquire(self, key: str) -> tuple[bool, float]:
        """取一个令牌。返回 (是否允许, 需要等待的秒数)。"""
        now = self.clock()
        bucket = self._buckets.get(key)
        if bucket is None:
            if len(self._buckets) >= self.max_keys:
                self._evict(now)
            bucket = self._buckets[key] = _Bucket(tokens=self.burst, updated=now)
        else:
            bucket.tokens = min(self.burst, bucket.tokens + (now - bucket.updated) * self.rate)
            bucket.updated = now
        if bucket.tokens >= 1:
            bucket.tokens -= 1
            return True, 0.0
        wait = (1 - bucket.tokens) / self.rate if self.rate > 0 else 60.0
        return False, wait

    def _evict(self, now: float) -> None:
        full_after = self.burst / self.rate if self.rate > 0 else float("inf")
        for key in [k for k, b in self._buckets.items() if now - b.updated >= full_after]:
            del self._buckets[key]
        if len(self._buckets) >= self.max_keys:
            self._buckets.clear()
