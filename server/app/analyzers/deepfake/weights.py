"""权重清单（`data/deepfake/weights.json`）：校验大小和 SHA-256、下载、手动放置说明。

加载权重前一律先校验（torch.load 会执行 pickle，只加载哈希对得上的文件）。
校验结果按（路径、大小、修改时间）缓存在进程内，避免每次都重算 900 多 MB 的哈希。
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

CHUNK = 1024 * 1024


@dataclass(frozen=True)
class WeightFile:
    id: str
    used_by: str
    path: str
    size: int
    sha256: str
    urls: tuple[str, ...] = ()
    headers: dict[str, str] = field(default_factory=dict, hash=False, compare=False)
    source: str = ""
    license: str = ""

    def target(self, models_dir: Path) -> Path:
        return models_dir / self.path


def load_manifest(path: Path) -> list[WeightFile]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    files = []
    for raw in doc["files"]:
        files.append(
            WeightFile(
                id=raw["id"],
                used_by=raw["used_by"],
                path=raw["path"],
                size=int(raw["size"]),
                sha256=raw["sha256"].lower(),
                urls=tuple(raw.get("urls", ())),
                headers=dict(raw.get("headers", {})),
                source=raw.get("source", ""),
                license=raw.get("license", ""),
            )
        )
    return files


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


_verified: dict[tuple[str, int, int], str] = {}
_verified_lock = threading.Lock()


def check(wf: WeightFile, models_dir: Path) -> str | None:
    """返回 None 表示文件完整；否则返回一句中文原因。阻塞调用（大文件要几秒），请放在线程里。"""
    target = wf.target(models_dir)
    if not target.is_file():
        return "文件不存在"
    stat = target.stat()
    if stat.st_size != wf.size:
        return f"大小不对（{stat.st_size} 字节，应为 {wf.size}）"
    key = (str(target), stat.st_size, stat.st_mtime_ns)
    with _verified_lock:
        cached = _verified.get(key)
    if cached is None:
        cached = sha256_of(target)
        with _verified_lock:
            _verified[key] = cached
    if cached != wf.sha256:
        return "SHA-256 对不上（文件损坏或不是这个版本）"
    return None


def require(files: list[WeightFile], models_dir: Path) -> None:
    """全部校验通过才返回，否则抛 `WeightError`。"""
    problems = [f"{wf.path}：{reason}" for wf in files if (reason := check(wf, models_dir))]
    if problems:
        raise WeightError("；".join(problems))


class WeightError(RuntimeError):
    pass


def manual_instructions(wf: WeightFile, models_dir: Path) -> str:
    lines = [
        f"手动下载 {wf.id}：",
        *(f"  地址：{url}" for url in wf.urls),
        f"  放到：{wf.target(models_dir)}",
        f"  大小：{wf.size} 字节",
        f"  SHA-256：{wf.sha256}",
        f"  来源：{wf.source}（{wf.license}）",
        "  放好后执行 uv run python -m app.analyzers.deepfake verify 确认。",
    ]
    return "\n".join(lines)


def download(
    wf: WeightFile,
    models_dir: Path,
    *,
    timeout_s: float = 600.0,
    progress: Callable[[int, int], None] | None = None,
) -> None:
    """依次尝试清单里的地址，写到 `.part`，校验通过后改名。全部失败时抛 `WeightError`。"""
    import httpx

    target = wf.target(models_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    errors: list[str] = []
    for url in wf.urls:
        try:
            with httpx.stream(
                "GET",
                url,
                headers=wf.headers,
                follow_redirects=True,
                timeout=httpx.Timeout(timeout_s, connect=30.0),
            ) as resp:
                resp.raise_for_status()
                digest = hashlib.sha256()
                done = 0
                with part.open("wb") as fh:
                    for chunk in resp.iter_bytes(CHUNK):
                        fh.write(chunk)
                        digest.update(chunk)
                        done += len(chunk)
                        if done > wf.size:
                            raise WeightError("下载的内容比清单里的大")
                        if progress:
                            progress(done, wf.size)
            if done != wf.size or digest.hexdigest() != wf.sha256:
                raise WeightError("下载完成但大小或 SHA-256 对不上")
            part.replace(target)
            return
        except Exception as exc:  # noqa: BLE001 - 换下一个地址
            errors.append(f"{url.split('?')[0]}：{type(exc).__name__}")
            part.unlink(missing_ok=True)
    raise WeightError("；".join(errors) or "清单里没有下载地址")


__all__ = [
    "WeightError",
    "WeightFile",
    "check",
    "download",
    "load_manifest",
    "manual_instructions",
    "require",
    "sha256_of",
]
