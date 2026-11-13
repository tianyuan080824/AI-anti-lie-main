"""三个模型的真实加载和推理。只在后台加载线程和推理线程里调用；本模块顶层不导入大依赖。

日志：FunASR 用根日志器打 info（例如「decoding, utt: ...」），这里给根日志器加一个过滤器，
丢掉来自 funasr / modelscope / rapidocr 源码的 WARNING 以下记录，避免任何正文相关内容进日志。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

import numpy as np

from app.analyzers.deepfake.env import configure_cache_dirs
from app.analyzers.deepfake.text import SAMPLE_RATE, OcrBox
from app.analyzers.deepfake.weights import WeightFile, require

AASIST_CONFIG: dict[str, Any] = {
    "architecture": "AASIST",
    "nb_samp": 64600,
    "first_conv": 128,
    "filts": [70, [1, 32], [32, 32], [32, 24], [24, 24]],
    "gat_dims": [24, 32],
    "pool_ratios": [0.4, 0.5, 0.7, 0.5],
    "temperatures": [2.0, 2.0, 100.0, 100.0],
}

_NOISY = ("/funasr/", "/modelscope/", "/rapidocr/", "\\funasr\\", "\\modelscope\\", "\\rapidocr\\")
_filter_lock = threading.Lock()
_filter_installed = False


class _QuietThirdParty(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True
        return not any(part in (record.pathname or "") for part in _NOISY)


def quiet_third_party_logs() -> None:
    global _filter_installed
    with _filter_lock:
        if _filter_installed:
            return
        logging.getLogger().addFilter(_QuietThirdParty())
        for name in ("funasr", "modelscope", "RapidOCR", "rapidocr"):
            logging.getLogger(name).setLevel(logging.WARNING)
        _filter_installed = True


# ---------------------------------------------------------------------------
# OCR（RapidOCR + onnxruntime，模型随 wheel 附带）
# ---------------------------------------------------------------------------


class RapidOcrEngine:
    def __init__(self, threads: int) -> None:
        quiet_third_party_logs()
        from rapidocr import RapidOCR

        self._ocr = RapidOCR(
            params={
                "Global.log_level": "error",
                "EngineConfig.onnxruntime.intra_op_num_threads": threads,
                "EngineConfig.onnxruntime.inter_op_num_threads": 1,
            }
        )
        self._lock = threading.Lock()
        # 先跑一张空白图，让 onnxruntime 建好会话（否则第一次请求多花约 1 秒）
        self.recognize(np.full((64, 256, 3), 255, dtype=np.uint8))

    def recognize(self, bgr: np.ndarray) -> list[OcrBox]:
        with self._lock:
            result = self._ocr(bgr)
        txts = result.txts or ()
        scores = result.scores or ()
        boxes = result.boxes if result.boxes is not None else ()
        out = []
        for text, score, box in zip(txts, scores, boxes):
            pts = tuple((float(p[0]), float(p[1])) for p in box)
            out.append(OcrBox(text=str(text), score=float(score), box=pts))
        return out


# ---------------------------------------------------------------------------
# 语音转写（FunASR + SenseVoiceSmall，本地目录加载，不访问模型仓库）
# ---------------------------------------------------------------------------


class SenseVoiceEngine:
    def __init__(self, models_dir: Path, files: list[WeightFile], threads: int) -> None:
        require(files, models_dir)
        configure_cache_dirs(models_dir)
        quiet_third_party_logs()
        import torch

        torch.set_num_threads(threads)
        from funasr import AutoModel
        from funasr.utils.postprocess_utils import rich_transcription_postprocess

        self._post = rich_transcription_postprocess
        self._model = AutoModel(
            model=str(models_dir / "sensevoice-small"),
            device="cpu",
            disable_update=True,
            disable_pbar=True,
            disable_log=True,
            log_level="ERROR",
            ncpu=threads,
        )
        self._lock = threading.Lock()
        self.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32))

    def transcribe(self, pcm: np.ndarray) -> str:
        if pcm.size == 0:
            return ""
        with self._lock:
            res = self._model.generate(input=pcm, cache={}, language="zh", use_itn=True, disable_pbar=True)
        if not res:
            return ""
        return self._post(str(res[0].get("text", "")))


# ---------------------------------------------------------------------------
# 合成语音检测（AASIST-L，CPU）
# ---------------------------------------------------------------------------


class AasistEngine:
    def __init__(self, models_dir: Path, files: list[WeightFile], threads: int) -> None:
        require(files, models_dir)
        configure_cache_dirs(models_dir)
        import torch

        torch.set_num_threads(threads)
        from app.analyzers.deepfake.aasist_model import Model

        model = Model(AASIST_CONFIG)
        state = torch.load(models_dir / files[0].path, map_location="cpu", weights_only=True)
        model.load_state_dict(state)
        model.eval()
        self._torch = torch
        self._model = model
        self._lock = threading.Lock()

    def scores(self, segments: np.ndarray) -> list[float]:
        """每个片段的合成概率（softmax 第 0 类：spoof）。"""
        if len(segments) == 0:
            return []
        torch = self._torch
        with self._lock, torch.inference_mode():
            _, logits = self._model(torch.from_numpy(np.ascontiguousarray(segments, dtype=np.float32)))
            probs = torch.softmax(logits, dim=1)[:, 0]
        return [float(p) for p in probs]


__all__ = ["AASIST_CONFIG", "AasistEngine", "RapidOcrEngine", "SenseVoiceEngine", "quiet_third_party_logs"]
