"""相似案例测试的辅助：假的 Embedder、写 jsonl、构造输入。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Sequence

import numpy as np
import pytest

from app.analyzers.base import AnalysisInput, InputLine
from app.analyzers.cases.embedder import Embedder, EmbeddingError
from app.contract import Speaker

DIM = 4
MODEL = "fake-embed"


def unit(*values: float) -> np.ndarray:
    v = np.asarray(values, dtype=np.float32)
    return v / np.linalg.norm(v)


class FakeEmbedder(Embedder):
    """按关键词给出确定的向量；记录每次调用的文本。"""

    def __init__(
        self,
        vectors: dict[str, Sequence[float]] | None = None,
        *,
        model: str = MODEL,
        dim: int = DIM,
        configured: bool = True,
        fail: str | None = None,
        fail_after_calls: int | None = None,
        batch_size: int = 10,
    ) -> None:
        self.vectors = vectors or {}
        self.model = model
        self.dim = dim
        self._configured = configured
        self.fail = fail
        self.fail_after_calls = fail_after_calls
        self.batch_size = batch_size
        self.calls: list[list[str]] = []
        self.closed = False

    @property
    def configured(self) -> bool:
        return self._configured

    def vector_for(self, text: str) -> np.ndarray:
        for key, vec in self.vectors.items():
            if key in text:
                return np.asarray(vec, dtype=np.float32)
        return np.ones(self.dim, dtype=np.float32)

    async def embed(self, texts, *, timeout_s=None):
        texts = list(texts)
        self.calls.append(texts)
        if self.fail_after_calls is not None and len(self.calls) > self.fail_after_calls:
            raise EmbeddingError("timeout")
        if self.fail:
            raise EmbeddingError(self.fail)
        return np.vstack([self.vector_for(t) for t in texts]).astype(np.float32)

    @property
    def embedded_texts(self) -> list[str]:
        return [t for call in self.calls for t in call]

    async def aclose(self) -> None:
        self.closed = True


def case_row(case_id: str, title: str, body: str = "正文", **extra: Any) -> dict[str, Any]:
    row = {
        "case_id": case_id,
        "title": title,
        "scene": "brushing_rebate",
        "snippet": f"{title}的摘要",
        "body": body,
        "source_name": "合成示例",
        "source_url": None,
        "published_at": None,
        "synthetic": True,
    }
    row.update(extra)
    return row


def write_jsonl(path: Path, rows: list[Any]) -> Path:
    lines = [r if isinstance(r, str) else json.dumps(r, ensure_ascii=False) for r in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def make_input(*lines: tuple[str, str]) -> AnalysisInput:
    return AnalysisInput(
        check_id="c_test",
        trace_id="t_test",
        lines=tuple(
            InputLine(line_id=f"0-{i}", speaker=Speaker(sp), text=text, item_index=0)
            for i, (sp, text) in enumerate(lines)
        ),
    )


@pytest.fixture
def cases_dir(tmp_path) -> Path:
    d = tmp_path / "cases_data"
    d.mkdir()
    return d


@pytest.fixture
def embedder_factory() -> Callable[..., Callable]:
    """返回 `(embedder) -> factory`，供 CasesAnalyzer(embedder_factory=...) 使用。"""
    return lambda embedder: (lambda settings: embedder)
