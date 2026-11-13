"""格式探测和解码：ffprobe / ffmpeg 子进程（音频、视频），Pillow（图片）。

- 一律按文件内容判断格式，不看扩展名和 Content-Type（存盘的文件名本来就没有扩展名）。
- ffprobe / ffmpeg 只允许读本地文件（`-protocol_whitelist file`），只允许白名单里的容器
  （`-format_whitelist`），防止播放列表之类的格式去读别的文件或联网。
- 子进程有超时；超时或被取消（CancelledError）时先杀掉子进程再往外抛。
"""

from __future__ import annotations

import asyncio
import io
import json
import math
from dataclasses import dataclass
from pathlib import Path

from app.contract import MediaKind

MOV_FAMILY = "mov,mp4,m4a,3gp,3g2,mj2"
MKV_FAMILY = "matroska,webm"
#: ffprobe 报出的 format_name → 允许的类型
AUDIO_FORMATS = frozenset({"wav", "mp3", "aac", "flac", "ogg", "amr", "amrnb", "amrwb", MOV_FAMILY, MKV_FAMILY})
VIDEO_FORMATS = frozenset({MOV_FAMILY, MKV_FAMILY})
#: 传给 -format_whitelist 的分路器名（逗号分隔）
DEMUXER_WHITELIST = "wav,mp3,aac,flac,ogg,amr,amrnb,amrwb,mov,mp4,m4a,3gp,3g2,mj2,matroska,webm"

IMAGE_FORMATS = frozenset({"PNG", "JPEG", "WEBP"})
#: 图片像素上限（解码后约 160 MB），挡住「小文件、超大尺寸」的解压炸弹
MAX_IMAGE_PIXELS = 40_000_000

SAMPLE_RATE = 16_000
_STDERR_LIMIT = 4096


class MediaRejected(Exception):
    """上传的文件不合格。`too_large` 为 True 时对应 413，否则对应 415。message 可以直接给用户看。"""

    def __init__(self, message: str, *, too_large: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.too_large = too_large


class ToolError(Exception):
    """ffmpeg / ffprobe 运行失败（不是文件本身的问题时也会用它）。只带退出码，不带输出正文。"""


@dataclass(frozen=True)
class ProbeInfo:
    format_name: str
    duration_s: float | None
    has_audio: bool
    has_video: bool
    sample_rate: int | None


@dataclass(frozen=True)
class ImageInfo:
    format_name: str
    width: int
    height: int


async def run_tool(args: list[str], *, timeout_s: float, max_stdout: int) -> bytes:
    """运行子进程，返回 stdout。超过 `max_stdout` 字节、超时、非零退出都抛 ToolError。"""
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(timeout_s):
            out, err = await _collect(proc, max_stdout)
    except BaseException:
        if proc.returncode is None:
            proc.kill()
            try:
                await asyncio.shield(proc.wait())
            except BaseException:  # noqa: BLE001 - 进程已收到 kill，退出码拿不到也无妨
                pass
        raise
    if proc.returncode != 0:
        raise ToolError(f"{Path(args[0]).name} exit {proc.returncode}")
    del err
    return out


async def _collect(proc: asyncio.subprocess.Process, max_stdout: int) -> tuple[bytes, bytes]:
    assert proc.stdout is not None and proc.stderr is not None
    stderr_task = asyncio.create_task(proc.stderr.read(_STDERR_LIMIT))
    try:
        chunks: list[bytes] = []
        size = 0
        while chunk := await proc.stdout.read(65536):
            size += len(chunk)
            if size > max_stdout:
                raise ToolError("output too large")
            chunks.append(chunk)
        await proc.wait()
        err = await stderr_task
    finally:
        if not stderr_task.done():
            stderr_task.cancel()
    return b"".join(chunks), err


def _input_args(path: Path) -> list[str]:
    return ["-protocol_whitelist", "file", "-format_whitelist", DEMUXER_WHITELIST, "-i", f"file:{path}"]


async def probe(ffprobe: str, path: Path, *, timeout_s: float) -> ProbeInfo:
    """ffprobe 读容器和流信息。认不出来时抛 MediaRejected（415）。"""
    args = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=format_name,duration:stream=codec_type,sample_rate,duration:stream_disposition=attached_pic",
        "-of",
        "json",
        *_input_args(path),
    ]
    try:
        out = await run_tool(args, timeout_s=timeout_s, max_stdout=256 * 1024)
        doc = json.loads(out.decode("utf-8", "replace"))
    except ToolError as exc:
        raise MediaRejected("无法识别这个文件，请上传常见格式的音频或视频") from exc
    except ValueError as exc:
        raise MediaRejected("无法识别这个文件，请上传常见格式的音频或视频") from exc
    fmt = doc.get("format") or {}
    streams = doc.get("streams") or []
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    video = [
        s
        for s in streams
        if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")
    ]
    duration = _float(fmt.get("duration"))
    if duration is None:
        durations = [d for s in audio + video if (d := _float(s.get("duration"))) is not None]
        duration = max(durations) if durations else None
    rate = _int(audio[0].get("sample_rate")) if audio else None
    return ProbeInfo(
        format_name=str(fmt.get("format_name") or ""),
        duration_s=duration,
        has_audio=bool(audio),
        has_video=bool(video),
        sample_rate=rate,
    )


