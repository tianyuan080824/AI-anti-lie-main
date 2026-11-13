"""评测指标（纯函数，便于手算核对）。

约定：风险概率 p = risk_score / 100；「判为诈骗」= p ≥ 阈值；正类 = label 为 fraud。
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Sequence

#: 契约第 6 节的三支阈值
HIGH_THRESHOLD = 0.60
LOW_THRESHOLD = 0.05
DEFAULT_THRESHOLDS = (0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)
DEFAULT_FPR_TARGETS = (0.01, 0.05, 0.10)


@dataclass(frozen=True)
class Confusion:
    threshold: float
    tp: int
    fp: int
    tn: int
    fn: int

    @property
    def recall(self) -> float | None:
        return _div(self.tp, self.tp + self.fn)

    @property
    def fpr(self) -> float | None:
        return _div(self.fp, self.fp + self.tn)

    @property
    def precision(self) -> float | None:
        return _div(self.tp, self.tp + self.fp)

    @property
    def accuracy(self) -> float | None:
        return _div(self.tp + self.tn, self.tp + self.fp + self.tn + self.fn)

    def as_dict(self) -> dict:
        return {
            "threshold": self.threshold,
            "tp": self.tp,
            "fp": self.fp,
            "tn": self.tn,
            "fn": self.fn,
            "recall": self.recall,
            "fpr": self.fpr,
            "precision": self.precision,
            "accuracy": self.accuracy,
        }


def _div(a: int, b: int) -> float | None:
    return None if b == 0 else a / b


def confusion_at(probs: Sequence[float], positives: Sequence[bool], threshold: float) -> Confusion:
    tp = fp = tn = fn = 0
    for p, pos in zip(probs, positives, strict=True):
        pred = p >= threshold
        if pred and pos:
            tp += 1
        elif pred:
            fp += 1
        elif pos:
            fn += 1
        else:
            tn += 1
    return Confusion(threshold, tp, fp, tn, fn)


@dataclass(frozen=True)
class RecallAtFpr:
    max_fpr: float
    #: 满足 FPR ≤ max_fpr 的最低阈值；None 表示只能一条都不判（阈值无穷大）
    threshold: float | None
    recall: float | None
    fpr: float | None

    def as_dict(self) -> dict:
        return {"max_fpr": self.max_fpr, "threshold": self.threshold, "recall": self.recall, "fpr": self.fpr}


def recall_at_fpr(probs: Sequence[float], positives: Sequence[bool], max_fpr: float) -> RecallAtFpr:
    """在误报率不超过 max_fpr 的阈值里取召回最高的（即最低的可行阈值）。没有负样本时 recall/fpr 为 None。"""
    if not any(not p for p in positives):
        return RecallAtFpr(max_fpr, None, None, None)
    for t in sorted(set(probs)):
        c = confusion_at(probs, positives, t)
        if c.fpr is not None and c.fpr <= max_fpr + 1e-12:
            return RecallAtFpr(max_fpr, t, c.recall, c.fpr)
    c = confusion_at(probs, positives, math.inf)
    return RecallAtFpr(max_fpr, None, c.recall, c.fpr)


def three_way(p: float, high: float = HIGH_THRESHOLD, low: float = LOW_THRESHOLD) -> str:
    """契约第 6 节的三支结论（只看阈值，不含硬性约束）。"""
    if p >= high:
        return "high_risk"
    if p <= low:
        return "low_risk"
    return "suspicious"


def percentile(values: Sequence[float], q: float) -> float | None:
    """线性插值分位数（与 numpy.percentile 默认方法一致），q 取 0–100。"""
    if not values:
        return None
    xs = sorted(values)
    if len(xs) == 1:
        return float(xs[0])
    pos = (len(xs) - 1) * q / 100
    lo = math.floor(pos)
    hi = min(lo + 1, len(xs) - 1)
    return float(xs[lo] + (xs[hi] - xs[lo]) * (pos - lo))


def latency_summary(values_ms: Sequence[float]) -> dict:
    return {
        "n": len(values_ms),
        "p50": percentile(values_ms, 50),
        "p90": percentile(values_ms, 90),
        "p95": percentile(values_ms, 95),
        "max": float(max(values_ms)) if values_ms else None,
    }


def count(items: Iterable[str]) -> dict[str, int]:
    return dict(sorted(Counter(items).items()))
