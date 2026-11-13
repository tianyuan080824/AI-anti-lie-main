"""内存里的案例向量索引：启动时从 `cases_case` 读出，查询时算余弦相似度。不联网。"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from sqlalchemy import select

from app.analyzers.cases.models import Case
from app.contract import Scene, SimilarCase
from app.db import Database


@dataclass(frozen=True)
class CaseMeta:
    case_id: str
    title: str
    scene: str
    snippet: str
    source_name: str
    source_url: str | None
    published_at: str | None
    synthetic: bool

    def to_similar(self, similarity: float) -> SimilarCase:
        return SimilarCase(
            case_id=self.case_id,
            title=self.title,
            scene=Scene(self.scene),
            snippet=self.snippet,
            source_name=self.source_name,
            source_url=self.source_url,
            published_at=self.published_at,
            similarity=similarity,
            synthetic=self.synthetic,
        )


@dataclass
class CaseIndex:
    model: str
    dim: int
    #: 库里的案例总数（含向量不可用的）
    total: int = 0
    synthetic: int = 0
    #: 向量型号或维度和当前配置不符的条数
    mismatched: int = 0
    #: 没有向量的条数（导入没做完）
    missing: int = 0
    metas: list[CaseMeta] = field(default_factory=list)
    #: (usable, dim)，每行已归一化
    matrix: np.ndarray = field(default_factory=lambda: np.zeros((0, 0), dtype=np.float32))

    @property
    def usable(self) -> int:
        return len(self.metas)

    def search(self, query: np.ndarray, *, top_k: int, threshold: float) -> list[SimilarCase]:
        """返回相似度 ≥ threshold 的前 top_k 条，按相似度降序。"""
        if self.usable == 0 or top_k <= 0:
            return []
        q = np.asarray(query, dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(q))
        if q.shape[0] != self.dim or norm == 0.0:
            return []
        scores = self.matrix @ (q / norm)
        order = np.argsort(-scores, kind="stable")[:top_k]
        out = []
        for i in order:
            score = float(scores[i])
            if score < threshold:
                break
            out.append(self.metas[i].to_similar(round(min(1.0, max(0.0, score)), 4)))
        return out

    def best_score(self, query: np.ndarray) -> float | None:
        if self.usable == 0:
            return None
        q = np.asarray(query, dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(q))
        if q.shape[0] != self.dim or norm == 0.0:
            return None
        return float(np.max(self.matrix @ (q / norm)))


def vector_from_bytes(raw: bytes | None, dim: int) -> np.ndarray | None:
    if raw is None or len(raw) != dim * 4:
        return None
    return np.frombuffer(raw, dtype="<f4").astype(np.float32)


def vector_to_bytes(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype="<f4").reshape(-1).tobytes()


async def load_index(db: Database, *, model: str, dim: int) -> CaseIndex:
    """只读本地数据库。型号或维度对不上的向量不进索引（计入 mismatched），需要重新导入。"""
    async with db.session() as session:
        rows = (await session.execute(select(Case).order_by(Case.case_id))).scalars().all()
    index = CaseIndex(model=model, dim=dim, total=len(rows))
    vectors: list[np.ndarray] = []
    for row in rows:
        if row.synthetic:
            index.synthetic += 1
        if row.embedding is None:
            index.missing += 1
            continue
        vec = vector_from_bytes(row.embedding, dim)
        if row.embedding_model != model or row.embedding_dim != dim or vec is None:
            index.mismatched += 1
            continue
        norm = float(np.linalg.norm(vec))
        if norm == 0.0:
            index.missing += 1
            continue
        vectors.append(vec / norm)
        index.metas.append(
            CaseMeta(
                case_id=row.case_id,
                title=row.title,
                scene=row.scene,
                snippet=row.snippet,
                source_name=row.source_name,
                source_url=row.source_url,
                published_at=row.published_at,
                synthetic=row.synthetic,
            )
        )
    if vectors:
        index.matrix = np.vstack(vectors).astype(np.float32)
    else:
        index.matrix = np.zeros((0, dim), dtype=np.float32)
    return index