def check_probe(info: ProbeInfo, kind: MediaKind, *, max_seconds: float) -> None:
    """按类型检查容器、流和时长。不合格抛 MediaRejected。"""
    label = "音频" if kind == MediaKind.AUDIO else "视频"
    allowed = AUDIO_FORMATS if kind == MediaKind.AUDIO else VIDEO_FORMATS
    if info.format_name not in allowed:
        raise MediaRejected(f"不支持这种{label}格式")
    if kind == MediaKind.AUDIO and not info.has_audio:
        raise MediaRejected("文件里没有音频")
    if kind == MediaKind.VIDEO and not info.has_video:
        raise MediaRejected("文件里没有视频画面")
    if info.duration_s is None:
        raise MediaRejected(f"读不出{label}时长，请换一个文件")
    if info.duration_s > max_seconds:
        raise MediaRejected(f"{label}太长，最多 {max_seconds:g} 秒", too_large=True)


async def decode_audio(ffmpeg: str, path: Path, *, max_seconds: float, timeout_s: float) -> bytes:
    """解出第一条音轨，转成 16 kHz 单声道 16 位 PCM（小端）。最多取 `max_seconds` 秒。"""
    args = [
        ffmpeg,
        "-nostdin",
        "-v",
        "error",
        *_input_args(path),
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-ac",
        "1",
        "-ar",
        str(SAMPLE_RATE),
        "-t",
        f"{max_seconds:g}",
        "-f",
        "s16le",
        "pipe:1",
    ]
    limit = int(math.ceil(max_seconds + 1)) * SAMPLE_RATE * 2
    return await run_tool(args, timeout_s=timeout_s, max_stdout=limit)


def sniff_image(data: bytes) -> ImageInfo:
    """用 Pillow 按内容认图片并完整解码一次。不合格抛 MediaRejected。阻塞调用，请放在线程里。"""
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(data)) as img:  # noqa: utf8 - 二进制解码，不是文本文件
            fmt = img.format or ""
            width, height = img.size
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise MediaRejected("无法识别这张图片，请上传 PNG、JPEG 或 WebP 格式") from exc
    if fmt not in IMAGE_FORMATS:
        raise MediaRejected("只支持 PNG、JPEG、WebP 格式的图片")
    if width * height > MAX_IMAGE_PIXELS:
        raise MediaRejected("图片尺寸太大", too_large=True)
    try:
        with Image.open(io.BytesIO(data)) as img:  # noqa: utf8 - 二进制解码，不是文本文件
            img.load()
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise MediaRejected("图片已损坏，无法读取") from exc
    return ImageInfo(format_name=fmt, width=width, height=height)


def load_image_bgr(data: bytes):
    """解码成 OCR 用的 BGR ndarray（按 EXIF 方向转正）。阻塞调用。"""
    import numpy as np
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(data)) as img:  # noqa: utf8 - 二进制解码，不是文本文件
        if img.format not in IMAGE_FORMATS or img.width * img.height > MAX_IMAGE_PIXELS:
            raise MediaRejected("图片格式或尺寸不符合要求")
        img = ImageOps.exif_transpose(img) or img
        rgb = np.asarray(img.convert("RGB"))
    return np.ascontiguousarray(rgb[:, :, ::-1])


def _float(value: object) -> float | None:
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


__all__ = [
    "AUDIO_FORMATS",
    "ImageInfo",
    "MediaRejected",
    "ProbeInfo",
    "SAMPLE_RATE",
    "ToolError",
    "VIDEO_FORMATS",
    "check_probe",
    "decode_audio",
    "load_image_bgr",
    "probe",
    "run_tool",
    "sniff_image",
]
