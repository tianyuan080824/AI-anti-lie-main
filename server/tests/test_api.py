"""三个 P0 接口的主要路径和错误路径。"""

from __future__ import annotations

import pytest

from app.contract import CheckStatusResponse, ErrorResponse, HealthResponse
from tests.conftest import AUTH, DEVICE, TOKEN, chat_request, contract_example, poll_until_done


def assert_error(res, status: int, code: str, retryable: bool | None = None) -> dict:
    assert res.status_code == status, res.text
    body = ErrorResponse.model_validate(res.json())
    assert body.error.code.value == code
    assert body.error.trace_id.startswith("t_")
    assert res.headers["X-Trace-Id"] == body.error.trace_id
    if retryable is not None:
        assert body.error.retryable is retryable
    return res.json()


# ---- 健康检查 ----


def test_health_reports_real_state(client):
    res = client.get("/v1/health", headers={"Authorization": f"Bearer {TOKEN}"})
    assert res.status_code == 200
    body = HealthResponse.model_validate(res.json()).model_dump(mode="json")
    assert body == {
        "status": "ok",
        "version": "0.1.0",
        "rules_version": res.json()["rules_version"],
        "mock": False,
        "llm": "unknown",  # 没有任何真实调用或自检之前
        "db": "ok",
    }
    assert set(res.json()) == set(contract_example("health"))


def test_health_mock_flag(make_client, make_settings):
    client = make_client(make_settings(mock_mode=True))
    assert client.get("/v1/health", headers=AUTH).json()["mock"] is True


def test_health_requires_token(client):
    assert_error(client.get("/v1/health"), 401, "UNAUTHORIZED", retryable=False)


# ---- 鉴权 ----


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": TOKEN},
        {"Authorization": f"Basic {TOKEN}"},
    ],
)
def test_checks_reject_bad_token(client, headers):
    res = client.post("/v1/checks", json=chat_request("你好你好你好你好你好"), headers={**headers, "X-Device-Id": DEVICE})
    assert_error(res, 401, "UNAUTHORIZED")


def test_token_checked_before_body(client):
    res = client.post("/v1/checks", content=b"not json", headers={"Content-Type": "application/json"})
    assert_error(res, 401, "UNAUTHORIZED")


# ---- 设备标识 ----


def test_device_id_required(client):
    res = client.post("/v1/checks", json=chat_request("你好你好你好你好你好"), headers={"Authorization": f"Bearer {TOKEN}"})
    assert_error(res, 400, "INVALID_INPUT")


def test_device_id_format(client):
    res = client.post(
        "/v1/checks",
        json=chat_request("你好你好你好你好你好"),
        headers={"Authorization": f"Bearer {TOKEN}", "X-Device-Id": "bad id with spaces"},
    )
    assert_error(res, 400, "INVALID_INPUT")


# ---- 发起与查询 ----


def test_submit_then_poll_returns_contract_report(client):
    res = client.post("/v1/checks", json=chat_request("日结兼职，点赞截图一单3元", "先垫付返利"), headers=AUTH)
    assert res.status_code == 202, res.text
    created = res.json()
    assert created["status"] == "queued"
    assert created["poll_after_ms"] == 800
    assert created["check_id"].startswith("c_")

    body = poll_until_done(client, created["check_id"])
    parsed = CheckStatusResponse.model_validate(body)
    assert parsed.status.value == "succeeded"
    assert [a["name"] for a in body["analyzers"]] == ["script", "link", "cases", "deepfake", "summary"]
    assert body["error"] is None
    assert body["derived_lines"] == []
    report = body["report"]
    for key in ("evidence", "links", "media", "similar_cases", "advice", "actions", "degraded"):
        assert isinstance(report[key], list), key  # 没有内容也返回空数组，不省略
    # 占位分析器：结论不是 high_risk，也不出现「安全」之类的保证
    assert report["verdict"] in ("suspicious", "inconclusive")
    assert "安全" not in report["headline"] + report["summary"] + "".join(report["advice"])


def test_poll_response_shape_while_running(make_client, make_settings):
    client = make_client(make_settings(mock_mode=True, mock_step_ms=300))
    check_id = client.post("/v1/checks", json=chat_request("你好你好你好你好你好"), headers=AUTH).json()["check_id"]
    body = client.get(f"/v1/checks/{check_id}", headers=AUTH).json()
    assert body["status"] in ("queued", "running")
    assert body["report"] is None and body["error"] is None
    assert set(body) == set(contract_example("check-status-running"))
    for state in body["analyzers"]:
        if state["status"] in ("pending", "running", "skipped"):
            assert "elapsed_ms" not in state


def test_unknown_check_is_404(client):
    assert_error(client.get("/v1/checks/c_DOESNOTEXIST", headers=AUTH), 404, "NOT_FOUND", retryable=False)


def test_expired_check_is_404(make_client, make_settings):
    client = make_client(make_settings(retention_days=0))
    check_id = client.post("/v1/checks", json=chat_request("你好你好你好你好你好"), headers=AUTH).json()["check_id"]
    assert_error(client.get(f"/v1/checks/{check_id}", headers=AUTH), 404, "NOT_FOUND")


