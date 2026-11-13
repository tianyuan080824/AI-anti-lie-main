"""`POST /v1/media`：上传音频、视频或图片，返回 `media_id`。

- `multipart/form-data`，字段 `file`（二进制）和 `kind`（`audio` / `video` / `image`）。
- 按文件内容判断格式（ffprobe / Pillow），不看扩展名和 Content-Type。
- 大小、时长超过契约上限返回 413，格式认不出来返回 415，其余输入问题返回 400。
- 原文件存 `var/media/<随机名>`（无扩展名），表里只记元数据。

集成时在 `app/main.py` 加一行 `app.include_router(router)`。注意：现有中间件对所有
`/v1/` POST 按 `limit_body_bytes`（1 MB）卡请求体，媒体上传会全部被拒，需要核心会话放行
（见 NOTES.md）。
"""

from __future__ import annotations

import logging
from datetime import timedelta

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.analyzers.deepfake.env import find_tools, has_module
from app.analyzers.deepfake.models import MediaRecord
from app.analyzers.deepfake.probe import MediaRejected, ToolError, check_probe, decode_audio, probe, sniff_image
from app.analyzers.deepfake.settings import MediaSettings, get_media_settings
from app.analyzers.deepfake.storage import (
    PART_SUFFIX,
    add,
    ensure_media_dir,
    file_path,
    lifetime_s,
    new_file_name,
    remove,
)
from app.config import Settings
from app.contract import ErrorCode, MediaKind, MediaUploadResponse
from app.db import iso_utc, utcnow
from app.errors import error_response, trace_id_of
from app.ids import new_media_id
from app.logs import log_event

logger = logging.getLogger("app.analyzers.deepfake")

router = APIRouter()

_KIND_TEXT = {"audio": "音频", "video": "视频", "image": "图片"}


def _error(request: Request, code: ErrorCode, message: str | None = None) -> JSONResponse:
    return error_response(code, trace_id_of(request), message)


def _limit_bytes(settings: Settings, kind: MediaKind) -> int:
    return {
        MediaKind.IMAGE: settings.limit_image_bytes,
        MediaKind.AUDIO: settings.limit_audio_bytes,
        MediaKind.VIDEO: settings.limit_video_bytes,
    }[kind]


def _limit_seconds(settings: Settings, kind: MediaKind) -> float:
    return settings.limit_audio_seconds if kind == MediaKind.AUDIO else settings.limit_video_seconds


