"""文本向量客户端。

`Embedder` 是接口，以后换成本地模型或别家接口只需另写一个实现。
`OpenAICompatEmbedder` 走 OpenAI 兼容的 `POST {base_url}/embeddings`（默认阿里云百炼）。
请求格式按 OpenAI 兼容约定写成，没有用真实密钥跑过，见 NOTES.md。
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Sequence

import httpx
import numpy as np

from app.config import Settings

KEY_HINT = "在 server/.env 里配置 EMBEDDING_API_KEY（或 LLM_PRIMARY_API_KEY，同一个百炼账号可共用）"


class EmbeddingError(Exception):
    """向量接口不可用。`kind` 是不含正文的短代码，可以写进日志和 note。"""

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.detail = detail


class Embedder(ABC):
    #: 存进案例表的型号标识；换型号后旧向量不能和新查询比较
    model: str
    dim: int
    #: 一次请求最多带几条文本
    batch_size: int = 10

    @property
    def configured(self) -> bool:
        """是否具备调用条件（如已配置密钥）。False 时调用方不应发请求。"""
        return True

    @abstractmethod
    async def embed(self, texts: Sequence[str], *, timeout_s: float | None = None) -> np.ndarray:
        """返回 `(len(texts), dim)` 的 float32 矩阵。失败抛 `EmbeddingError`。"""

    async def aclose(self) -> None:
        pass


class OpenAICompatEmbedder(Embedder):
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        model: str,
        dim: int,
        timeout_s: float,
        connect_timeout_s: float,
        batch_size: int = 10,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.dim = dim
        self.timeout_s = timeout_s
        self.connect_timeout_s = connect_timeout_s
        self.batch_size = batch_size
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(transport=self._transport, trust_env=False)
        return self._client

    async def embed(self, texts: Sequence[str], *, timeout_s: float | None = None) -> np.ndarray:
        if not self.api_key:
            raise EmbeddingError("no_api_key", KEY_HINT)
        texts = list(texts)
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        parts = [
            await self._embed_batch(texts[i : i + self.batch_size], timeout_s or self.timeout_s)
            for i in range(0, len(texts), self.batch_size)
        ]
        return np.vstack(parts)

    async def _embed_batch(self, texts: list[str], timeout_s: float) -> np.ndarray:
        timeout = httpx.Timeout(timeout_s, connect=min(self.connect_timeout_s, timeout_s / 2))
        payload = {"model": self.model, "input": texts, "dimensions": self.dim, "encoding_format": "float"}
        try:
            async with asyncio.timeout(timeout_s):
                res = await self._http().post(
                    f"{self.base_url}/embeddings",
                    json=payload,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    timeout=timeout,
                )
        except httpx.ConnectTimeout as exc:
            raise EmbeddingError("connect_timeout") from exc
        except httpx.ConnectError as exc:
            raise EmbeddingError("connect_error") from exc
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise EmbeddingError("timeout") from exc
        except httpx.HTTPError as exc:
            raise EmbeddingError("network", type(exc).__name__) from exc
        if res.status_code != 200:
            raise EmbeddingError(f"http_{res.status_code}")
        try:
            data = sorted(res.json()["data"], key=lambda d: d["index"])
            matrix = np.asarray([d["embedding"] for d in data], dtype=np.float32)
        except Exception as exc:  # noqa: BLE001 - 返回格式不对
            raise EmbeddingError("bad_response", type(exc).__name__) from exc
        if matrix.shape != (len(texts), self.dim):
            raise EmbeddingError("dim_mismatch", f"expected {(len(texts), self.dim)}, got {matrix.shape}")
        return matrix

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def create_embedder(settings: Settings) -> OpenAICompatEmbedder:
    return OpenAICompatEmbedder(
        base_url=settings.embedding_base_url,
        api_key=settings.effective_embedding_api_key,
        model=settings.embedding_model,
        dim=settings.embedding_dim,
        timeout_s=settings.embedding_timeout_s,
        connect_timeout_s=min(settings.llm_connect_timeout_s, settings.embedding_timeout_s / 2),
    )
