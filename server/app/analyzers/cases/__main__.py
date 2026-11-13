"""相似案例命令行：在 `server/` 目录下执行 `uv run python -m app.analyzers.cases <子命令>`。

- `status`：打印数据库位置、案例条数、向量是否和当前配置一致（不联网）。
- `import`：从 `data/cases/*.jsonl` 导入并计算向量，可重复执行。导入后重启服务生效。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from app.analyzers.cases.embedder import create_embedder
from app.analyzers.cases.importer import CASES_DIR, find_files, import_cases
from app.analyzers.cases.index import load_index
from app.config import get_settings
from app.services import open_services


def _force_utf8_console() -> None:
    """中文 Windows 控制台默认 GBK，强制 UTF-8 避免乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


async def _status() -> int:
    settings = get_settings()
    async with open_services(settings) as services:
        print(f"数据库：{settings.database_path}")
        idx = await load_index(services.db, model=settings.embedding_model, dim=settings.embedding_dim)
        print(f"案例：{idx.total} 条（合成示例 {idx.synthetic} 条），可检索 {idx.usable} 条")
        print(f"向量配置：{settings.embedding_model} / {settings.embedding_dim} 维")
        if idx.mismatched or idx.missing:
            print(f"需要重新导入：向量型号或维度不符 {idx.mismatched} 条，缺向量 {idx.missing} 条")
        if idx.total == 0:
            print("案例库为空：把案例放进 server/data/cases/*.jsonl 后运行 uv run python -m app.analyzers.cases import")
    return 0


async def _import(directory: Path, prune: bool, dry_run: bool) -> int:
    settings = get_settings()
    files = find_files(directory)
    if not files:
        print(f"没有找到案例文件：{directory}/*.jsonl")
        return 1
    embedder = create_embedder(settings)
    try:
        async with open_services(settings) as services:
            print(f"数据库：{settings.database_path}")
            print(f"向量：{embedder.model} / {embedder.dim} 维，{settings.embedding_base_url}")
            report = await import_cases(services.db, embedder, files, prune=prune, dry_run=dry_run)
    finally:
        await embedder.aclose()
    for line in report.lines():
        print(line)
    if not dry_run and not report.missing_key:
        print("正在运行的服务不会自动看到新案例：请重启服务。")
    return 0 if report.ok else 1


def main(argv: list[str] | None = None) -> int:
    _force_utf8_console()
    parser = argparse.ArgumentParser(prog="python -m app.analyzers.cases", description="相似案例库")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="打印案例库状态（不联网）")
    p_import = sub.add_parser("import", help="从 data/cases/*.jsonl 导入并计算向量（可重复执行）")
    p_import.add_argument("--dir", type=Path, default=CASES_DIR, help="案例 jsonl 所在目录，默认 server/data/cases")
    p_import.add_argument("--prune", action="store_true", help="删除库里有、jsonl 里已经没有的案例")
    p_import.add_argument("--dry-run", action="store_true", help="只校验和统计，不写库、不调用向量接口")
    args = parser.parse_args(argv)
    if args.command == "status":
        return asyncio.run(_status())
    if args.command == "import":
        return asyncio.run(_import(args.dir, args.prune, args.dry_run))
    return 2


if __name__ == "__main__":
    sys.exit(main())
