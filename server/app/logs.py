"""结构化日志。

- 控制台：一行一条、人眼好读（`时间 级别 事件 k=v ...`）。
- 文件：`server/var/logs/server.log`，JSON Lines，UTF-8，按大小轮转。
- 只记 ID、长度、耗时、所用模型和提示词版本；不记正文、不记密钥。

用法：`log_event(logger, "check.created", trace_id=..., lines=12)`。
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from datetime import datetime, timezone
from typing import Any

from app.config import Settings

_MARK = "_antifraud_handler"


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO, **fields: Any) -> None:
    logger.log(level, event, extra={"fields": fields})


def _fields(record: logging.LogRecord) -> dict[str, Any]:
    fields = getattr(record, "fields", None)
    return fields if isinstance(fields, dict) else {}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        payload.update(_fields(record))
        if record.exc_info and record.exc_info[0] is not None:
            payload["error"] = record.exc_info[0].__name__
        return json.dumps(payload, ensure_ascii=False, default=str)


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        parts = [ts, f"{record.levelname:<7}", record.getMessage()]
        for key, value in _fields(record).items():
            if key == "stack":
                continue
            parts.append(f"{key}={value}")
        return " ".join(parts)


def setup_logging(settings: Settings, *, console: bool = True, file: bool = True) -> None:
    """配置根日志器。重复调用是安全的（会先移除上次装上的处理器）。"""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _MARK, False):
            root.removeHandler(handler)
            handler.close()
    root.setLevel(settings.log_level.upper())

    if console:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(ConsoleFormatter())
        setattr(stream, _MARK, True)
        root.addHandler(stream)

    if file:
        settings.log_dir.mkdir(parents=True, exist_ok=True)
        rotating = logging.handlers.RotatingFileHandler(
            settings.log_dir / "server.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        rotating.setFormatter(JsonFormatter())
        setattr(rotating, _MARK, True)
        root.addHandler(rotating)
