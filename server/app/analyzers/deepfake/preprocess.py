"""媒体预处理：取文件、转码、OCR / 转写，并打合成分。

两个调用点：

- `prepare(inp, services)`：流水线跑四个分析器之前调用，把派生行补进 `AnalysisInput`，
  让话术 / 链接 / 案例三路也能看到转写和 OCR 文本。返回 `Prepared`，分析器的 `run()` 再取合成分。
- `analyze_item(...)`：分析器 `run()` 自己的入口（流水线没接预处理时也能独立出结果）。

任何失败都降级成 `not_analyzed` + `degraded`，不向外抛（`CancelledError` 除外，超时靠它生效）。
原文件读进内存后立即删除。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

import numpy as np

from app.analyzers.base import AnalysisInput, InputLine, MediaInput
from app.analyzers.deepfake.engines import Engines
from app.analyzers.deepfake.probe import MediaRejected, ToolError, decode_audio, load_image_bgr
from app.analyzers.deepfake.settings import MediaSettings
from app.analyzers.deepfake.spoof import Thresholds, Verdict, aggregate, not_analyzed_note
from app.analyzers.deepfake.storage import claim, file_path, remove
from app.analyzers.deepfake.text import (
    asr_chunks,
    clean_transcript,
    derived_lines,
    ocr_lines,
    pcm16_to_float,
    sentences,
    spoof_segments,
    voiced_seconds,
)
from app.config import Settings
from app.contract import DegradedFlag, DerivedLine, MediaKind, MediaLabel, MediaResult
from app.db import Database
from app.logs import log_event
from app.services import Services

logger = logging.getLogger("app.analyzers.deepfake")

#: 派生行单行上限，沿用契约里行长度的限制
LINE_CHARS = 500


@dataclass
class ItemOutcome:
    media: MediaResult
    lines: list[DerivedLine] = field(default_factory=list)
    degraded: set[DegradedFlag] = field(default_factory=set)


@dataclass
class Prepared:
    """预处理的产出，等 `run()` 来取。"""

    outcomes: dict[str, ItemOutcome] = field(default_factory=dict)
    lines: list[DerivedLine] = field(default_factory=list)
    degraded: set[DegradedFlag] = field(default_factory=set)
    created_at: float = field(default_factory=time.monotonic)

    def take(self, media_id: str) -> ItemOutcome | None:
        return self.outcomes.pop(media_id, None)


def _thresholds(ms: MediaSettings) -> Thresholds:
    return Thresholds(synthetic=ms.synthetic_threshold, genuine=ms.genuine_threshold, min_segments=1)


def _not_analyzed(item: MediaInput, reason: str, *, degraded: bool = False) -> ItemOutcome:
    return ItemOutcome(
        media=MediaResult(
            media_id=item.media_id,
            kind=item.kind,
            label=MediaLabel.NOT_ANALYZED,
            note=not_analyzed_note(reason, item.kind),
        ),
        degraded={DegradedFlag.DEEPFAKE_UNAVAILABLE} if degraded else set(),
    )


def _from_verdict(item: MediaInput, verdict: Verdict, lines: list[DerivedLine]) -> ItemOutcome:
    return ItemOutcome(
        media=MediaResult(
            media_id=item.media_id,
            kind=item.kind,
            label=verdict.label,
            spoof_score=verdict.spoof_score,
            note=verdict.note,
        ),
        lines=lines,
    )


async def _read_and_delete(settings: Settings, file_name: str) -> bytes | None:
    """读进内存后立即删除原文件。文件不在了返回 None（例如被过期清理删掉）。"""
    path = file_path(settings, file_name)

    def read() -> bytes | None:
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return None
        remove(path)
        return data

    return await asyncio.to_thread(read)


async def analyze_item(
    item: MediaInput,
    *,
    check_id: str,
    services: Services,
    engines: Engines,
    ms: MediaSettings,
    budget_s: float,
) -> ItemOutcome:
    """分析一条媒体。不抛异常（CancelledError 除外）。"""
    try:
        return await asyncio.wait_for(_analyze(item, check_id, services, engines, ms), budget_s)
    except TimeoutError:
        log_event(logger, "media.budget_exceeded", logging.WARNING, media_id=item.media_id, kind=item.kind.value)
        return _not_analyzed(item, "分析超时", degraded=True)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - 降级，不让一条媒体拖垮整单
        log_event(logger, "media.analyze_failed", logging.ERROR, media_id=item.media_id, error=type(exc).__name__)
        return _not_analyzed(item, "分析失败", degraded=True)


async def _analyze(item: MediaInput, check_id: str, services: Services, engines: Engines, ms: MediaSettings) -> ItemOutcome:
    settings = services.settings
    claimed = await claim(services.db, item.media_id, check_id)
    if claimed is None:
        # 引用了媒体却取不到（没上传、已过期、被清掉）：对调用方来说就是这路分析不可用
        return _not_analyzed(item, "媒体不存在或已过期", degraded=True)
    if claimed.record.kind != item.kind.value:
        return _not_analyzed(item, "媒体类型和上传时不一致")
    if not claimed.owned:
        # 同一次鉴定重试：原文件已删，没法再分析
        return _not_analyzed(item, "这份媒体已经分析过")
    data = await _read_and_delete(settings, claimed.record.file_name)
    if data is None:
        return _not_analyzed(item, "媒体文件已不存在")
    log_event(logger, "media.loaded", media_id=item.media_id, kind=item.kind.value, size=len(data))
    if item.kind == MediaKind.IMAGE:
        return await _image(item, data, engines, ms)
    return await _speech(item, data, settings, engines, ms)


async def _image(item: MediaInput, data: bytes, engines: Engines, ms: MediaSettings) -> ItemOutcome:
    ocr = await engines.ocr.get(ms.preprocess_budget_s)
    if ocr is None:
        return _not_analyzed(item, "文字识别不可用", degraded=True)
    try:
        bgr = await asyncio.to_thread(load_image_bgr, data)
        boxes = await asyncio.to_thread(ocr.recognize, bgr)
    except MediaRejected:
        return _not_analyzed(item, "图片无法读取")
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        log_event(logger, "media.ocr_failed", logging.ERROR, media_id=item.media_id, error=type(exc).__name__)
        return _not_analyzed(item, "文字识别失败", degraded=True)
    texts = ocr_lines(boxes, min_score=ms.ocr_min_score)
    lines = derived_lines(texts, item_index=item.item_index, max_lines=ms.max_derived_lines, max_chars=LINE_CHARS)
    log_event(logger, "media.ocr_done", media_id=item.media_id, lines=len(lines))
    return ItemOutcome(
        media=MediaResult(
            media_id=item.media_id,
            kind=item.kind,
            label=MediaLabel.NOT_ANALYZED,
            note=not_analyzed_note("图片只做文字识别，没有做合成检测", item.kind),
        ),
        lines=lines,
    )


async def _speech(item: MediaInput, data: bytes, settings: Settings, engines: Engines, ms: MediaSettings) -> ItemOutcome:
    tools = _tools(settings)
    if tools is None:
        return _not_analyzed(item, "服务器没有安装 ffmpeg", degraded=True)
    ffmpeg, _ffprobe = tools
    max_seconds = settings.limit_audio_seconds if item.kind == MediaKind.AUDIO else settings.limit_video_seconds
    with _temp_file(settings, data) as path:
        try:
            pcm = await decode_audio(ffmpeg, path, max_seconds=max_seconds, timeout_s=ms.probe_timeout_s)
        except (MediaRejected, ToolError, OSError) as exc:
            log_event(logger, "media.decode_failed", logging.WARNING, media_id=item.media_id, error=type(exc).__name__)
            return _not_analyzed(item, "音频无法解码", degraded=True)
    audio = pcm16_to_float(pcm)
    lines, lines_degraded = await _transcribe(item, audio, engines, ms)
    verdict, spoof_degraded = await _score(item, audio, engines, ms)
    degraded = set()
    if lines_degraded or spoof_degraded:
        degraded.add(DegradedFlag.PARTIAL_TIMEOUT)
    if verdict is None:
        return ItemOutcome(
            media=MediaResult(
                media_id=item.media_id,
                kind=item.kind,
                label=MediaLabel.NOT_ANALYZED,
                note=not_analyzed_note("合成语音检测不可用", item.kind),
            ),
            lines=lines,
            degraded=degraded | {DegradedFlag.DEEPFAKE_UNAVAILABLE},
        )
    return ItemOutcome(
        media=MediaResult(
            media_id=item.media_id, kind=item.kind, label=verdict.label, spoof_score=verdict.spoof_score, note=verdict.note
        ),
        lines=lines,
        degraded=degraded,
    )


def _tools(settings: Settings) -> tuple[str, str] | None:
    from app.analyzers.deepfake.env import find_tools

    tools = find_tools(settings)
    if not tools.ok:
        return None
    assert tools.ffmpeg and tools.ffprobe
    return tools.ffmpeg, tools.ffprobe


class _TempFile:
    """解码用的临时文件：ffmpeg 只能读路径，用完立即删除。"""

    def __init__(self, settings: Settings, data: bytes) -> None:
        from app.analyzers.deepfake.storage import ensure_media_dir, new_file_name

        self.path = ensure_media_dir(settings) / (new_file_name() + ".part")
        self.path.write_bytes(data)

    def __enter__(self):
        return self.path

    def __exit__(self, *exc: object) -> None:
        remove(self.path)


def _temp_file(settings: Settings, data: bytes) -> _TempFile:
    return _TempFile(settings, data)


async def _transcribe(item: MediaInput, audio: np.ndarray, engines: Engines, ms: MediaSettings) -> tuple[list[DerivedLine], bool]:
    """返回 (派生行, 是否降级)。"""
    asr = await engines.asr.get(0)
    if asr is None:
        return [], True
    try:
        chunks = asr_chunks(audio, max_s=ms.asr_chunk_s)
        parts = [await asyncio.to_thread(asr.transcribe, audio[start:end]) for start, end in chunks]
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        log_event(logger, "media.asr_failed", logging.ERROR, media_id=item.media_id, error=type(exc).__name__)
        return [], True
    texts = sentences(clean_transcript(" ".join(parts)), max_chars=LINE_CHARS)
    lines = derived_lines(texts, item_index=item.item_index, max_lines=ms.max_derived_lines, max_chars=LINE_CHARS)
    log_event(logger, "media.asr_done", media_id=item.media_id, lines=len(lines), chars=sum(len(line.text) for line in lines))
    return lines, False


async def _score(item: MediaInput, audio: np.ndarray, engines: Engines, ms: MediaSettings) -> tuple[Verdict | None, bool]:
    spoof = await engines.spoof.get(0)
    if spoof is None:
        return None, True
    try:
        voiced = await asyncio.to_thread(voiced_seconds, audio)
        segments = await asyncio.to_thread(spoof_segments, audio, max_segments=8, min_voiced_s=ms.min_speech_s)
        scores = await asyncio.to_thread(spoof.scores, segments) if len(segments) else []
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        log_event(logger, "media.spoof_failed", logging.ERROR, media_id=item.media_id, error=type(exc).__name__)
        return None, True
    return aggregate(scores, voiced_s=voiced, min_speech_s=ms.min_speech_s, th=_thresholds(ms), kind=item.kind), False


async def prepare(inp: AnalysisInput, services: Services, engines: Engines, ms: MediaSettings) -> tuple[AnalysisInput, Prepared]:
    """流水线入口：在预算内预处理全部媒体，把派生行补进输入。不抛异常（CancelledError 除外）。"""
    if not inp.media or not ms.enabled:
        return inp, Prepared()
    t0 = time.perf_counter()
    tasks = [
        asyncio.create_task(
            analyze_item(item, check_id=inp.check_id, services=services, engines=engines, ms=ms, budget_s=ms.preprocess_budget_s)
        )
        for item in inp.media
    ]
    try:
        async with asyncio.timeout(ms.preprocess_budget_s):
            outcomes = await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        for task in tasks:
            task.cancel()
        raise
    except TimeoutError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        outcomes = [task.result() if task.done() and not task.cancelled() else None for task in tasks]
        outcomes = [
            outcome or _not_analyzed(item, "分析超时", degraded=True) for item, outcome in zip(inp.media, outcomes)
        ]
    prepared = Prepared()
    extra_lines: list[InputLine] = []
    for outcome in outcomes:
        prepared.outcomes[outcome.media.media_id] = outcome
        prepared.lines.extend(outcome.lines)
        prepared.degraded |= outcome.degraded
        extra_lines.extend(
            InputLine(line_id=line.line_id, speaker=line.speaker, text=line.text, item_index=line.item_index)
            for line in outcome.lines
        )
    log_event(
        logger,
        "media.prepared",
        check_id=inp.check_id,
        items=len(inp.media),
        lines=len(prepared.lines),
        elapsed_ms=int((time.perf_counter() - t0) * 1000),
    )
    if not extra_lines:
        return inp, prepared
    merged = inp.model_copy(update={"lines": inp.lines + tuple(extra_lines)})
    return merged, prepared


async def sweep_expired(db: Database, settings: Settings, ms: MediaSettings) -> None:
    from app.analyzers.deepfake.storage import sweep

    await sweep(db, settings, ms)


__all__ = ["ItemOutcome", "Prepared", "analyze_item", "prepare"]
