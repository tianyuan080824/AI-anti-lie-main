"""启动命令：在 `server/` 目录下执行 `uv run python -m app`。

启动前在控制台打印一段状态：监听地址、是否模拟模式、规则和案例的数量、主备模型名称、各分析器状态。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from app import __version__
from app.netinfo import IPV6_ANY, IPV6_ONLY_NOTE, base_url, lan_lines, listens_on_all


def _force_utf8_console() -> None:
    """中文 Windows 控制台默认 GBK：强制 UTF-8，避免中文日志乱码或 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _mask(value: str | None) -> str:
    return "已配置" if value else "未配置"


def _skip_model_preload(pipeline) -> None:
    """启动状态只打印引擎是否可用，不为它加载模型（真正的服务启动后会再加载一次）。"""
    from app.analyzers.deepfake.analyzer import DeepfakeAnalyzer

    for analyzer in pipeline.analyzers:
        if isinstance(analyzer, DeepfakeAnalyzer):
            analyzer.ms = analyzer.ms.model_copy(update={"preload": False})


async def _collect_status(settings) -> list[str]:
    """打印用的状态（打开数据库建表、加载分析器，但不联网）。"""
    from app.pipeline.runner import Pipeline
    from app.redact import IMPLEMENTED as REDACT_IMPLEMENTED
    from app.rules import load_rules_meta
    from app.services import open_services

    rules = load_rules_meta()
    lines = [
        f"一键鉴诈服务 v{__version__}",
        f"  监听地址    {base_url(settings.host, settings.port)}",
        f"  模拟模式    {'开启（MOCK_MODE=1，报告是写死的示例，路演时不要开）' if settings.mock_mode else '关闭'}",
        f"  访问令牌    {'默认值 dev-token（只用于开发）' if settings.app_token == 'dev-token' else '已配置（不是默认值）'}",
        f"  规则        {rules.count} 条，版本 {rules.version}" + (f"（{rules.path.name} 未找到）" if rules.error == 'missing' else ""),
        f"  主模型      {settings.llm_primary_model}（密钥{_mask(settings.llm_primary_api_key)}）",
        f"  备用模型    {settings.llm_backup_model}（密钥{_mask(settings.llm_backup_api_key)}）",
        f"  数据库      {settings.database_path}",
    ]
    if not REDACT_IMPLEMENTED:
        lines.append("  服务端脱敏  尚未实现（第二阶段）")
    async with open_services(settings) as services:
        if services.table_errors:
            lines.append(f"  建表失败    {', '.join(services.table_errors)}")
        if not settings.mock_mode:
            pipeline = Pipeline(services)
            _skip_model_preload(pipeline)
            await pipeline.startup()
            for name, text in pipeline.status_lines().items():
                lines.append(f"  {name.value:<10}  {text}")
            await pipeline.shutdown()
    return lines


def main(argv: list[str] | None = None) -> int:
    _force_utf8_console()
    parser = argparse.ArgumentParser(prog="python -m app", description="启动一键鉴诈服务")
    parser.add_argument("--host", help="监听地址，默认 127.0.0.1（也可在 .env 里设 HOST）")
    parser.add_argument("--port", type=int, help="端口，默认 8000")
    parser.add_argument("--mock", action="store_true", help="以模拟模式启动（等同 MOCK_MODE=1）")
    args = parser.parse_args(argv)

    if args.host:
        os.environ["HOST"] = args.host
    if args.port:
        os.environ["PORT"] = str(args.port)
    if args.mock:
        os.environ["MOCK_MODE"] = "1"

    import uvicorn

    from app.config import get_settings
    from app.logs import setup_logging
    from app.main import create_app

    settings = get_settings()
    setup_logging(settings)

    for line in asyncio.run(_collect_status(settings)):
        print(line, flush=True)
    if settings.host == IPV6_ANY:
        print(f"  提醒        {IPV6_ONLY_NOTE}", flush=True)
    if listens_on_all(settings.host):
        for line in lan_lines(settings.port):
            print(line, flush=True)
    print("  自检        上台前先跑 uv run python -m app.selfcheck", flush=True)
    print("按 Ctrl+C 停止服务", flush=True)

    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_config=None,  # 沿用 setup_logging 的配置
        access_log=False,  # 访问日志由中间件记录（带 trace_id）
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
