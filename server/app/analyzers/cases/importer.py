"""案例导入：`data/cases/*.jsonl` → `cases_case` 表（可重复执行）。

- 逐行校验，不合格的行报出「文件:行号 原因」并跳过，其余照常导入；有不合格行时命令返回非零。
- 按 `case_id` 插入或更新。算向量的文本（标题 + 正文）、向量型号、维度都没变时不重新算向量。
- 需要算向量但没配密钥时，什么都不写，直接给出提示。
- 向量接口中途失败时，已经算好的批次已写库；修好后重跑即可续上。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import delete, select

from app.analyzers.cases.embedder import KEY_HINT, Embedder, EmbeddingError
from app.analyzers.cases.index import vector_to_bytes
from app.analyzers.cases.models import Case
from app.contract import Scene
from app.db import Database, utcnow
from app.paths import DATA_DIR

CASES_DIR = DATA_DIR / "cases"
SYNTHETIC_SOURCE = "合成示例"
_CASE_ID = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


class CaseRecord(BaseModel):
    """jsonl 里的一行。字段说明见 data/cases/README.md。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    case_id: str
    title: str = Field(min_length=1, max_length=200)
    scene: Scene
    snippet: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=4000)
    source_name: str = Field(min_length=1, max_length=200)
    source_url: str | None = None
    published_at: str | None = None
    synthetic: bool = False

    @field_validator("case_id")
    @classmethod
    def _case_id(cls, v: str) -> str:
        if not _CASE_ID.match(v):
            raise ValueError("case_id 只能用字母、数字、下划线、连字符，最长 64")
        return v

    @field_validator("scene")
    @classmethod
    def _scene(cls, v: Scene) -> Scene:
        if v == Scene.NONE:
            raise ValueError("scene 不能是 none")
        return v

    @field_validator("source_url", "published_at", mode="before")
    @classmethod
    def _empty_is_none(cls, v):
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("published_at")
    @classmethod
    def _date(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                date.fromisoformat(v)
            except ValueError as exc:
                raise ValueError("published_at 要写成 YYYY-MM-DD") from exc
            if len(v) != 10:
                raise ValueError("published_at 要写成 YYYY-MM-DD")
        return v

    @model_validator(mode="after")
    def _source(self) -> "CaseRecord":
        if self.synthetic:
            if self.source_name != SYNTHETIC_SOURCE:
                raise ValueError(f"合成样例的 source_name 必须是「{SYNTHETIC_SOURCE}」")
            if self.source_url is not None:
                raise ValueError("合成样例的 source_url 必须为空")
        else:
            if self.source_name == SYNTHETIC_SOURCE:
                raise ValueError("source_name 是「合成示例」时必须写 synthetic: true")
            if not self.source_url or not re.match(r"^https?://\S+$", self.source_url):
                raise ValueError("真实案例必须有来源链接（http:// 或 https:// 开头）")
        return self

    @property
    def embed_text(self) -> str:
        return f"{self.title}\n{self.body}"

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.embed_text.encode("utf-8")).hexdigest()


@dataclass
class RowError:
    file: str
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.file}:{self.line}  {self.message}"


@dataclass
class ParsedFile:
    records: list[tuple[str, int, CaseRecord]] = field(default_factory=list)
    errors: list[RowError] = field(default_factory=list)
    #: 解析不合格但能读出 case_id 的行（--prune 时不删这些）
    invalid_ids: set[str] = field(default_factory=set)


