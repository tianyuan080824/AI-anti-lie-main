"""把各片段的合成分汇总成一个媒体结果。阈值偏保守：拿不准一律 uncertain。所有说明都带「仅供参考」。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from app.contract import MediaKind, MediaLabel

VIDEO_SUFFIX = "画面是否换脸没有检测。"

NOTE = {
    MediaLabel.LIKELY_SYNTHETIC: "声音有明显的合成特征，可能是 AI 拟声，仅供参考；请通过当面或原有电话核实对方身份。",
    MediaLabel.LIKELY_GENUINE: "没有发现明显的合成特征，仅供参考，不能据此确认对方身份。",
    MediaLabel.UNCERTAIN: "无法确定声音是否为合成，仅供参考。",
}


@dataclass(frozen=True)
class Verdict:
    label: MediaLabel
    spoof_score: float | None
    note: str
    segments: int


@dataclass(frozen=True)
class Thresholds:
    synthetic: float
    genuine: float
    min_segments: int


def aggregate(scores: Sequence[float], *, voiced_s: float, min_speech_s: float, th: Thresholds, kind: MediaKind) -> Verdict:
    """
    - 有效语音不足 `min_speech_s` 秒或没有可打分的片段：uncertain，不给分数；
    - 片段数少于 `min_segments`：只给 uncertain（分数照报，但不下结论）；
    - 平均分 ≥ synthetic 且至少一半片段 ≥ 0.5：likely_synthetic；
    - 平均分 ≤ genuine 且没有任何片段 ≥ 0.5：likely_genuine；
    - 其余：uncertain。
    """
    suffix = VIDEO_SUFFIX if kind == MediaKind.VIDEO else ""
    if voiced_s < min_speech_s or not scores:
        return Verdict(MediaLabel.UNCERTAIN, None, "有效语音太短，无法判断是否为合成，仅供参考。" + suffix, len(scores))
    mean = sum(scores) / len(scores)
    high = sum(1 for s in scores if s >= 0.5)
    label = MediaLabel.UNCERTAIN
    if len(scores) >= th.min_segments:
        if mean >= th.synthetic and high * 2 >= len(scores):
            label = MediaLabel.LIKELY_SYNTHETIC
        elif mean <= th.genuine and high == 0:
            label = MediaLabel.LIKELY_GENUINE
    return Verdict(label, round(min(max(mean, 0.0), 1.0), 3), NOTE[label] + suffix, len(scores))


def not_analyzed_note(reason: str, kind: MediaKind) -> str:
    """not_analyzed 的说明：原因 + 仅供参考。原因是不含正文的中文短句。"""
    suffix = VIDEO_SUFFIX if kind == MediaKind.VIDEO and "换脸" not in reason else ""
    return f"{reason.rstrip('。')}，未做分析，仅供参考。{suffix}"


__all__ = ["NOTE", "Thresholds", "VIDEO_SUFFIX", "Verdict", "aggregate", "not_analyzed_note"]
