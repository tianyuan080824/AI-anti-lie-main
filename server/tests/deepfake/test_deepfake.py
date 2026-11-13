"""媒体分析的单元测试：不加载模型、不联网、不需要 ffmpeg（探测类测试除外）。

模型推理一律用假引擎（`Engine` 直接塞结果），只验证编排、降级和登记。
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import numpy as np
import pytest

from app.analyzers.base import AnalysisInput, AnalyzerContext, MediaInput
from app.analyzers.deepfake.analyzer import DeepfakeAnalyzer
from app.analyzers.deepfake.engines import Engine, Engines
from app.analyzers.deepfake.models import MediaRecord
from app.analyzers.deepfake.preprocess import analyze_item
from app.analyzers.deepfake.settings import MediaSettings
from app.analyzers.deepfake.spoof import Thresholds, aggregate, not_analyzed_note
from app.analyzers.deepfake.storage import add, claim, file_path, lifetime_s, new_file_name, sweep
from app.analyzers.deepfake.text import (
    OcrBox,
    clean_transcript,
    derived_lines,
    ocr_lines,
    sentences,
    spoof_segments,
    voiced_seconds,
)
from app.config import Settings
from app.contract import AnalyzerStatus, DegradedFlag, MediaKind, MediaLabel, Speaker
from app.db import Database, iso_utc, utcnow
from app.services import Services
from tests.conftest import AUTH

PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------


def test_ocr_lines_group_rows_and_drop_low_score() -> None:
    boxes = [
        OcrBox("你好", 0.9, ((0, 0), (40, 0), (40, 20), (0, 20))),
        OcrBox("世界", 0.9, ((50, 1), (90, 1), (90, 21), (50, 21))),
        OcrBox("下一行", 0.9, ((0, 40), (60, 40), (60, 60), (0, 60))),
        OcrBox("模糊", 0.2, ((0, 80), (40, 80), (40, 100), (0, 100))),
    ]
    assert ocr_lines(boxes, min_score=0.5) == ["你好 世界", "下一行"]


def test_clean_transcript_strips_sensevoice_tags() -> None:
    assert clean_transcript("<|zh|><|NEUTRAL|><|Speech|>日结兼职。<|woitn|>") == "日结兼职。"


def test_sentences_split_and_cap() -> None:
    assert sentences("第一句。第二句！", max_chars=100) == ["第一句。", "第二句！"]
    assert sentences("甲" * 12, max_chars=5) == ["甲" * 5, "甲" * 5, "甲" * 2]


def test_derived_lines_redact_and_number() -> None:
    lines = derived_lines(["打我电话13812345678", ""], item_index=2, max_lines=10, max_chars=100)
    assert len(lines) == 1
    assert lines[0].line_id == "d2-0"
    assert lines[0].speaker == Speaker.UNKNOWN
    assert lines[0].item_index == 2
    assert "13812345678" not in lines[0].text
    assert lines[0].text == "打我电话<PHONE>"


def test_voiced_seconds_zero_for_silence() -> None:
    silence = np.zeros(16_000, dtype=np.float32)
    assert voiced_seconds(silence) == 0.0


def test_spoof_segments_drop_silence_and_keep_speech() -> None:
    silence = np.zeros(64_600, dtype=np.float32)
    speech = np.zeros(64_600, dtype=np.float32)
    speech[::2] = 0.3
    out = spoof_segments(np.concatenate([silence, speech]), max_segments=4, min_voiced_s=1.0)
    assert out.shape == (1, 64_600)


def test_aggregate_is_conservative() -> None:
    th = Thresholds(synthetic=0.9, genuine=0.1, min_segments=1)
    short = aggregate([], voiced_s=0.2, min_speech_s=1.0, th=th, kind=MediaKind.AUDIO)
    assert short.label == MediaLabel.UNCERTAIN and short.spoof_score is None
    synthetic = aggregate([0.95, 0.92], voiced_s=5.0, min_speech_s=1.0, th=th, kind=MediaKind.AUDIO)
    assert synthetic.label == MediaLabel.LIKELY_SYNTHETIC
    mixed = aggregate([0.95, 0.1], voiced_s=5.0, min_speech_s=1.0, th=th, kind=MediaKind.AUDIO)
    assert mixed.label == MediaLabel.UNCERTAIN
    video = aggregate([0.95, 0.95], voiced_s=5.0, min_speech_s=1.0, th=th, kind=MediaKind.VIDEO)
    assert "换脸" in (video.note or "")
    assert "仅供参考" in not_analyzed_note("缺权重", MediaKind.AUDIO)


# ---------------------------------------------------------------------------
# 登记、认领、清理
# ---------------------------------------------------------------------------


async def _db(tmp_path) -> Database:
    db = Database(tmp_path / "t.db")
    await db.create_all()
    return db


def _record(media_id: str, *, seconds_left: float, file_name: str | None = None) -> MediaRecord:
    now = utcnow()
    return MediaRecord(
        media_id=media_id,
        kind="audio",
        file_name=file_name or new_file_name(),
        size_bytes=10,
        format_name="wav",
        has_audio=True,
        created_at=now,
        expires_at=now + timedelta(seconds=seconds_left),
    )


async def test_claim_is_single_use_and_expiry_blocks(tmp_path) -> None:
    db = await _db(tmp_path)
    fresh, stale = _record("m_fresh", seconds_left=600), _record("m_stale", seconds_left=-10)
    await add(db, fresh)
    await add(db, stale)
    first = await claim(db, "m_fresh", "c_1")
    assert first is not None and first.owned
    again = await claim(db, "m_fresh", "c_2")
    assert again is not None and not again.owned
    assert await claim(db, "m_stale", "c_1") is None
    assert await claim(db, "m_missing", "c_1") is None
    await db.dispose()


async def test_sweep_removes_expired_rows_and_orphan_files(tmp_path, settings: Settings) -> None:
    db = await _db(tmp_path)
    settings = settings.model_copy(update={"var_dir": tmp_path / "var"})
    expired = _record("m_old", seconds_left=-5)
    (file_path(settings, expired.file_name)).parent.mkdir(parents=True, exist_ok=True)
    file_path(settings, expired.file_name).write_bytes(b"x")
    await add(db, expired)
    orphan = (tmp_path / "var" / "media" / (new_file_name() + ".part"))
    orphan.write_bytes(b"y")
    import os

    os.utime(orphan, (0, 0))
    ms = MediaSettings(sweep_interval_s=60)
    result = await sweep(db, settings, ms)
    assert result.rows == 1 and result.files == 2
    assert not file_path(settings, expired.file_name).exists()
    await db.dispose()


def test_lifetime_never_exceeds_one_hour(settings: Settings) -> None:
    ms = MediaSettings(sweep_interval_s=60)
    assert lifetime_s(settings, ms) <= 3600
    assert lifetime_s(settings.model_copy(update={"media_max_age_s": 30}), ms) >= 60


# ---------------------------------------------------------------------------
# 分析编排（假引擎）
# ---------------------------------------------------------------------------


class _FakeOcr:
    def recognize(self, bgr: np.ndarray) -> list[OcrBox]:
        return [OcrBox("加我 13812345678", 0.99, ((0, 0), (100, 0), (100, 20), (0, 20)))]


class _FakeAsr:
    def transcribe(self, pcm: np.ndarray) -> str:
        return "<|zh|>转账五千元到13812345678"


class _FakeSpoof:
    def __init__(self, score: float) -> None:
        self.score = score

    def scores(self, segments: np.ndarray) -> list[float]:
        return [self.score] * len(segments)


def _engines(**ready: Any) -> Engines:
    def engine(name: str) -> Engine:
        if name in ready:
            return Engine(name, lambda obj=ready[name]: obj)
        return Engine(name, lambda: None, unavailable="测试里不可用")

    engines = Engines(ocr=engine("ocr"), asr=engine("asr"), spoof=engine("spoof"))
    engines.start_all()
    return engines


def _media_settings() -> MediaSettings:
    return MediaSettings(preprocess_budget_s=5, preload=False)


async def _services(tmp_path, settings: Settings) -> Services:
    db = await _db(tmp_path)
    return Services(settings=settings.model_copy(update={"var_dir": tmp_path / "var"}), db=db)


async def _store_media(services: Services, kind: str, data: bytes) -> str:
    from app.ids import new_media_id

    record = _record(new_media_id(), seconds_left=600, file_name=new_file_name())
    record.kind = kind
    path = file_path(services.settings, record.file_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    await add(services.db, record)
    return record.media_id


async def test_image_ocr_produces_redacted_lines_and_deletes_file(tmp_path, settings: Settings) -> None:
    services = await _services(tmp_path, settings)
    media_id = await _store_media(services, "image", PNG_1X1)
    item = MediaInput(media_id=media_id, kind=MediaKind.IMAGE, item_index=1)
    outcome = await analyze_item(item, check_id="c_t", services=services, engines=_engines(ocr=_FakeOcr()), ms=_media_settings(), budget_s=5)
    assert outcome.media.label == MediaLabel.NOT_ANALYZED
    assert outcome.lines and outcome.lines[0].line_id == "d1-0"
    assert outcome.lines[0].text == "加我 <PHONE>"
    claimed = await claim(services.db, media_id, "c_x")
    assert claimed is not None and not claimed.owned
    await services.db.dispose()


async def test_missing_media_is_not_analyzed_and_degraded(tmp_path, settings: Settings) -> None:
    services = await _services(tmp_path, settings)
    item = MediaInput(media_id="m_nope", kind=MediaKind.AUDIO, item_index=0)
    outcome = await analyze_item(item, check_id="c_t", services=services, engines=_engines(), ms=_media_settings(), budget_s=5)
    assert outcome.media.label == MediaLabel.NOT_ANALYZED
    assert DegradedFlag.DEEPFAKE_UNAVAILABLE in outcome.degraded
    await services.db.dispose()


async def test_unavailable_engines_degrade(tmp_path, settings: Settings) -> None:
    services = await _services(tmp_path, settings)
    media_id = await _store_media(services, "image", PNG_1X1)
    item = MediaInput(media_id=media_id, kind=MediaKind.IMAGE, item_index=0)
    outcome = await analyze_item(item, check_id="c_t", services=services, engines=_engines(), ms=_media_settings(), budget_s=5)
    assert outcome.media.label == MediaLabel.NOT_ANALYZED
    assert DegradedFlag.DEEPFAKE_UNAVAILABLE in outcome.degraded
    await services.db.dispose()


async def test_analyzer_returns_media_results(tmp_path, settings: Settings) -> None:
    services = await _services(tmp_path, settings)
    media_id = await _store_media(services, "image", PNG_1X1)
    analyzer = DeepfakeAnalyzer(ms=_media_settings(), engines=_engines(ocr=_FakeOcr()))
    inp = AnalysisInput(
        check_id="c_1",
        trace_id="t_1",
        media=(MediaInput(media_id=media_id, kind=MediaKind.IMAGE, item_index=0),),
    )
    ctx = AnalyzerContext(services=services, check_id="c_1", trace_id="t_1")
    out = await analyzer.run(inp, ctx)
    assert out.status == AnalyzerStatus.DONE
    assert out.media[0].media_id == media_id
    assert out.derived_lines and out.derived_lines[0].line_id.startswith("d")
    assert not analyzer.should_run(AnalysisInput(check_id="c_2", trace_id="t_2"))
    await services.db.dispose()


def test_status_line_when_disabled() -> None:
    analyzer = DeepfakeAnalyzer(ms=MediaSettings(enabled=False))
    assert "未启用" in analyzer.status_line()


# ---------------------------------------------------------------------------
# 上传接口
# ---------------------------------------------------------------------------


@pytest.fixture
def media_client(make_client, settings):
    return make_client


@pytest.fixture
def client(media_client, settings):
    return media_client(settings)


def _png_upload(client, kind: str = "image", content: bytes = PNG_1X1, content_type: str = "application/octet-stream"):
    return client.post(
        "/v1/media",
        headers=AUTH,
        files={"file": ("fake.mp3", content, content_type)},
        data={"kind": kind},
    )


def test_upload_detects_png_despite_mp3_extension(client) -> None:
    res = _png_upload(client)
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["kind"] == "image" and body["media_id"].startswith("m_")
    assert body["size_bytes"] == len(PNG_1X1)
    assert iso_utc(utcnow()) <= body["expires_at"]


def test_upload_rejects_bad_kind_and_empty_file(client) -> None:
    bad_kind = client.post("/v1/media", headers=AUTH, files={"file": ("a.png", PNG_1X1, "image/png")}, data={"kind": "gif"})
    assert bad_kind.status_code == 400 and bad_kind.json()["error"]["code"] == "INVALID_INPUT"
    empty = _png_upload(client, content=b"")
    assert empty.status_code == 400


def test_upload_rejects_unknown_bytes_as_unsupported(client) -> None:
    res = _png_upload(client, content=b"this is not an image at all")
    assert res.status_code == 415
    assert res.json()["error"]["code"] == "UNSUPPORTED_MEDIA"


def test_upload_rejects_oversized_image(media_client, make_settings) -> None:
    small = make_settings(limit_image_bytes=64)
    res = _png_upload(media_client(small), content=b"\x89PNG" + b"\x00" * 128)
    assert res.status_code == 413
    assert res.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


def test_upload_unavailable_when_disabled(media_client, make_settings, monkeypatch) -> None:
    monkeypatch.setenv("MEDIA_ENABLED", "0")
    from app.analyzers.deepfake.settings import get_media_settings

    get_media_settings.cache_clear()
    res = _png_upload(media_client(make_settings()))
    get_media_settings.cache_clear()
    assert res.status_code == 503
    assert res.json()["error"]["code"] == "UNAVAILABLE"


def test_upload_requires_multipart(client) -> None:
    res = client.post("/v1/media", headers=AUTH, json={"kind": "image"})
    assert res.status_code == 400


# ---------------------------------------------------------------------------
# 需要 ffmpeg 的测试
# ---------------------------------------------------------------------------


def _ffmpeg() -> str | None:
    from app.analyzers.deepfake.env import find_tools

    return find_tools(Settings(_env_file=None)).ffmpeg


@pytest.mark.skipif(_ffmpeg() is None, reason="需要 ffmpeg")
def test_upload_audio_and_rejects_spoofed_extension(client, tmp_path) -> None:
    import subprocess

    wav = tmp_path / "tone.wav"
    subprocess.run(
        [_ffmpeg(), "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "pcm_s16le", str(wav)],
        check=True,
        capture_output=True,
    )
    data = wav.read_bytes()
    res = client.post(
        "/v1/media", headers=AUTH, files={"file": ("photo.png", data, "image/png")}, data={"kind": "audio"}
    )
    assert res.status_code == 201, res.text
    assert res.json()["kind"] == "audio"
    too_long = client.post(
        "/v1/media", headers=AUTH, files={"file": ("a.wav", data, "audio/wav")}, data={"kind": "video"}
    )
    assert too_long.status_code == 415


@pytest.mark.skipif(_ffmpeg() is None, reason="需要 ffmpeg")
async def test_speech_path_with_fake_engines(tmp_path, settings: Settings) -> None:
    import subprocess

    wav = tmp_path / "tone.wav"
    subprocess.run(
        [_ffmpeg(), "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:a", "pcm_s16le", str(wav)],
        check=True,
        capture_output=True,
    )
    services = await _services(tmp_path, settings)
    media_id = await _store_media(services, "audio", wav.read_bytes())
    item = MediaInput(media_id=media_id, kind=MediaKind.AUDIO, item_index=3)
    engines = _engines(asr=_FakeAsr(), spoof=_FakeSpoof(0.97))
    await asyncio.gather(*(e.get(5) for e in engines.all()))
    outcome = await analyze_item(item, check_id="c_t", services=services, engines=engines, ms=_media_settings(), budget_s=15)
    assert outcome.media.label == MediaLabel.LIKELY_SYNTHETIC
    assert outcome.media.spoof_score is not None and outcome.media.spoof_score >= 0.9
    assert "仅供参考" in (outcome.media.note or "")
    assert outcome.lines and outcome.lines[0].text == "转账五千元到<PHONE>"
    assert outcome.lines[0].line_id == "d3-0"
    await services.db.dispose()
