"""打分（契约第 6 节第 1、2 条）：各路信号 → 对数几率分量 → 风险概率 p。

权重在同目录的 `weights.yaml`。这里只算分，硬性约束在 `app/summary/` 里实现。
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Iterable

import yaml
from pydantic import BaseModel, ConfigDict

from app.contract import (
    Evidence,
    Intent,
    LinkReason,
    LinkResult,
    LinkRisk,
    MediaLabel,
    SimilarCase,
    Verdict,
)

WEIGHTS_FILE = Path(__file__).resolve().parent / "weights.yaml"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Thresholds(_Strict):
    high_risk: float
    low_risk: float


class IntentWeights(_Strict):
    weights: dict[Intent, float]
    repeat_bonus: float
    repeat_cap: float


class RuleWeights(_Strict):
    factor_with_llm: float
    factor_rules_only: float
    cap: float


class LinkWeights(_Strict):
    risk: dict[LinkRisk, float]
    blacklist_bonus: float


class CaseWeights(_Strict):
    weight: float
    floor: float


class DeepfakeWeights(_Strict):
    weight: float
    cap: float


class Weights(_Strict):
    version: str
    bias: float
    thresholds: Thresholds
    capped_p: float
    intents: IntentWeights
    rules: RuleWeights
    links: LinkWeights
    cases: CaseWeights
    deepfake: DeepfakeWeights


def load_weights(path: Path = WEIGHTS_FILE) -> Weights:
    return _load(path, path.stat().st_mtime_ns)


@lru_cache(maxsize=4)
def _load(path: Path, mtime_ns: int) -> Weights:
    weights = Weights.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    missing = set(Intent) - set(weights.intents.weights)
    if missing:
        raise ValueError(f"weights.yaml: missing intent weights {sorted(missing)}")
    return weights


def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def risk_score(p: float) -> int:
    """契约：risk_score = round(100·p)。"""
    return int(round(100 * p))


def verdict_for(p: float, w: Weights) -> Verdict:
    if p >= w.thresholds.high_risk:
        return Verdict.HIGH_RISK
    if p <= w.thresholds.low_risk:
        return Verdict.LOW_RISK
    return Verdict.SUSPICIOUS


@dataclass(frozen=True)
class RuleSignal:
    rule_id: str
    weight: float


@dataclass
class Signals:
    """进打分的信号。`evidence` 只放通过逐字回查的大模型证据。"""

    evidence: list[Evidence] = field(default_factory=list)
    rules: list[RuleSignal] = field(default_factory=list)
    links: list[LinkResult] = field(default_factory=list)
    cases: list[SimilarCase] = field(default_factory=list)
    media: list = field(default_factory=list)
    llm_available: bool = True


@dataclass(frozen=True)
class Score:
    bias: float
    intents: float
    rules: float
    links: float
    cases: float
    deepfake: float

    @property
    def logit(self) -> float:
        return self.bias + self.intents + self.rules + self.links + self.cases + self.deepfake

    @property
    def p(self) -> float:
        return sigmoid(self.logit)

    @property
    def p_without_deepfake(self) -> float:
        return sigmoid(self.logit - self.deepfake)

    def as_dict(self) -> dict[str, float]:
        return {
            "bias": self.bias,
            "intents": round(self.intents, 4),
            "rules": round(self.rules, 4),
            "links": round(self.links, 4),
            "cases": round(self.cases, 4),
            "deepfake": round(self.deepfake, 4),
            "p": round(self.p, 4),
        }


def intent_component(evidence: Iterable[Evidence], w: Weights) -> float:
    counts = Counter(e.intent for e in evidence)
    total = 0.0
    for intent, n in counts.items():
        extra = min(w.intents.repeat_cap, (n - 1) * w.intents.repeat_bonus)
        total += w.intents.weights[intent] + extra
    return total


def rule_component(rules: Iterable[RuleSignal], w: Weights, *, llm_available: bool) -> float:
    by_id: dict[str, float] = {}
    for r in rules:
        by_id[r.rule_id] = max(by_id.get(r.rule_id, 0.0), r.weight)
    factor = w.rules.factor_with_llm if llm_available else w.rules.factor_rules_only
    return min(w.rules.cap, sum(by_id.values()) * factor)


def is_blacklisted(link: LinkResult) -> bool:
    return LinkReason.BLACKLIST_HIT in link.reasons


def link_component(links: Iterable[LinkResult], w: Weights) -> float:
    best = 0.0
    for link in links:
        value = w.links.risk.get(link.risk, 0.0)
        if is_blacklisted(link):
            value += w.links.blacklist_bonus
        best = max(best, value)
    return best


def case_component(cases: Iterable[SimilarCase], w: Weights) -> float:
    top = max((c.similarity for c in cases), default=0.0)
    if top <= w.cases.floor or w.cases.floor >= 1:
        return 0.0
    return w.cases.weight * (top - w.cases.floor) / (1 - w.cases.floor)


def deepfake_component(media: Iterable, w: Weights) -> float:
    best = 0.0
    for m in media:
        if m.label == MediaLabel.LIKELY_SYNTHETIC:
            best = max(best, w.deepfake.weight * float(m.spoof_score or 0.0))
    return min(w.deepfake.cap, best)


def score(signals: Signals, w: Weights) -> Score:
    return Score(
        bias=w.bias,
        intents=intent_component(signals.evidence, w),
        rules=rule_component(signals.rules, w, llm_available=signals.llm_available),
        links=link_component(signals.links, w),
        cases=case_component(signals.cases, w),
        deepfake=deepfake_component(signals.media, w),
    )
