"""P1：规则集下发 `GET /v1/rules`（契约 4.5），`If-None-Match` 命中返回 304。"""

from __future__ import annotations

import json

import pytest

from app.contract import RulesFile
from app.paths import RULES_FILE
from tests.conftest import AUTH
from tests.test_api import assert_error

def test_rules_returns_shared_file_with_etag(client):
    res = client.get("/v1/rules", headers=AUTH)
    assert res.status_code == 200
    data = res.json()
    shipped = json.loads(RULES_FILE.read_text(encoding="utf-8"))
    RulesFile.model_validate(data)
    assert data["version"] == shipped["version"]
    assert [r["id"] for r in data["rules"]] == [r["id"] for r in shipped["rules"]]
    assert data["normalize"] == shipped["normalize"]
    assert res.headers["ETag"] == f'"{shipped["version"]}"'
    assert client.get("/v1/health", headers=AUTH).json()["rules_version"] == shipped["version"]


@pytest.mark.parametrize("fmt", ['{e}', 'W/{e}', '"old", {e}', '*'])
def test_rules_if_none_match_hits_304(client, fmt):
    etag = client.get("/v1/rules", headers=AUTH).headers["ETag"]
    res = client.get("/v1/rules", headers={**AUTH, "If-None-Match": fmt.format(e=etag)})
    assert res.status_code == 304 and res.content == b""
    assert res.headers["ETag"] == etag


@pytest.mark.parametrize("value", ['"2000.01.1"', "", "2026.11.2"])
def test_rules_if_none_match_miss_returns_body(client, value):
    res = client.get("/v1/rules", headers={**AUTH, "If-None-Match": value})
    assert res.status_code == 200 and res.json()["rules"]


def test_rules_requires_token(client):
    assert_error(client.get("/v1/rules"), 401, "UNAUTHORIZED")


def test_rules_unavailable_when_file_missing(client, monkeypatch, tmp_path):
    import app.api.routes as routes

    missing = tmp_path / "nope.json"
    monkeypatch.setattr(routes, "load_rules_file", lambda: __import__("app.rules", fromlist=["x"]).load_rules_file(missing))
    body = assert_error(client.get("/v1/rules", headers=AUTH), 503, "UNAVAILABLE", retryable=True)
    assert "内置" in body["error"]["message"]


def test_rules_unavailable_when_file_invalid(client, monkeypatch, tmp_path):
    import app.api.routes as routes
    from app.rules import load_rules_file

    bad = tmp_path / "rules.json"
    bad.write_text('{"version": "x"}', encoding="utf-8")
    monkeypatch.setattr(routes, "load_rules_file", lambda: load_rules_file(bad))
    assert_error(client.get("/v1/rules", headers=AUTH), 503, "UNAVAILABLE")


def test_rules_reloaded_after_file_change(client, monkeypatch, tmp_path):
    import os

    import app.api.routes as routes
    from app.rules import load_rules_file

    path = tmp_path / "rules.json"
    data = json.loads(RULES_FILE.read_text(encoding="utf-8"))
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(routes, "load_rules_file", lambda: load_rules_file(path))
    first = client.get("/v1/rules", headers=AUTH).headers["ETag"]
    data["version"] = "2099.01.1"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
    res = client.get("/v1/rules", headers={**AUTH, "If-None-Match": first})
    assert res.status_code == 200 and res.headers["ETag"] == '"2099.01.1"'
