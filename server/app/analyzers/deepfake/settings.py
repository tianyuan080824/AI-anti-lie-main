"""媒体分析自己的配置项（前缀 `MEDIA_`），第三阶段集成时并进 `app/config.py`。

大小、时长上限、`FFMPEG_PATH`、`MEDIA_MAX_AGE_S`、`TIMEOUT_DEEPFAKE_S` 仍用公共 `Settings` 里的字段。
注意公共字段 `media_max_age_s` 的环境变量就是 `MEDIA_MAX_AGE_S`，这里不能再定义 `max_age_s`。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.config import ENV_FILE
from app.paths import DATA_DIR, SERVER_DIR


class MediaSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MEDIA_",
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    enabled: bool = True
    """总开关。关掉后上传接口返回 UNAVAILABLE，鉴定里的媒体条目一律 not_analyzed。"""
    preload: bool = True
    """启动时加载模型（每个进程只加载一次，约 10–40 秒）。关掉则在第一次用到时加载。"""
    models_dir: Path = SERVER_DIR / "models"
    """权重目录。各库的缓存目录也指到这里，避免写到 ~/.cache。"""
    manifest_path: Path = DATA_DIR / "deepfake" / "weights.json"

    preprocess_budget_s: float = 6.0
    """预处理（取文件、转码、转写 / OCR）的时间预算：整单 20 − 汇总 2 − 话术 12。"""
    threads: int = 4
    """torch / onnxruntime 的 CPU 线程数。"""
    probe_timeout_s: float = 10.0
    """上传时 ffprobe / ffmpeg 校验的超时。"""

    ocr_min_score: float = 0.5
    """OCR 行置信度低于它的丢弃。"""
    asr_chunk_s: float = 25.0
    """长音频按静音点切成不超过这么长的片段再转写。"""

    synthetic_threshold: float = 0.9
    """片段平均合成分 ≥ 它且至少一半片段 ≥ 0.5 时才判 likely_synthetic。"""
    genuine_threshold: float = 0.1
    """片段平均合成分 ≤ 它且没有片段 ≥ 0.5 时才判 likely_genuine。其余一律 uncertain。"""
    min_speech_s: float = 1.0
    """有效语音短于它时不打分，返回 uncertain。"""

    sweep_interval_s: float = 60.0
    """清理过期媒体文件的间隔。媒体有效期 = min(MEDIA_MAX_AGE_S, 3600) − 2 × 间隔，保证 1 小时内删除。"""
    prepared_ttl_s: float = 120.0
    """预处理结果（内存里的波形和派生行）等 run() 来取的最长时间。"""
    max_derived_lines: int = 100
    """一次鉴定最多产出多少条派生行。"""


@lru_cache
def get_media_settings() -> MediaSettings:
    return MediaSettings()


__all__ = ["MediaSettings", "get_media_settings"]
