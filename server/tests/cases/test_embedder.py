"""OpenAI 兼容向量客户端：请求格式、失败分类、超时设置。全部用 respx 打桩。"""

from __future__ import annotations

import json

import httpx
import numpy as np
import pytest
import respx

from app.analyzers.cases.embedder import EmbeddingError, OpenAICompatEmbedder, create_embedder

BASE = "https://embed.example.test/v1"


def _embedder(**kw) -> OpenAICompatEmbedder:
    values = dict(base_url=BASE + "/", api_key="sk-test", model="m-test", dim=3, timeout_s=2.0, connect_timeout_s=0.5)
    values.update(kw)
    return OpenAICompatEmbedder(**values)


def _ok(n: int, dim: int = 3) -> httpx.Response:
    # 故意打乱 index 顺序，客户端要按 index 排回去
    data = [{"object": "embedding", "index": i, "embedding": [float(i)] * dim} for i in reversed(range(n))]
    return httpx.Response(200, json={"object": "list", "data": data, "model": "m-test"})


@respx.mock
async def test_request_format_and_order():
    route = respx.post(f"{BASE}/embeddings").mock(return_value=_ok(2))
    emb = _embedder()
    out = await emb.embed(["甲", "乙"])
    await emb.aclose()
    req = route.calls.last.request
    assert req.headers["Authorization"] == "Bearer sk-test"
    assert json.loads(req.content) == {"model": "m-test", "input": ["甲", "乙"], "dimensions": 3, "encoding_format": "float"}
    assert out.dtype == np.float32 and out.shape == (2, 3)
    assert out[:, 0].tolist() == [0.0, 1.0]


@respx.mock
async def test_batches_by_batch_size():
    route = respx.post(f"{BASE}/embeddings").mock(side_effect=lambda req: _ok(len(json.loads(req.content)["input"])))
    emb = _embedder(batch_size=2)
    out = await emb.embed(["a", "b", "c"])
    await emb.aclose()
    assert [len(json.loads(c.request.content)["input"]) for c in route.calls] == [2, 1]
    assert out.shape == (3, 3)


async def test_no_key_raises_without_request():
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(f"{BASE}/embeddings")
        with pytest.raises(EmbeddingError) as ei:
            await _embedder(api_key=None).embed(["a"])
    assert ei.value.kind == "no_api_key" and not route.called


@pytest.mark.parametrize(
    "effect,kind",
    [
        (httpx.ConnectTimeout("x"), "connect_timeout"),
        (httpx.ConnectError("x"), "connect_error"),
        (httpx.ReadTimeout("x"), "timeout"),
        (httpx.Response(401, json={"error": "bad key"}), "http_401"),
        (httpx.Response(200, json={"unexpected": 1}), "bad_response"),
        (httpx.Response(200, text="not json"), "bad_response"),
    ],
)
@respx.mock
async def test_failures_are_classified(effect, kind):
    respx.post(f"{BASE}/embeddings").mock(side_effect=[effect] if isinstance(effect, Exception) else None, return_value=None if isinstance(effect, Exception) else effect)
    emb = _embedder()
    with pytest.raises(EmbeddingError) as ei:
        await emb.embed(["a"])
    await emb.aclose()
    assert ei.value.kind == kind


@respx.mock
async def test_wrong_dimension_is_an_error():
    respx.post(f"{BASE}/embeddings").mock(return_value=_ok(1, dim=5))
    emb = _embedder()
    with pytest.raises(EmbeddingError) as ei:
        await emb.embed(["a"])
    await emb.aclose()
    assert ei.value.kind == "dim_mismatch"


@respx.mock
async def test_connect_timeout_shorter_than_read_timeout():
    seen = {}

    def handler(req):
        seen.update(req.extensions["timeout"])
        return _ok(1)

    respx.post(f"{BASE}/embeddings").mock(side_effect=handler)
    emb = _embedder(timeout_s=2.0, connect_timeout_s=0.5)
    await emb.embed(["a"], timeout_s=1.0)
    await emb.aclose()
    assert seen["connect"] == 0.5 and seen["read"] == 1.0


def test_create_embedder_reads_settings(make_settings):
    s = make_settings(llm_primary_api_key="sk-primary", embedding_dim=8, embedding_timeout_s=2.5, llm_connect_timeout_s=3)
    emb = create_embedder(s)
    assert emb.api_key == "sk-primary" and emb.dim == 8 and emb.model == s.embedding_model
    assert emb.connect_timeout_s < emb.timeout_s
    assert not create_embedder(make_settings()).configured


async def test_empty_input_needs_no_request():
    out = await _embedder().embed([])
    assert out.shape == (0, 3)