def _format_validation(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"])
        msg = err["msg"].removeprefix("Value error, ")
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "；".join(parts)


def parse_files(files: list[Path]) -> ParsedFile:
    out = ParsedFile()
    seen: dict[str, str] = {}
    for path in files:
        with path.open("r", encoding="utf-8-sig") as fh:
            for no, raw in enumerate(fh, 1):
                line = raw.strip()
                if not line or line.startswith("//"):
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError as exc:
                    out.errors.append(RowError(path.name, no, f"不是合法的 JSON（{exc.msg}）"))
                    continue
                if not isinstance(data, dict):
                    out.errors.append(RowError(path.name, no, "每行必须是一个 JSON 对象"))
                    continue
                try:
                    rec = CaseRecord.model_validate(data)
                except ValidationError as exc:
                    if isinstance(data.get("case_id"), str):
                        out.invalid_ids.add(data["case_id"])
                    out.errors.append(RowError(path.name, no, _format_validation(exc)))
                    continue
                if rec.case_id in seen:
                    out.errors.append(RowError(path.name, no, f"case_id {rec.case_id} 重复（已在 {seen[rec.case_id]} 出现）"))
                    continue
                seen[rec.case_id] = f"{path.name}:{no}"
                out.records.append((path.name, no, rec))
    return out


def find_files(directory: Path = CASES_DIR) -> list[Path]:
    return sorted(directory.glob("*.jsonl"))


@dataclass
class ImportReport:
    files: list[str] = field(default_factory=list)
    errors: list[RowError] = field(default_factory=list)
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    embedded: int = 0
    #: 库里有、jsonl 里已经没有的案例
    stale: list[str] = field(default_factory=list)
    pruned: int = 0
    #: 需要算向量但没算成（没密钥 / 接口失败）
    pending_embedding: int = 0
    embedding_error: str | None = None
    missing_key: bool = False
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        """有不合格行、没密钥、向量接口失败时为 False（命令返回 1）。"""
        return not self.errors and self.embedding_error is None and not self.missing_key

    def lines(self) -> list[str]:
        out = [f"读取文件：{', '.join(self.files) or '（没有找到 *.jsonl）'}"]
        if self.errors:
            out.append(f"不合格的行 {len(self.errors)} 条（已跳过，其余照常导入）：")
            out += [f"  {e}" for e in self.errors]
        if self.missing_key:
            out.append(f"有 {self.pending_embedding} 条案例需要计算向量，但没有配置向量接口密钥，本次没有写入任何数据。")
            out.append(f"  请{KEY_HINT}，然后重新运行导入。")
            return out
        prefix = "（试运行，没有写库）" if self.dry_run else ""
        out.append(
            f"{prefix}新增 {self.inserted} 条，更新 {self.updated} 条，未变 {self.unchanged} 条，"
            f"本次计算向量 {self.embedded} 条"
        )
        if self.embedding_error:
            out.append(
                f"向量接口失败（{self.embedding_error}），还有 {self.pending_embedding} 条没有向量。"
                "检查网络和密钥后重新运行导入，已完成的部分不会重复计算。"
            )
        if self.pruned:
            out.append(f"已删除 jsonl 里不再存在的案例 {self.pruned} 条")
        elif self.stale:
            out.append(f"库里有 {len(self.stale)} 条案例在 jsonl 里已经不存在（加 --prune 删除）：{', '.join(self.stale[:10])}")
        return out


def _needs_embedding(row: Case | None, rec: CaseRecord, embedder: Embedder) -> bool:
    return (
        row is None
        or row.embedding is None
        or row.content_hash != rec.content_hash
        or row.embedding_model != embedder.model
        or row.embedding_dim != embedder.dim
    )


def _values(rec: CaseRecord, source_file: str) -> dict:
    return {
        "title": rec.title,
        "scene": rec.scene.value,
        "snippet": rec.snippet,
        "body": rec.body,
        "source_name": rec.source_name,
        "source_url": rec.source_url,
        "published_at": rec.published_at,
        "synthetic": rec.synthetic,
        "content_hash": rec.content_hash,
        "source_file": source_file,
    }


def _differs(row: Case, rec: CaseRecord, source_file: str) -> bool:
    return any(getattr(row, k) != v for k, v in _values(rec, source_file).items())


def _apply(row: Case, rec: CaseRecord, source_file: str) -> bool:
    """把 jsonl 的字段写进 row，返回是否有变化（不含向量）。"""
    changed = False
    for key, value in _values(rec, source_file).items():
        if getattr(row, key) != value:
            setattr(row, key, value)
            changed = True
    return changed


async def import_cases(
    db: Database,
    embedder: Embedder,
    files: list[Path],
    *,
    prune: bool = False,
    dry_run: bool = False,
) -> ImportReport:
    parsed = parse_files(files)
    report = ImportReport(files=[p.name for p in files], errors=parsed.errors, dry_run=dry_run)

    async with db.session() as session:
        existing = {row.case_id: row for row in (await session.execute(select(Case))).scalars().all()}

    wanted = {rec.case_id for _, _, rec in parsed.records}
    report.stale = sorted(cid for cid in existing if cid not in wanted and cid not in parsed.invalid_ids)
    to_embed = [(f, rec) for f, _, rec in parsed.records if _needs_embedding(existing.get(rec.case_id), rec, embedder)]
    meta_only = [(f, rec) for f, _, rec in parsed.records if not _needs_embedding(existing.get(rec.case_id), rec, embedder)]
    report.pending_embedding = len(to_embed)

    if dry_run:
        for _, rec in to_embed:
            if rec.case_id in existing:
                report.updated += 1
            else:
                report.inserted += 1
        for f, rec in meta_only:
            changed = _differs(existing[rec.case_id], rec, f)
            report.updated += int(changed)
            report.unchanged += int(not changed)
        return report

    if to_embed and not embedder.configured:
        report.missing_key = True
        return report

    now = utcnow()
    async with db.session() as session:
        for f, rec in meta_only:
            row = await session.get(Case, rec.case_id)
            assert row is not None
            if _apply(row, rec, f):
                row.updated_at = now
                report.updated += 1
            else:
                report.unchanged += 1
        if prune and report.stale:
            await session.execute(delete(Case).where(Case.case_id.in_(report.stale)))
            report.pruned = len(report.stale)
        await session.commit()

    for start in range(0, len(to_embed), embedder.batch_size):
        batch = to_embed[start : start + embedder.batch_size]
        try:
            vectors = await embedder.embed([rec.embed_text for _, rec in batch])
        except EmbeddingError as exc:
            report.embedding_error = exc.kind
            break
        async with db.session() as session:
            for (f, rec), vec in zip(batch, vectors):
                row = await session.get(Case, rec.case_id)
                if row is None:
                    row = Case(case_id=rec.case_id)
                    session.add(row)
                    report.inserted += 1
                else:
                    report.updated += 1
                _apply(row, rec, f)
                row.embedding = vector_to_bytes(vec)
                row.embedding_model = embedder.model
                row.embedding_dim = embedder.dim
                row.updated_at = now
            await session.commit()
        report.embedded += len(batch)
        report.pending_embedding -= len(batch)
    return report
