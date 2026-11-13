"""三个推理引擎（OCR、语音转写、合成语音检测）的加载状态。

- 每个引擎在独立的守护线程里加载（导入 torch / FunASR 冷启动要几十秒），不占用事件循环，
  也不会让服务退出时等它加载完。
- 调用方用 `await engine.get(timeout)` 取已加载的实例；在给定时间内没加载好就返回 None，
  由调用方降级，不会把这次鉴定卡住。
- 加载失败后过 `RETRY_AFTER_S` 秒允许重试（例如运行中补下载了权重）。
- 测试里直接传 `Engine("ocr", loader=lambda: 假实现)`，不导入任何大依赖。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from app.logs import log_event

logger = logging.getLogger("app.analyzers.deepfake")

RETRY_AFTER_S = 60.0


class Engine:
    def __init__(self, name: str, loader: Callable[[], Any], *, unavailable: str | None = None) -> None:
        self.name = name
        self._loader = loader
        #: 缺依赖、缺权重、缺 ffmpeg 等：一句中文原因。为 None 表示可以加载。
        self.unavailable = unavailable
        self._lock = threading.Lock()
        self._future: concurrent.futures.Future | None = None
        self._failed_at: float | None = None
        self.error: str | None = None
        self.load_s: float | None = None

    @property
    def state(self) -> str:
        """unavailable / idle / loading / ready / failed"""
        if self.unavailable:
            return "unavailable"
        fut = self._future
        if fut is None:
            return "idle"
        if not fut.done():
            return "loading"
        return "failed" if fut.exception() is not None else "ready"

    def start(self) -> concurrent.futures.Future | None:
        """开始在后台加载（已在加载或已加载好时直接返回同一个 future）。"""
        if self.unavailable:
            return None
        with self._lock:
            fut = self._future
            if fut is not None and fut.done() and fut.exception() is not None:
                if self._failed_at is not None and time.monotonic() - self._failed_at >= RETRY_AFTER_S:
                    fut = None
            if fut is None:
                fut = concurrent.futures.Future()
                self._future = fut
                threading.Thread(target=self._load, args=(fut,), name=f"media-load-{self.name}", daemon=True).start()
            return fut

    def _load(self, fut: concurrent.futures.Future) -> None:
        if not fut.set_running_or_notify_cancel():
            return
        t0 = time.perf_counter()
        try:
            obj = self._loader()
        except BaseException as exc:  # noqa: BLE001 - 加载失败只降级，记下类型
            self.error = type(exc).__name__
            self._failed_at = time.monotonic()
            log_event(logger, "media.engine_failed", logging.ERROR, engine=self.name, error=self.error)
            fut.set_exception(exc)
            return
        self.load_s = time.perf_counter() - t0
        self.error = None
        log_event(logger, "media.engine_ready", engine=self.name, load_ms=int(self.load_s * 1000))
        fut.set_result(obj)

    def ready_instance(self) -> Any | None:
        fut = self._future
        if fut is not None and fut.done() and fut.exception() is None:
            return fut.result()
        return None

    async def get(self, timeout: float | None) -> Any | None:
        """取实例；`timeout` 秒内没加载好或加载失败返回 None。不会取消后台加载。"""
        fut = self.start()
        if fut is None:
            return None
        if not fut.done():
            if timeout is not None and timeout <= 0:
                return None
            try:
                await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(fut)), timeout)
            except TimeoutError:
                return None
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 失败已在 _load 里记日志
                return None
        if fut.exception() is not None:
            return None
        return fut.result()

    def describe(self) -> str:
        state = self.state
        if state == "unavailable":
            return f"不可用（{self.unavailable}）"
        if state == "ready":
            return "就绪" + (f"（加载 {self.load_s:.1f} 秒）" if self.load_s is not None else "")
        if state == "failed":
            return f"加载失败（{self.error}）"
        return {"idle": "未加载（第一次用到时加载）", "loading": "加载中"}[state]


@dataclass
class Engines:
    ocr: Engine
    asr: Engine
    spoof: Engine

    def all(self) -> list[Engine]:
        return [self.ocr, self.asr, self.spoof]

    def start_all(self) -> None:
        for engine in self.all():
            engine.start()


__all__ = ["Engine", "Engines", "RETRY_AFTER_S"]
