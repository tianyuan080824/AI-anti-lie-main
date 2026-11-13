"""P1：用户反馈 `POST /v1/checks/{check_id}/feedback`（契约 4.4）。"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import update

from app.checks.store import CheckStore
from app.db import utcnow
from app.db.tables import CheckRecord, FeedbackRecord
from tests.conftest import AUTH, TOKEN, chat_request, contract_example
from tests.test_api import assert_error


def submit(client) -> str:
    res = client.post("/v1/checks", headers=AUTH, json=chat_request("你好"))
    assert res.status_code == 202, res.text
    return res.json()["check_id"]


async def _feedback(settings, check_id):
    from app.services import open_services

    async with open_services(settings) as services:
        async with services.db.session() as session:
            return await session.get(FeedbackRecord, check_id)


# ---- 反馈 ----


def test_feedback_contract_example_returns_204(client, settings):
    check_id = submit(client)
    res = client.post(f"/v1/checks/{check_id}/feedback", headers=AUTH, json=contract_example("feedback-request"))
    assert res.status_code == 204 and res.content == b""
    row = asyncio.run(_feedback(settings, check_id))
    assert row.label == "not_scam" and row.note == "这是我同事"
    assert row.device_id == AUTH["X-Device-Id"]


def test_feedback_note_is_redacted(client, settings):
    check_id = submit(client)
    note = "他让我转5000元到6222021234567890128，电话13812345678"
    res = client.post(f"/v1/checks/{check_id}/feedback", headers=AUTH, json={"label": "scam", "note": note})
    assert res.status_code == 204
    row = asyncio.run(_feedback(settings, check_id))
    assert row.note == "他让我转<AMOUNT:5000>到<CARD>，电话<PHONE>"


@pytest.mark.parametrize("note", [None, "", "   "])
def test_feedback_empty_note_stored_as_none(client, settings, note):
    check_id = submit(client)
    body = {"label": "unsure"} if note is None else {"label": "unsure", "note": note}
    assert client.post(f"/v1/checks/{check_id}/feedback", headers=AUTH, json=body).status_code == 204
    assert asyncio.run(_feedback(settings, check_id)).note is None


def test_feedback_note_limit_200(client):
    check_id = submit(client)
    ok = client.post(f"/v1/checks/{check_id}/feedback", headers=AUTH, json={"label": "scam", "note": "字" * 200})
    assert ok.status_code == 204
    res = client.post(f"/v1/checks/{check_id}/feedback", headers=AUTH, json={"label": "scam", "note": "字" * 201})
    body = assert_error(res, 400, "INVALID_INPUT")
    assert "字" * 50 not in body["error"]["message"]  # 不回显用户内容


@pytest.mark.parametrize("body", [{"label": "maybe"}, {}, {"label": "scam", "note": 3}])
def test_feedback_bad_body(client, body):
    check_id = submit(client)
    assert_error(client.post(f"/v1/checks/{check_id}/feedback", headers=AUTH, json=body), 400, "INVALID_INPUT")


def test_feedback_unknown_check_404(client):
    assert_error(client.post("/v1/checks/c_NOPE/feedback", headers=AUTH, json={"label": "scam"}), 404, "NOT_FOUND")


def test_feedback_expired_check_404(client, settings):
    check_id = submit(client)

    async def expire():
        from app.services import open_services

        async with open_services(settings) as services:
            async with services.db.session() as session:
                await session.execute(
                    update(CheckRecord).where(CheckRecord.check_id == check_id).values(expires_at=utcnow() - timedelta(seconds=1))
                )
                await session.commit()

    asyncio.run(expire())
    assert_error(client.post(f"/v1/checks/{check_id}/feedback", headers=AUTH, json={"label": "scam"}), 404, "NOT_FOUND")


def test_feedback_again_overwrites(client, settings):
    check_id = submit(client)
    client.post(f"/v1/checks/{check_id}/feedback", headers=AUTH, json={"label": "scam", "note": "第一次"})
    client.post(f"/v1/checks/{check_id}/feedback", headers=AUTH, json={"label": "not_scam"})
    row = asyncio.run(_feedback(settings, check_id))
    assert row.label == "not_scam" and row.note is None


def test_feedback_device_id_optional_but_validated(client, settings):
    check_id = submit(client)
    no_device = {"Authorization": f"Bearer {TOKEN}"}
    assert client.post(f"/v1/checks/{check_id}/feedback", headers=no_device, json={"label": "scam"}).status_code == 204
    assert asyncio.run(_feedback(settings, check_id)).device_id is None
    bad = {**no_device, "X-Device-Id": "bad id!"}
    assert_error(client.post(f"/v1/checks/{check_id}/feedback", headers=bad, json={"label": "scam"}), 400, "INVALID_INPUT")


def test_feedback_requires_token(client):
    check_id = submit(client)
    res = client.post(f"/v1/checks/{check_id}/feedback", json={"label": "scam"})
    assert_error(res, 401, "UNAUTHORIZED")


def test_feedback_works_in_mock_mode(make_client, make_settings):
    settings = make_settings(mock_mode=True)
    client = make_client(settings)
    check_id = submit(client)
    assert client.post(f"/v1/checks/{check_id}/feedback", headers=AUTH, json={"label": "scam"}).status_code == 204


async def test_feedback_purged_with_check(settings):
    from app.services import open_services

    async with open_services(settings) as services:
        store = CheckStore(services.db, retention_days=7)
        record = await store.create(check_id="c_X", trace_id="t_X", device_id=None, request={}, mock=False)
        await store.save_feedback(record, device_id=None, label="scam", note=None)
        assert (await store.get_feedback("c_X")).expires_at == record.expires_at
        async with services.db.session() as session:
            past = utcnow() - timedelta(seconds=1)
            await session.execute(update(CheckRecord).values(expires_at=past))
            await session.execute(update(FeedbackRecord).values(expires_at=past))
            await session.commit()
        assert await store.purge_expired() == 1
        assert await store.get_feedback("c_X") is None
