"""启动前自检：在 `server/` 目录下执行 `uv run python -m app.selfcheck`（Mac 和 Windows 同一条命令）。

逐项打印「通过 / 失败 / 未配置 / 提示」，失败时给出该怎么办。全部通过（「提示」不算）时退出码 0，否则 1。

- 公共项（本文件）：配置文件、模拟模式、访问令牌、数据库可写、ffmpeg（P1，找不到只提示）、监听地址；
- 各分析器的项来自它们的 `selfcheck()` 钩子：话术（规则条数、提示词、主备模型各发一次最小请求）、
  案例（案例条数、向量接口）、链接（数据文件、RDAP）、媒体（由媒体那一路自己提供）。

大模型的自检结果写进数据库（`app_state` 表），服务启动时读回健康检查的 `llm` 字段（契约 4.6：
「反映最近一次真实调用或启动自检的结果」）。没有密钥时显示「未配置」，`llm` 保持 `unknown`。
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import delete

from app.analyzers import load_analyzers
from app.analyzers.base import Analyzer, SelfCheckItem
from app.config import ENV_FILE, Settings, get_settings
from app.db import Database, utcnow
from app.db.tables import StateRecord
from app.llm import LlmHealth
from app.netinfo import IPV6_ANY, IPV6_ONLY_NOTE, base_url, lan_addresses, listens_on_all
from app.services import Services, open_services

LLM_STATE_KEY = "llm_health"
#: 自检结果多久之内算数：隔夜的「通过」不能当成现在模型可用
LLM_STATE_MAX_AGE = timedelta(hours=12)
#: 单个分析器的自检最多等多久（主备模型的探测本身最多 15 秒）
ANALYZER_SELFCHECK_TIMEOUT_S = 30.0

PASS, FAIL, NOT_CONFIGURED, HINT = "通过", "失败", "未配置", "提示"


def label(item: SelfCheckItem) -> str:
    if item.state == "not_configured":
        return NOT_CONFIGURED
    if item.state == "hint":
        return HINT
    return PASS if item.ok else FAIL


def counts_as_failure(item: SelfCheckItem) -> bool:
    return item.state != "hint" and (item.state == "not_configured" or not item.ok)


# ---------------------------------------------------------------------------
# 大模型状态的持久化（自检进程 → 服务进程）
# ---------------------------------------------------------------------------


def _stored_at(row: StateRecord | None) -> datetime | None:
    if row is None or not isinstance(row.value, dict):
        return None
    try:
        return datetime.fromisoformat(row.value["at"])
    except (KeyError, TypeError, ValueError):
        return None


async def save_llm_health(db: Database, health: LlmHealth, *, only_if_newer: bool = False) -> bool:
    """写入大模型状态。`only_if_newer`：库里已有同样新或更新的结果（例如服务运行中跑过自检）时不写，返回 False。"""
    at = health.updated_at or utcnow()
    value = {
        "status": health.status,
        "model": health.model,
        "detail": health.detail,
        "source": health.source,
        "at": at.isoformat(),
    }
    async with db.session() as session:
        if only_if_newer:
            stored = _stored_at(await session.get(StateRecord, LLM_STATE_KEY))
            if stored is not None and stored >= at:
                return False
        await session.merge(StateRecord(key=LLM_STATE_KEY, value=value, updated_at=utcnow()))
        await session.commit()
    return True


async def load_llm_health(db: Database, health: LlmHealth, *, now: datetime | None = None) -> bool:
    """把上次保存的结果（自检，或上次运行里的真实调用）读进 `health`。没有记录、太旧、或那次没配置密钥时不改，返回 False。"""
    async with db.session() as session:
        row = await session.get(StateRecord, LLM_STATE_KEY)
    if row is None or not isinstance(row.value, dict):
        return False
    value = row.value
    if value.get("status") not in ("ok", "down"):
        return False
    try:
        at = datetime.fromisoformat(value["at"])
    except (KeyError, TypeError, ValueError):
        return False
    if (now or utcnow()) - at > LLM_STATE_MAX_AGE:
        return False
    if health.updated_at is not None and health.updated_at >= at:
        return False  # 服务里已经有更新的真实调用结果
    source = value.get("source") if value.get("source") in ("selfcheck", "call") else "selfcheck"
    health.record(value["status"] == "ok", model=value.get("model"), detail=value.get("detail"), source=source, at=at)
    return True


# ---------------------------------------------------------------------------
# 公共项
# ---------------------------------------------------------------------------


def _config_items(settings: Settings, env_file: Path) -> list[SelfCheckItem]:
    items = [
        SelfCheckItem(
            "配置文件",
            env_file.exists(),
            str(env_file) if env_file.exists() else f"{env_file} 不存在，全部用默认值（没有任何密钥）",
            "把 server/.env.example 复制成 server/.env，填上密钥",
            state=None if env_file.exists() else "hint",
        ),
        SelfCheckItem(
            "模拟模式",
            not settings.mock_mode,
            "开启：报告是写死的示例" if settings.mock_mode else "关闭",
            "路演时不要开：把 .env 里的 MOCK_MODE 改成 0，启动时不要带 --mock",
        ),
    ]
    if settings.app_token == "dev-token":
        items.append(
            SelfCheckItem(
                "访问令牌",
                False,
                "还是默认值 dev-token",
                "在 .env 里设 APP_TOKEN，并在 App 里填同一个值（只挡误访问，不是安全边界）",
                state="hint",
            )
        )
    else:
        items.append(SelfCheckItem("访问令牌", True, "已配置（不是默认值）"))
    return items


def _db_hint(path: Path) -> str:
    return f"检查 {path.parent} 是否存在、有没有写权限、磁盘是否已满；数据库被别的程序占着时先关掉它；文件损坏时把它改名后重新启动（会丢掉历史鉴定）"


async def _db_item(services: Services) -> SelfCheckItem:
    path = services.settings.database_path
    hint = _db_hint(path)
    if services.table_errors:
        return SelfCheckItem("数据库", False, f"这些表没建成：{', '.join(services.table_errors)}", hint)
    try:
        async with services.db.session() as session:
            # merge：上次自检被中断留下的这一行也能覆盖，两个自检同时跑也不会撞主键
            await session.merge(StateRecord(key="selfcheck_write", value={"ok": True}, updated_at=utcnow()))
            await session.commit()
            await session.execute(delete(StateRecord).where(StateRecord.key == "selfcheck_write"))
            await session.commit()
    except Exception as exc:  # noqa: BLE001
        return SelfCheckItem("数据库", False, f"{path} 写入失败（{type(exc).__name__}）", hint)
    return SelfCheckItem("数据库", True, f"{path} 可写")


def ffmpeg_item(settings: Settings) -> SelfCheckItem:
    found = settings.ffmpeg_path if settings.ffmpeg_path and Path(settings.ffmpeg_path).is_file() else shutil.which("ffmpeg")
    if found:
        return SelfCheckItem("ffmpeg（P1 语音）", True, found)
    where = f"FFMPEG_PATH={settings.ffmpeg_path} 不是文件" if settings.ffmpeg_path else "PATH 里没有"
    return SelfCheckItem(
        "ffmpeg（P1 语音）",
        False,
        f"没找到（{where}）。只影响语音分析，服务照常启动",
        "需要语音分析时安装 ffmpeg，并把路径写进 .env 的 FFMPEG_PATH",
        state="hint",
    )


def _listen_item(settings: Settings) -> SelfCheckItem:
    if settings.host == IPV6_ANY:
        return SelfCheckItem("监听地址", False, base_url(settings.host, settings.port), IPV6_ONLY_NOTE, state="hint")
    if not listens_on_all(settings.host):
        return SelfCheckItem("监听地址", True, f"{base_url(settings.host, settings.port)}（配合 adb reverse）")
    addrs = lan_addresses()
    return SelfCheckItem(
        "监听地址",
        True,
        f"所有网卡，局域网地址 {', '.join(f'http://{a}:{settings.port}' for a in addrs) or '未找到'}",
        "需要在系统防火墙里放行这个端口",
        state="hint",
    )


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------


@dataclass
class SelfCheckResult:
    items: list[SelfCheckItem]

    @property
    def failures(self) -> list[SelfCheckItem]:
        return [i for i in self.items if counts_as_failure(i)]

    @property
    def ok(self) -> bool:
        return not self.failures


async def _analyzer_items(analyzer: Analyzer, services: Services) -> list[SelfCheckItem]:
    try:
        return await asyncio.wait_for(analyzer.selfcheck(services), timeout=ANALYZER_SELFCHECK_TIMEOUT_S)
    except asyncio.TimeoutError:
        detail = f"{ANALYZER_SELFCHECK_TIMEOUT_S:.0f} 秒内没有完成"
    except Exception as exc:  # noqa: BLE001  自检只报告，不抛
        detail = f"自检出错（{type(exc).__name__}）"
    return [SelfCheckItem(f"{analyzer.name.value} 自检", False, detail, "看 var/logs/server.log；这一路运行时会降级")]


async def run_selfcheck(
    settings: Settings | None = None,
    *,
    analyzers: list[Analyzer] | None = None,
    env_file: Path = ENV_FILE,
) -> SelfCheckResult:
    settings = settings or get_settings()
    items = _config_items(settings, env_file)
    async with AsyncExitStack() as stack:
        try:
            services = await stack.enter_async_context(open_services(settings))
        except Exception as exc:  # noqa: BLE001  打不开数据库也要把其余各项打印出来
            path = settings.database_path
            items.append(SelfCheckItem("数据库", False, f"{path} 打不开（{type(exc).__name__}）", _db_hint(path)))
            items.append(
                SelfCheckItem("分析器自检", False, "数据库打不开，没有继续检查规则、模型、案例和链接", "先处理上面数据库的问题")
            )
            items.append(ffmpeg_item(settings))
            items.append(_listen_item(settings))
            return SelfCheckResult(items)
        items.append(await _db_item(services))
        loaded = analyzers if analyzers is not None else load_analyzers()
        try:
            # 各分析器的自检互不依赖，同时跑：断网时总用时取决于最慢的一路，而不是加起来
            groups = await asyncio.gather(*(_analyzer_items(a, services) for a in loaded))
        finally:
            for analyzer in loaded:
                try:
                    await analyzer.shutdown()
                except Exception:  # noqa: BLE001
                    pass
        for group in groups:
            items.extend(group)
        if not any("ffmpeg" in i.name.lower() for i in items):
            items.append(ffmpeg_item(settings))
        items.append(_listen_item(settings))
        # 大模型的结果交给服务进程：没配置密钥也写一条（unknown），盖掉以前的旧结果
        try:
            await save_llm_health(services.db, services.llm_health)
        except Exception as exc:  # noqa: BLE001
            items.append(
                SelfCheckItem("保存自检结果", False, f"写数据库失败（{type(exc).__name__}）", "健康检查的 llm 字段会显示 unknown")
            )
    return SelfCheckResult(items)


def format_result(result: SelfCheckResult) -> list[str]:
    lines = []
    for item in result.items:
        lines.append(f"[{label(item)}] {item.name}：{item.detail}")
        if item.hint and (counts_as_failure(item) or item.state == "hint"):
            lines.append(f"       怎么办：{item.hint}")
    failed = result.failures
    if not failed:
        lines.append("全部通过，可以启动服务：uv run python -m app")
    else:
        lines.append(f"有 {len(failed)} 项没通过：{'、'.join(i.name for i in failed)}。按上面的提示处理后再跑一次自检。")
        lines.append("大模型或向量接口不可用时服务照样能启动，但报告只用规则，结论最高 suspicious。")
    return lines


def main(argv: list[str] | None = None) -> int:
    from app.__main__ import _force_utf8_console
    from app.logs import setup_logging

    _force_utf8_console()
    try:
        settings = get_settings()
    except ValidationError as exc:
        print(f"[失败] 配置文件：{ENV_FILE} 里有值不合法", flush=True)
        for err in exc.errors():
            name = "_".join(str(p) for p in err.get("loc", ())).upper() or "?"
            print(f"       {name}：{err.get('msg', '')}", flush=True)
        print("       怎么办：对照 .env.example 改正上面这些值，再跑一次自检", flush=True)
        return 1
    setup_logging(settings)
    print(f"启动前自检（数据库 {settings.database_path}）", flush=True)
    result = asyncio.run(run_selfcheck(settings))
    for line in format_result(result):
        print(line, flush=True)
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
