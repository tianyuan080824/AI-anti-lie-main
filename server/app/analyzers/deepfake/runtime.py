"""引擎的创建和加载等待。

- 缺依赖、缺权重时引擎标记为不可用（`unavailable`），不会抛异常，服务照常启动。
- 权重是否齐全在后台加载线程里用 SHA-256 校验（`weights.require`），不在事件循环里算 900 多 MB 的哈希。
- 加载大依赖之前先把各库的缓存目录指到 models_dir 下（`configure_cache_dirs`），防止写到 ~/.cache。
"""

from __future__ import annotations

import concurrent.futures
import logging
from pathlib import Path

from app.analyzers.deepfake.engines import Engine, Engines
from app.analyzers.deepfake.env import configure_cache_dirs, has_module
from app.analyzers.deepfake.settings import MediaSettings
from app.analyzers.deepfake.weights import WeightFile, load_manifest
from app.logs import log_event

logger = logging.getLogger("app.analyzers.deepfake")

#: 退出时最多等正在加载的引擎这么久，避免守护线程在 import 中途被 SIGTERM 打断
SHUTDOWN_WAIT_S = 5.0


def manifest_files(ms: MediaSettings, used_by: str) -> list[WeightFile]:
    """清单里某个能力用到的文件。清单读不出来时返回空列表（加载时会按缺权重降级）。"""
    try:
        files = load_manifest(ms.manifest_path)
    except (OSError, ValueError, KeyError):
        return []
    return [wf for wf in files if wf.used_by == used_by]


def create_engines(ms: MediaSettings) -> Engines:
    """创建三个引擎。不可用时记录中文原因，不抛异常。"""
    configure_cache_dirs(ms.models_dir)
    return Engines(
        ocr=_ocr_engine(ms.threads),
        asr=_weight_engine("asr", "funasr", "asr", ms),
        spoof=_weight_engine("spoof", "torch", "spoof", ms),
    )


def _ocr_engine(threads: int) -> Engine:
    if not has_module("rapidocr"):
        return Engine("ocr", lambda: None, unavailable="缺少 rapidocr 依赖（uv sync --extra media）")
    if not has_module("onnxruntime"):
        return Engine("ocr", lambda: None, unavailable="缺少 onnxruntime 依赖（uv sync --extra media）")

    def load() -> object:
        from app.analyzers.deepfake.inference import RapidOcrEngine

        return RapidOcrEngine(threads=threads)

    return Engine("ocr", load)


def _weight_engine(name: str, module: str, used_by: str, ms: MediaSettings) -> Engine:
    if not has_module(module):
        return Engine(name, lambda: None, unavailable=f"缺少 {module} 依赖（uv sync --extra media）")
    if not has_module("torch"):
        return Engine(name, lambda: None, unavailable="缺少 torch 依赖（uv sync --extra media）")
    files = manifest_files(ms, used_by)
    if not files:
        return Engine(name, lambda: None, unavailable=f"权重清单里没有 {used_by} 的文件")

    def load() -> object:
        from app.analyzers.deepfake.inference import AasistEngine, SenseVoiceEngine

        cls = SenseVoiceEngine if name == "asr" else AasistEngine
        return cls(ms.models_dir, files, ms.threads)

    return Engine(name, load)


def preload(engines: Engines) -> None:
    """启动后台加载并记下每个引擎的初始状态。"""
    for engine in engines.all():
        log_event(logger, "media.engine_init", engine=engine.name, state=engine.describe())
    engines.start_all()


def await_loading(engines: Engines, *, timeout_s: float = SHUTDOWN_WAIT_S) -> None:
    """等正在加载的引擎结束（成功或失败都算）。阻塞调用，请放在线程里。"""
    futures = [engine._future for engine in engines.all() if engine._future is not None and not engine._future.done()]  # noqa: SLF001 - 生命周期管理，需要看内部加载状态
    if not futures:
        return
    log_event(logger, "media.await_engines", count=len(futures))
    concurrent.futures.wait(futures, timeout=timeout_s)


def engine_status(engines: Engines) -> str:
    """`status_line()` 用的一句话。"""
    parts = [f"{engine.name} {engine.describe()}" for engine in engines.all()]
    return "；".join(parts)


def models_dir_of(ms: MediaSettings) -> Path:
    return ms.models_dir


__all__ = [
    "SHUTDOWN_WAIT_S",
    "await_loading",
    "create_engines",
    "engine_status",
    "manifest_files",
    "preload",
]
