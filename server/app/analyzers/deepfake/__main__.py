"""媒体分析的命令行入口（在 `server/` 目录下执行）：

    uv run python -m app.analyzers.deepfake check        # 自检：依赖、ffmpeg、权重
    uv run python -m app.analyzers.deepfake download     # 按清单下载并校验权重
    uv run python -m app.analyzers.deepfake verify       # 只校验已放置的权重
    uv run python -m app.analyzers.deepfake sweep        # 清理过期的媒体文件和登记

下载失败时打印手动放置说明（地址、目标路径、大小、SHA-256）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.analyzers.deepfake import create
from app.analyzers.deepfake.settings import get_media_settings
from app.analyzers.deepfake.storage import sweep
from app.analyzers.deepfake.weights import WeightError, check, download, load_manifest, manual_instructions
from app.config import get_settings
from app.services import open_services


def _force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


async def _check() -> int:
    analyzer = create()
    async with open_services() as services:
        try:
            items = await analyzer.selfcheck(services)
        finally:
            await analyzer.shutdown()
    ok = True
    for item in items:
        ok &= item.ok
        mark = "通过" if item.ok else "失败"
        print(f"[{mark}] {item.name}：{item.detail}")
        if not item.ok and item.hint:
            print(f"       怎么办：{item.hint}")
    return 0 if ok else 1


def _verify() -> int:
    ms = get_media_settings()
    files = load_manifest(ms.manifest_path)
    failed = 0
    for wf in files:
        reason = check(wf, ms.models_dir)
        if reason is None:
            print(f"[通过] {wf.path}")
        else:
            failed += 1
            print(f"[失败] {wf.path}：{reason}")
            print(manual_instructions(wf, ms.models_dir))
    return 0 if not failed else 1


def _download() -> int:
    ms = get_media_settings()
    files = load_manifest(ms.manifest_path)
    failed = 0
    for wf in files:
        if check(wf, ms.models_dir) is None:
            print(f"[已存在] {wf.path}")
            continue
        print(f"[下载] {wf.id}（{wf.size / 1024 / 1024:.1f} MB）")

        def progress(done: int, total: int, *, _id: str = wf.id) -> None:
            if total and done == total:
                print(f"       {_id} 下载完成，校验中")

        try:
            download(wf, ms.models_dir, progress=progress)
        except WeightError as exc:
            failed += 1
            print(f"[失败] {wf.id}：{exc}")
            print(manual_instructions(wf, ms.models_dir))
        else:
            print(f"[通过] {wf.path}")
    return 0 if not failed else 1


async def _sweep() -> int:
    ms = get_media_settings()
    async with open_services() as services:
        result = await sweep(services.db, services.settings, ms)
    print(f"删除过期登记 {result.rows} 条、文件 {result.files} 个。")
    return 0


def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    parser = argparse.ArgumentParser(prog="python -m app.analyzers.deepfake")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="自检：依赖、ffmpeg、权重")
    sub.add_parser("download", help="按清单下载并校验权重")
    sub.add_parser("verify", help="只校验已放置的权重")
    sub.add_parser("sweep", help="清理过期的媒体文件和登记")
    args = parser.parse_args(argv)
    if args.command == "check":
        return asyncio.run(_check())
    if args.command == "verify":
        return _verify()
    if args.command == "download":
        return _download()
    if args.command == "sweep":
        return asyncio.run(_sweep())
    return 2


if __name__ == "__main__":
    sys.exit(main())