@router.post("/v1/media", status_code=201)
async def upload_media(request: Request) -> JSONResponse:
    settings: Settings = request.app.state.settings
    ms = get_media_settings()
    if not ms.enabled:
        return _error(request, ErrorCode.UNAVAILABLE, "媒体分析未启用")
    if not has_module("python_multipart"):
        return _error(request, ErrorCode.UNAVAILABLE, "服务器未安装媒体上传组件（uv sync --extra media）")
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" not in content_type.lower():
        return _error(request, ErrorCode.INVALID_INPUT, "请用 multipart/form-data 上传文件")

    form = await request.form()
    kind_raw = form.get("kind")
    if not isinstance(kind_raw, str) or kind_raw not in _KIND_TEXT:
        return _error(request, ErrorCode.INVALID_INPUT, "请在 kind 字段里填写 audio、video 或 image")
    kind = MediaKind(kind_raw)
    upload = form.get("file")
    if not hasattr(upload, "read"):
        return _error(request, ErrorCode.INVALID_INPUT, "请在 file 字段里附上文件")

    limit = _limit_bytes(settings, kind)
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit + 4096:
        return _error(request, ErrorCode.PAYLOAD_TOO_LARGE, f"{_KIND_TEXT[kind.value]}太大，最多 {limit // 1024 // 1024} MB")

    media_dir = ensure_media_dir(settings)
    name = new_file_name()
    temp = media_dir / (name + PART_SUFFIX)
    size = 0
    too_big = False
    try:
        with temp.open("wb") as fh:
            while chunk := await upload.read(65536):  # type: ignore[union-attr]
                size += len(chunk)
                if size > limit:
                    too_big = True
                    break
                fh.write(chunk)
    except Exception as exc:  # noqa: BLE001 - 落盘失败只记类型
        remove(temp)
        log_event(logger, "media.write_failed", logging.ERROR, error=type(exc).__name__)
        return _error(request, ErrorCode.INTERNAL)
    if too_big:
        remove(temp)
        return _error(request, ErrorCode.PAYLOAD_TOO_LARGE, f"{_KIND_TEXT[kind.value]}太大，最多 {limit // 1024 // 1024} MB")
    if size == 0:
        remove(temp)
        return _error(request, ErrorCode.INVALID_INPUT, "文件是空的")

    try:
        record = await _inspect(settings, ms, kind, temp, name, size)
    except MediaRejected as exc:
        remove(temp)
        code = ErrorCode.PAYLOAD_TOO_LARGE if exc.too_large else ErrorCode.UNSUPPORTED_MEDIA
        return _error(request, code, exc.message)
    except ToolError:
        remove(temp)
        log_event(logger, "media.probe_failed", logging.ERROR, kind=kind.value)
        return _error(request, ErrorCode.INTERNAL, "文件校验失败，请稍后重试")

    final = file_path(settings, name)
    try:
        temp.replace(final)
    except OSError:
        remove(temp)
        return _error(request, ErrorCode.INTERNAL)
    try:
        await add(request.app.state.services.db, record)
    except Exception as exc:  # noqa: BLE001
        remove(final)
        log_event(logger, "media.register_failed", logging.ERROR, error=type(exc).__name__)
        return _error(request, ErrorCode.INTERNAL)

    log_event(logger, "media.uploaded", media_id=record.media_id, kind=kind.value, size=size, fmt=record.format_name)
    body = MediaUploadResponse(
        media_id=record.media_id,
        kind=kind,
        size_bytes=size,
        expires_at=iso_utc(record.expires_at),
    )
    return JSONResponse(status_code=201, content=body.model_dump(mode="json"))


async def _inspect(settings: Settings, ms: MediaSettings, kind: MediaKind, temp, name: str, size: int) -> MediaRecord:
    """按内容校验并构造登记行。不合格抛 MediaRejected，工具出错抛 ToolError。"""
    import asyncio

    now = utcnow()
    expires = now + timedelta(seconds=lifetime_s(settings, ms))
    base = dict(media_id=new_media_id(), kind=kind.value, file_name=name, size_bytes=size, created_at=now, expires_at=expires)
    if kind == MediaKind.IMAGE:
        if not has_module("PIL"):
            raise MediaRejected("服务器未安装图片处理组件（uv sync --extra media）")
        data = await asyncio.to_thread(temp.read_bytes)
        info = await asyncio.to_thread(sniff_image, data)
        return MediaRecord(**base, format_name=info.format_name, has_audio=False)
    tools = find_tools(settings)
    if not tools.ok:
        raise MediaRejected("服务器未安装 ffmpeg，暂时不能接收音频和视频")
    assert tools.ffprobe and tools.ffmpeg
    info = await probe(tools.ffprobe, temp, timeout_s=ms.probe_timeout_s)
    check_probe(info, kind, max_seconds=_limit_seconds(settings, kind))
    if kind == MediaKind.VIDEO and not info.has_audio:
        raise MediaRejected("视频里没有音轨，暂时不能分析")
    # 按真实解码长度再卡一次时长：容器头里的时长可以被伪造
    pcm = await decode_audio(tools.ffmpeg, temp, max_seconds=_limit_seconds(settings, kind) + 1, timeout_s=ms.probe_timeout_s)
    decoded_s = len(pcm) / 2 / 16000
    if decoded_s > _limit_seconds(settings, kind):
        raise MediaRejected(f"{_KIND_TEXT[kind.value]}太长，最多 {_limit_seconds(settings, kind):g} 秒", too_large=True)
    return MediaRecord(
        **base,
        format_name=info.format_name,
        duration_s=round(decoded_s, 2),
        sample_rate=info.sample_rate,
        has_audio=info.has_audio,
    )


__all__ = ["router", "upload_media"]
