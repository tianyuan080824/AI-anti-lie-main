"""鉴定任务的存取（checks 表）。所有写操作串行化，避免 SQLite 写锁竞争。"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, select, update

from app.contract import ANALYZER_ORDER, AnalyzerState, AnalyzerStatus, CheckStatus, ErrorBody
from app.db import Database, utcnow
from app.db.tables import CheckRecord, FeedbackRecord


def initial_states() -> list[dict[str, Any]]:
    return [AnalyzerState(name=n, status=AnalyzerStatus.PENDING).model_dump(mode="json") for n in ANALYZER_ORDER]


def dump_states(states: list[AnalyzerState]) -> list[dict[str, Any]]:
    return [s.model_dump(mode="json") for s in states]


class CheckStore:
    def __init__(self, db: Database, retention_days: float) -> None:
        self.db = db
        self.retention = timedelta(days=retention_days)
        self._lock = asyncio.Lock()

    async def create(
        self,
        *,
        check_id: str,
        trace_id: str,
        device_id: str | None,
        request: dict[str, Any],
        mock: bool,
    ) -> CheckRecord:
        now = utcnow()
        record = CheckRecord(
            check_id=check_id,
            trace_id=trace_id,
            device_id=device_id,
            status=CheckStatus.QUEUED.value,
            mock=mock,
            created_at=now,
            updated_at=now,
            expires_at=now + self.retention,
            request=request,
            analyzers=initial_states(),
            derived_lines=[],
        )
        async with self._lock, self.db.session() as session:
            session.add(record)
            await session.commit()
        return record

    async def _update(self, check_id: str, **values: Any) -> None:
        values["updated_at"] = utcnow()
        async with self._lock, self.db.session() as session:
            await session.execute(update(CheckRecord).where(CheckRecord.check_id == check_id).values(**values))
            await session.commit()

    async def set_running(self, check_id: str) -> None:
        await self._update(check_id, status=CheckStatus.RUNNING.value)

    async def set_progress(self, check_id: str, states: list[AnalyzerState]) -> None:
        await self._update(check_id, analyzers=dump_states(states))

    async def set_succeeded(
        self,
        check_id: str,
        *,
        report: dict[str, Any],
        states: list[AnalyzerState],
        derived_lines: list[dict[str, Any]],
        runs: dict[str, Any] | None,
        elapsed_ms: int,
    ) -> None:
        await self._update(
            check_id,
            status=CheckStatus.SUCCEEDED.value,
            report=report,
            analyzers=dump_states(states),
            derived_lines=derived_lines,
            runs=runs,
            elapsed_ms=elapsed_ms,
        )

    async def set_failed(self, check_id: str, error: ErrorBody, states: list[dict[str, Any]] | None = None) -> None:
        values: dict[str, Any] = {"status": CheckStatus.FAILED.value, "error": error.model_dump(mode="json")}
        if states is not None:
            values["analyzers"] = states
        await self._update(check_id, **values)

    async def get(self, check_id: str) -> CheckRecord | None:
        """返回未过期的任务；不存在或已过期返回 None。"""
        async with self.db.session() as session:
            record = await session.get(CheckRecord, check_id)
        if record is None or record.expires_at <= utcnow():
            return None
        return record

    async def fail_orphans(self, error_for: Any) -> int:
        """进程启动时：把上次遗留的 queued / running 任务标成 failed。

        `error_for(trace_id) -> ErrorBody`。没跑完的分析器一并标成 failed，让客户端的进度条收尾。
        """
        unfinished = (CheckStatus.QUEUED.value, CheckStatus.RUNNING.value)
        async with self._lock, self.db.session() as session:
            rows = (await session.execute(select(CheckRecord).where(CheckRecord.status.in_(unfinished)))).scalars().all()
            for record in rows:
                states = []
                for state in record.analyzers or initial_states():
                    if state.get("status") in (AnalyzerStatus.PENDING.value, AnalyzerStatus.RUNNING.value):
                        state = {"name": state["name"], "status": AnalyzerStatus.FAILED.value}
                    states.append(state)
                record.status = CheckStatus.FAILED.value
                record.analyzers = states
                record.error = error_for(record.trace_id).model_dump(mode="json")
                record.updated_at = utcnow()
            await session.commit()
            return len(rows)

    async def save_feedback(self, record: CheckRecord, *, device_id: str | None, label: str, note: str | None) -> None:
        """同一次鉴定再次反馈时覆盖上一条。和鉴定同时过期。"""
        async with self._lock, self.db.session() as session:
            await session.merge(
                FeedbackRecord(
                    check_id=record.check_id,
                    device_id=device_id,
                    label=label,
                    note=note,
                    created_at=utcnow(),
                    expires_at=record.expires_at,
                )
            )
            await session.commit()

    async def get_feedback(self, check_id: str) -> FeedbackRecord | None:
        async with self.db.session() as session:
            return await session.get(FeedbackRecord, check_id)

    async def purge_expired(self) -> int:
        """删掉过期的鉴定和反馈，返回删掉的鉴定条数。"""
        now = utcnow()
        async with self._lock, self.db.session() as session:
            result = await session.execute(delete(CheckRecord).where(CheckRecord.expires_at <= now))
            await session.execute(delete(FeedbackRecord).where(FeedbackRecord.expires_at <= now))
            await session.commit()
            return result.rowcount or 0
