"""纯函数：OCR 框拼行、转写文本切句、生成派生行、音频切片和静音判断。不导入大依赖（只用 numpy）。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Sequence

import numpy as np

from app.contract import DerivedLine, Speaker
from app.redact import redact_text

SAMPLE_RATE = 16_000
#: AASIST 的输入长度（约 4 秒）
SEGMENT_SAMPLES = 64_600
FRAME = 400  # 25 ms

_TAG = re.compile(r"<\|[^|>]*\|>")
_SENTENCE = re.compile(r"[^。！？!?；;\n]*[。！？!?；;\n]+|[^。！？!?；;\n]+$")
_SPACES = re.compile(r"\s+")


@dataclass(frozen=True)
class OcrBox:
    text: str
    score: float
    #: 四个顶点 [[x, y], ...]
    box: tuple[tuple[float, float], ...]


def ocr_lines(boxes: Iterable[OcrBox], *, min_score: float) -> list[str]:
    """把 OCR 文本框按纵向位置拼成行，从上到下、从左到右。丢掉置信度低的框。"""
    kept = []
    for b in boxes:
        text = b.text.strip()
        if not text or b.score < min_score or not b.box:
            continue
        ys = [p[1] for p in b.box]
        xs = [p[0] for p in b.box]
        top, bottom = min(ys), max(ys)
        kept.append((text, (top + bottom) / 2, max(bottom - top, 1.0), min(xs)))
    kept.sort(key=lambda k: (k[1], k[3]))
    rows: list[list[tuple[str, float, float, float]]] = []
    for item in kept:
        if rows:
            row = rows[-1]
            center = sum(r[1] for r in row) / len(row)
            height = min(min(r[2] for r in row), item[2])
            if abs(item[1] - center) <= height * 0.5:
                row.append(item)
                continue
        rows.append([item])
    return [" ".join(r[0] for r in sorted(row, key=lambda r: r[3])) for row in rows]


def clean_transcript(text: str) -> str:
    """去掉 SenseVoice 的 `<|zh|><|NEUTRAL|>...` 标签。"""
    return _SPACES.sub(" ", _TAG.sub("", text)).strip()


def sentences(text: str, *, max_chars: int) -> list[str]:
    """按句末标点切句；太短的并到上一句，太长的硬切。"""
    out: list[str] = []
    for match in _SENTENCE.finditer(text):
        sent = match.group(0).strip()
        if not sent:
            continue
        if out and len(sent) <= 3 and len(out[-1]) + len(sent) <= max_chars:
            out[-1] += sent
            continue
        out.extend(sent[i : i + max_chars] for i in range(0, len(sent), max_chars))
    return out


def derived_lines(texts: Sequence[str], *, item_index: int, max_lines: int, max_chars: int) -> list[DerivedLine]:
    """生成派生行：先脱敏，line_id 为 `d<item 序号>-<行序号>`，说话人 unknown。"""
    lines: list[DerivedLine] = []
    for text in texts:
        if len(lines) >= max_lines:
            break
        text = redact_text(text.strip())
        if not text:
            continue
        if len(text) > max_chars:
            text = text[:max_chars]
        lines.append(
            DerivedLine(line_id=f"d{item_index}-{len(lines)}", speaker=Speaker.UNKNOWN, text=text, item_index=item_index)
        )
    return lines


def pcm16_to_float(data: bytes) -> np.ndarray:
    usable = len(data) - len(data) % 2
    return np.frombuffer(data[:usable], dtype="<i2").astype(np.float32) / 32768.0


def frame_rms(pcm: np.ndarray) -> np.ndarray:
    n = len(pcm) // FRAME
    if n == 0:
        return np.zeros(0, dtype=np.float32)
    frames = pcm[: n * FRAME].reshape(n, FRAME)
    return np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1)).astype(np.float32)


def voiced_mask(rms: np.ndarray) -> np.ndarray:
    """粗略的有声帧判断：高于 -54 dBFS，且不低于响亮部分（95 分位）的十分之一。"""
    if rms.size == 0:
        return np.zeros(0, dtype=bool)
    floor = max(0.002, 0.1 * float(np.percentile(rms, 95)))
    return rms >= floor


def voiced_seconds(pcm: np.ndarray) -> float:
    return float(voiced_mask(frame_rms(pcm)).sum()) * FRAME / SAMPLE_RATE


def spoof_segments(pcm: np.ndarray, *, max_segments: int, min_voiced_s: float = 1.0) -> np.ndarray:
    """切成 AASIST 用的定长片段（不重叠）。有声部分不足 `min_voiced_s` 秒的片段丢掉；
    最后一段不够长时循环补齐（和 AASIST 原仓库的做法一致）。片段太多时等间隔抽取。"""
    rms = frame_rms(pcm)
    voiced = voiced_mask(rms)
    frames_per_seg = SEGMENT_SAMPLES // FRAME
    min_frames = int(min_voiced_s * SAMPLE_RATE / FRAME)
    segs: list[np.ndarray] = []
    for start in range(0, len(pcm), SEGMENT_SAMPLES):
        chunk = pcm[start : start + SEGMENT_SAMPLES]
        f0 = start // FRAME
        if int(voiced[f0 : f0 + frames_per_seg].sum()) < min_frames:
            continue
        if len(chunk) < SEGMENT_SAMPLES:
            reps = SEGMENT_SAMPLES // len(chunk) + 1
            chunk = np.tile(chunk, reps)[:SEGMENT_SAMPLES]
        segs.append(chunk)
    if len(segs) > max_segments > 0:
        idx = np.linspace(0, len(segs) - 1, max_segments).round().astype(int)
        segs = [segs[i] for i in idx]
    if not segs:
        return np.zeros((0, SEGMENT_SAMPLES), dtype=np.float32)
    return np.stack(segs).astype(np.float32)


def asr_chunks(pcm: np.ndarray, *, max_s: float) -> list[tuple[int, int]]:
    """长音频切成不超过 `max_s` 秒的片段，切点选在每段末尾 5 秒里最安静的一帧。"""
    max_len = int(max_s * SAMPLE_RATE)
    n = len(pcm)
    if n <= max_len:
        return [(0, n)] if n else []
    window = min(5 * SAMPLE_RATE, max_len // 2)
    out: list[tuple[int, int]] = []
    start = 0
    while n - start > max_len:
        lo, hi = start + max_len - window, start + max_len
        rms = frame_rms(pcm[lo:hi])
        cut = lo + (int(np.argmin(rms)) * FRAME + FRAME // 2 if rms.size else window)
        out.append((start, cut))
        start = cut
    out.append((start, n))
    return out


__all__ = [
    "OcrBox",
    "SAMPLE_RATE",
    "SEGMENT_SAMPLES",
    "asr_chunks",
    "clean_transcript",
    "derived_lines",
    "frame_rms",
    "ocr_lines",
    "pcm16_to_float",
    "sentences",
    "spoof_segments",
    "voiced_mask",
    "voiced_seconds",
]
