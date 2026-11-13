"""公共测试夹具。`tests/` 下所有子目录（含 tests/link、tests/cases、tests/eval）都能直接使用。

- `settings`：指向临时目录的配置（不读 server/.env，不带任何密钥）。
- `make_settings(**overrides)`：同上，可覆盖字段。
- `client` / `make_client(settings, pipeline_factory=None)`：带生命周期的 TestClient。
- `chat_request(*texts, urls=())`：快速构造一个合法的 POST /v1/checks 请求体。
- `contract_example(name)`：读 shared/contract-examples/ 下的示例。
- 网络：`block_network` 自动生效，测试里任何真实的出站连接都会直接报错（respx 打桩的请求不受影响）。
"""

from __future__ import annotations

import json
import os
import socket
from contextlib import contextmanager
from typing import Any, Callable, Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.paths import CONTRACT_EXAMPLES_DIR

TOKEN = "test-token"
DEVICE = "0f8fad5b-d9cb-469f-a165-70867728950e"
AUTH = {"Authorization": f"Bearer {TOKEN}", "X-Device-Id": DEVICE}


@pytest.fixture(autouse=True)
def block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """禁止真实的外网连接；只允许回环地址（TestClient 不走 socket，aiosqlite 不走网络）。"""
    real_connect = socket.socket.connect

    def guarded(self: socket.socket, address: Any) -> Any:
        host = address[0] if isinstance(address, tuple) else address
        if isinstance(host, str) and host not in ("127.0.0.1", "::1", "localhost"):
            raise RuntimeError(f"tests must not access the network (tried {host!r})")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)


@pytest.fixture(autouse=True)
def no_media_preload(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """默认测试不加载真实的 OCR / 转写 / 合成检测模型（装了 media 组时每起一次应用要多等几秒）。
    也不读 server/.env 里的 MEDIA_* 项。需要别的媒体配置的测试自己 setenv 后 cache_clear。"""
    from app.analyzers.deepfake import settings as media_settings

    for key in [k for k in os.environ if k.upper().startswith("MEDIA_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("MEDIA_PRELOAD", "0")
    monkeypatch.setattr(media_settings.MediaSettings, "model_config", {**media_settings.MediaSettings.model_config, "env_file": None})
    media_settings.get_media_settings.cache_clear()
    yield
    media_settings.get_media_settings.cache_clear()


def _build_settings(tmp_path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "var_dir": tmp_path / "var",
        "app_token": TOKEN,
        "mock_step_ms": 20,
        "cleanup_interval_s": 3600,
        "llm_primary_api_key": None,
        "llm_backup_api_key": None,
        "embedding_api_key": None,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.fixture
def make_settings(tmp_path) -> Callable[..., Settings]:
    return lambda **overrides: _build_settings(tmp_path, **overrides)


@pytest.fixture
def settings(make_settings) -> Settings:
    return make_settings()


@pytest.fixture
def make_client() -> Iterator[Callable[..., TestClient]]:
    from app.main import create_app

    stack: list[TestClient] = []

    def factory(settings: Settings, pipeline_factory=None) -> TestClient:
        client = TestClient(create_app(settings, pipeline_factory=pipeline_factory))
        client.__enter__()
        stack.append(client)
        return client

    yield factory
    while stack:
        stack.pop().__exit__(None, None, None)


@pytest.fixture
def client(make_client, settings) -> TestClient:
    return make_client(settings)


def chat_request(*texts: str, urls: tuple[str, ...] = (), speaker: str = "other") -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    if texts:
        items.append(
            {
                "type": "chat",
                "origin": "pasted_text",
                "lines": [{"line_id": f"0-{i}", "speaker": speaker, "text": t} for i, t in enumerate(texts)],
            }
        )
    items.extend({"type": "url", "url": u, "origin": "typed"} for u in urls)
    return {"client": {"app_version": "0.1.0"}, "source": "in_app", "items": items}


def contract_example(name: str) -> Any:
    return json.loads((CONTRACT_EXAMPLES_DIR / f"{name}.json").read_text(encoding="utf-8"))


def poll_until_done(client: TestClient, check_id: str, timeout_s: float = 10.0) -> dict[str, Any]:
    """轮询直到 succeeded / failed，返回最后一次的响应体。"""
    import time

    deadline = time.monotonic() + timeout_s
    while True:
        res = client.get(f"/v1/checks/{check_id}", headers=AUTH)
        assert res.status_code == 200, res.text
        body = res.json()
        if body["status"] in ("succeeded", "failed"):
            return body
        if time.monotonic() > deadline:
            raise AssertionError(f"check {check_id} did not finish: {body}")
        time.sleep(0.02)


@contextmanager
def does_not_raise() -> Iterator[None]:
    yield


def pytest_collection_modifyitems(config, items):
    """带 @pytest.mark.live 的测试需要真实密钥和网络，只在 RUN_LIVE=1 时运行。"""
    import os

    if os.environ.get("RUN_LIVE") == "1":
        return
    skip = pytest.mark.skip(reason="需要 RUN_LIVE=1 和真实密钥")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)
