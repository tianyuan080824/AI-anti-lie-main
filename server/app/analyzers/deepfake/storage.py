"""媒体文件的落盘、认领和过期清理。

- 文件放在 `var/media/<32 位随机十六进制>`，没有扩展名；上传中的临时文件带 `.part` 后缀。
- 有效期 = min(MEDIA_MAX_AGE_S, 3600) − 2 × MEDIA_SWEEP_INTERVAL_S（至少 60 秒），
  清理任务每隔 MEDIA_SWEEP_INTERVAL_S 跑一次，所以原文件最迟不超过 1 小时一定被删除。
- 分析时读进内存后立即删除原文件；表里的行留到过期，用来区分「已经分析过」和「不存在」。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, select, update

from app.analyzers.deepfake.models import MediaRecord
from app.analyzers.deepfake.settings import MediaSettings
from app.config import Settings
from app.db import Database, utcnow
from app.logs import log_event

logger = logging.getLogger("app.analyzers.deepfake")

PART_SUFFIX = ".part"
_NAME = re.compile(r"^[0-9a-f]{32}$")


def media_dir(settings: Settings) -> Path:
    return settings.var_dir / "media"


def ensure_media_dir(settings: Settings) -> Path:
    path = media_dir(settings)
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def new_file_name() -> str:
    return secrets.token_hex(16)


def file_path(settings: Settings, file_name: str) -> Path:
    if not _NAME.match(file_name):
        raise ValueError("invalid media file name")
    return media_dir(settings) / file_name


def lifetime_s(settings: Settings, ms: MediaSettings) -> float:
    return max(60.0, min(float(settings.media_max_age_s), 3600.0) - 2 * float(ms.sweep_interval_s))


def remove(path: Path) -> bool:
    """删除文件；不存在算成功。删不掉（例如 Windows 上被占用）时返回 False，留给过期清理。"""
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return True
    except OSError:
        log_event(logger, "media.delete_failed", logging.WARNING, file=path.name)
        return False


async def add(db: Database, record: MediaRecord) -> None:
    async with db.session() as session:
        session.add(record)
        await session.commit()


@dataclass(frozen=True)
class Claim:
    record: MediaRecord
    #: 这次鉴定是否拿到了这个媒体（别的鉴定先用过时为 False）
    owned: bool


async def claim(db: Database, media_id: str, check_id: str) -> Claim | None:
    """认领一个未过期的媒体。不存在或已过期返回 None。同一个媒体只能被一次鉴定使用。"""
    now = utcnow()
    async with db.session() as session:
        await session.execute(
            update(MediaRecord)
            .where(
                MediaRecord.media_id == media_id,
                MediaRecord.claimed_by.is_(None),
                MediaRecord.expires_at > now,
            )
            .values(claimed_by=check_id, claimed_at=now)
        )
        await session.commit()
        record = await session.get(MediaRecord, media_id)
    if record is None or record.expires_at <= now:
        return None
    return Claim(record=record, owned=record.claimed_by == check_id)


@dataclass(frozen=True)
class SweepResult:
    rows: int
    files: int


async def sweep(db: Database, settings: Settings, ms: MediaSettings) -> SweepResult:
    """删除过期的行和文件，以及没有登记、超过有效期的残留文件（含中断的上传）。"""
    now = utcnow()
    async with db.session() as session:
        expired = (
            await session.execute(select(MediaRecord.media_id, MediaRecord.file_name).where(MediaRecord.expires_at <= now))
        ).all()
        ids = [row.media_id for row in expired]
        if ids:
            await session.execute(delete(MediaRecord).where(MediaRecord.media_id.in_(ids)))
            await session.commit()
        known = set((await session.scalars(select(MediaRecord.file_name))).all())
    names = [row.file_name for row in expired]
    files = await asyncio.to_thread(_sweep_files, media_dir(settings), names, known, lifetime_s(settings, ms))
    if ids or files:
        log_event(logger, "media.swept", rows=len(ids), files=files)
    return SweepResult(rows=len(ids), files=files)


def _sweep_files(directory: Path, expired: list[str], known: set[str], max_age_s: float) -> int:
    removed = 0
    for name in expired:
        if _NAME.match(name) and (directory / name).exists() and remove(directory / name):
            removed += 1
    if not directory.is_dir():
        return removed
    cutoff = time.time() - max_age_s
    for path in directory.iterdir():
        if not path.is_file() or path.name in known:
            continue
        try:
            old = path.stat().st_mtime < cutoff
        except OSError:
            continue
        if old and remove(path):
            removed += 1
    return removed


__all__ = [
    "Claim",
    "PART_SUFFIX",
    "SweepResult",
    "add",
    "claim",
    "ensure_media_dir",
    "file_path",
    "lifetime_s",
    "media_dir",
    "new_file_name",
    "remove",
    "sweep",
]
