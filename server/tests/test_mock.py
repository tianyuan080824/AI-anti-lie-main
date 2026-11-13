"""模拟模式：依次点亮各分析器，最后返回契约示例报告；不调用任何外部服务。"""

from __future__ import annotations

import time

from tests.conftest import AUTH, chat_request, contract_example, poll_until_done


def test_mock_returns_contract_example_report(make_client, make_settings):
    client = make_client(make_settings(mock_mode=True, mock_step_ms=20))
    res = client.post("/v1/checks", json=chat_request("随便什么内容都行"), headers=AUTH)
    assert res.status_code == 202
    body = poll_until_done(client, res.json()["check_id"])
    assert body["status"] == "succeeded"
    assert body["report"] == contract_example("report")
    assert [(a["name"], a["status"]) for a in body["analyzers"]] == [
        ("script", "done"),
        ("link", "done"),
        ("cases", "done"),
        ("deepfake", "done"),  # 示例报告里有 media
        ("summary", "done"),
    ]
    assert all(a["elapsed_ms"] == 20 for a in body["analyzers"])


def test_mock_lights_analyzers_progressively(make_client, make_settings):
    client = make_client(make_settings(mock_mode=True, mock_step_ms=150))
    check_id = client.post("/v1/checks", json=chat_request("内容内容内容内容"), headers=AUTH).json()["check_id"]
    seen: list[tuple[str, ...]] = []
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        body = client.get(f"/v1/checks/{check_id}", headers=AUTH).json()
        done = tuple(a["name"] for a in body["analyzers"] if a["status"] == "done")
        if not seen or seen[-1] != done:
            seen.append(done)
        if body["status"] == "succeeded":
            break
        time.sleep(0.03)
    # 至少观察到三个不同的中间进度，且「已完成」集合只增不减
    assert len(seen) >= 3, seen
    for before, after in zip(seen, seen[1:]):
        assert set(before) <= set(after)


def test_mock_does_not_load_analyzers(make_client, make_settings):
    def explode(services):
        raise AssertionError("mock mode must not build the real pipeline")

    client = make_client(make_settings(mock_mode=True), pipeline_factory=explode)
    assert client.get("/v1/health", headers=AUTH).json()["mock"] is True


def test_mock_still_validates_input(make_client, make_settings):
    client = make_client(make_settings(mock_mode=True))
    assert client.post("/v1/checks", json={"items": []}, headers=AUTH).status_code == 400