def test_unknown_route_uses_error_format(client):
    assert_error(client.get("/v1/nope", headers=AUTH), 404, "NOT_FOUND")


# ---- 校验与限制 ----


def test_invalid_json(client):
    res = client.post("/v1/checks", content=b"{bad", headers={**AUTH, "Content-Type": "application/json"})
    body = assert_error(res, 400, "INVALID_INPUT", retryable=False)
    assert "{bad" not in body["error"]["message"]


def test_missing_items(client):
    assert_error(client.post("/v1/checks", json={"source": "in_app"}, headers=AUTH), 400, "INVALID_INPUT")


def test_empty_items(client):
    assert_error(client.post("/v1/checks", json={"items": []}, headers=AUTH), 400, "INVALID_INPUT")


def test_validation_message_does_not_echo_content(client):
    req = chat_request("我的秘密内容")
    req["items"][0]["lines"][0]["line_id"] = ""
    body = assert_error(client.post("/v1/checks", json=req, headers=AUTH), 400, "INVALID_INPUT")
    assert "秘密" not in body["error"]["message"]


def test_too_many_items(client):
    req = {"items": [{"type": "url", "url": f"https://a{i}.example.cn"} for i in range(9)]}
    assert_error(client.post("/v1/checks", json=req, headers=AUTH), 400, "INVALID_INPUT")


def test_too_many_lines(client):
    lines = [{"line_id": f"0-{i}", "speaker": "other", "text": "嗯"} for i in range(201)]
    req = {"items": [{"type": "chat", "lines": lines}]}
    assert_error(client.post("/v1/checks", json=req, headers=AUTH), 400, "INVALID_INPUT")


def test_line_count_limit_is_across_items(client):
    item = lambda k: {"type": "chat", "lines": [{"line_id": f"{k}-{i}", "text": "嗯"} for i in range(101)]}
    req = {"items": [item(0), item(1)]}
    assert_error(client.post("/v1/checks", json=req, headers=AUTH), 400, "INVALID_INPUT")


def test_exactly_at_limits_is_accepted(client):
    lines = [{"line_id": f"0-{i}", "speaker": "other", "text": "字" * 500} for i in range(200)]
    req = {"items": [{"type": "chat", "lines": lines}] + [{"type": "url", "url": f"https://a{i}.example.cn"} for i in range(5)]}
    assert client.post("/v1/checks", json=req, headers=AUTH).status_code == 202


def test_line_too_long(client):
    assert_error(client.post("/v1/checks", json=chat_request("字" * 501), headers=AUTH), 400, "INVALID_INPUT")


def test_too_many_urls(client):
    req = chat_request(urls=tuple(f"https://a{i}.example.cn" for i in range(6)))
    assert_error(client.post("/v1/checks", json=req, headers=AUTH), 400, "INVALID_INPUT")


def test_duplicate_line_ids(client):
    req = chat_request("第一行内容内容", "第二行内容内容")
    req["items"][0]["lines"][1]["line_id"] = "0-0"
    assert_error(client.post("/v1/checks", json=req, headers=AUTH), 400, "INVALID_INPUT")


def test_body_too_large(client):
    res = client.post("/v1/checks", content=b"x" * 1_000_001, headers={**AUTH, "Content-Type": "application/json"})
    assert_error(res, 400, "INVALID_INPUT")


def test_contract_request_example_is_accepted(client):
    res = client.post("/v1/checks", json=contract_example("check-request"), headers=AUTH)
    assert res.status_code == 202, res.text
    body = poll_until_done(client, res.json()["check_id"])
    assert body["status"] == "succeeded"
    states = {a["name"]: a["status"] for a in body["analyzers"]}
    assert states["deepfake"] == "done"  # 请求里有 audio 引用
    assert body["report"]["media"][0]["label"] == "not_analyzed"
    assert "deepfake_unavailable" in body["report"]["degraded"]
    assert [l["url"] for l in body["report"]["links"]] == ["https://t.example.cn/a1"]
    assert body["report"]["links"][0]["origin"] == "qrcode"


def test_unknown_fields_and_item_types_are_ignored(client):
    req = chat_request("你好你好你好你好你好")
    req["new_field"] = {"x": 1}
    req["items"].append({"type": "sticker", "sticker_id": "s1"})
    assert client.post("/v1/checks", json=req, headers=AUTH).status_code == 202


# ---- 限流 ----


def test_rate_limit(make_client, make_settings):
    client = make_client(make_settings(rate_limit_burst=2, rate_limit_per_minute=1))
    req = chat_request("你好你好你好你好你好")
    assert client.post("/v1/checks", json=req, headers=AUTH).status_code == 202
    assert client.post("/v1/checks", json=req, headers=AUTH).status_code == 202
    res = client.post("/v1/checks", json=req, headers=AUTH)
    assert_error(res, 429, "RATE_LIMITED", retryable=True)
    assert int(res.headers["Retry-After"]) >= 1
    # 另一台设备不受影响
    other = {**AUTH, "X-Device-Id": "another-device"}
    assert client.post("/v1/checks", json=req, headers=other).status_code == 202
