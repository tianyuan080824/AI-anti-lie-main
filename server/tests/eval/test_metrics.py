"""评测指标与手算结果核对。"""

from __future__ import annotations

import math

import numpy as np
import pytest

from eval.metrics import confusion_at, latency_summary, percentile, recall_at_fpr, three_way

# 4 条诈骗、4 条正常
PROBS = [0.9, 0.7, 0.4, 0.2, 0.65, 0.1, 0.03, 0.0]
POS = [True, True, True, True, False, False, False, False]


def test_confusion_by_hand():
    # 阈值 0.60：诈骗里 0.9、0.7 判中（TP=2，FN=2）；正常里 0.65 误报（FP=1，TN=3）
    c = confusion_at(PROBS, POS, 0.60)
    assert (c.tp, c.fp, c.tn, c.fn) == (2, 1, 3, 2)
    assert c.recall == 0.5 and c.fpr == 0.25 and c.precision == pytest.approx(2 / 3) and c.accuracy == 5 / 8
    # 阈值 0.05：诈骗全中；正常里 0.65、0.1 误报
    c = confusion_at(PROBS, POS, 0.05)
    assert (c.tp, c.fp, c.tn, c.fn) == (4, 2, 2, 0)
    # 恰好等于阈值算判中
    assert confusion_at([0.6], [True], 0.6).tp == 1


def test_confusion_empty_and_one_class():
    c = confusion_at([], [], 0.5)
    assert (c.recall, c.fpr, c.precision, c.accuracy) == (None, None, None, None)
    c = confusion_at([0.9, 0.1], [True, True], 0.5)
    assert c.recall == 0.5 and c.fpr is None


def test_recall_at_fpr_by_hand():
    # 误报率 ≤ 25%：最低可行阈值 0.2（只有 0.65 误报，1/4），此时诈骗全中
    r = recall_at_fpr(PROBS, POS, 0.25)
    assert (r.threshold, r.recall, r.fpr) == (0.2, 1.0, 0.25)
    # 误报率 = 0：要到阈值 0.7 才没有误报，召回 2/4
    r = recall_at_fpr(PROBS, POS, 0.0)
    assert (r.threshold, r.recall, r.fpr) == (0.7, 0.5, 0.0)
    # 误报率 ≤ 50%：阈值 0.1（0.65、0.1 误报）
    r = recall_at_fpr(PROBS, POS, 0.5)
    assert (r.threshold, r.recall, r.fpr) == (0.1, 1.0, 0.5)


def test_recall_at_fpr_infeasible_and_no_negatives():
    # 正常样本分数最高：任何有限阈值都有误报，只能一条都不判
    r = recall_at_fpr([0.5, 0.9], [True, False], 0.0)
    assert (r.threshold, r.recall, r.fpr) == (None, 0.0, 0.0)
    r = recall_at_fpr([0.5], [True], 0.1)
    assert (r.threshold, r.recall, r.fpr) == (None, None, None)


def test_three_way_uses_contract_thresholds():
    assert [three_way(p) for p in PROBS] == [
        "high_risk",
        "high_risk",
        "suspicious",
        "suspicious",
        "high_risk",
        "suspicious",
        "low_risk",
        "low_risk",
    ]
    assert three_way(0.60) == "high_risk" and three_way(0.05) == "low_risk" and three_way(0.0501) == "suspicious"


def test_percentile_by_hand_and_matches_numpy():
    xs = [40, 10, 30, 20]
    # 排序后 10,20,30,40；P50 位置 1.5 → 25；P90 位置 2.7 → 37
    assert percentile(xs, 50) == 25.0
    assert percentile(xs, 90) == pytest.approx(37.0)
    assert percentile(xs, 0) == 10.0 and percentile(xs, 100) == 40.0
    assert percentile([7], 95) == 7.0 and percentile([], 50) is None
    rng = np.random.default_rng(0)
    data = rng.integers(0, 5000, 101).tolist()
    for q in (50, 90, 95):
        assert percentile(data, q) == pytest.approx(float(np.percentile(data, q)))


def test_latency_summary():
    s = latency_summary([100, 200, 300, 400, 500])
    assert s == {"n": 5, "p50": 300.0, "p90": 460.0, "p95": 480.0, "max": 500.0}
    assert latency_summary([])["p50"] is None and not math.isnan(latency_summary([1])["p50"])
