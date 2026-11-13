"""配置、日志、限流器、ID 等小部件。"""

from __future__ import annotations

import json
import logging

from app.api.ratelimit import RateLimiter
from app.config import Settings, override_settings, get_settings
from app.ids import new_check_id, new_trace_id
from app.logs import log_event, setup_logging


def test_env_file_is_read_as_utf8(tmp_path, monkeypatch):
    for key in ("APP_TOKEN", "LLM_PRIMARY_MODEL", "MOCK_MODE"):
        monkeypatch.delenv(key, raising=False)
    env = tmp_path / ".env"
    env.write_text("APP_TOKEN=令牌123\nLLM_PRIMARY_MODEL=qwen-test\nMOCK_MODE=1\n", encoding="utf-8")
    s = Settings(_env_file=env)
    assert s.app_token == "令牌123" and s.llm_primary_model == "qwen-test" and s.mock_mode is True


def test_env_var_overrides(monkeypatch):
    monkeypatch.setenv("CASES_SIMILARITY_THRESHOLD", "0.72")
    monkeypatch.setenv("TIMEOUT_LINK_S", "4.5")
    s = Settings(_env_file=None)
    assert s.cases_similarity_threshold == 0.72
    assert s.analyzer_timeout("link") == 4.5


def test_defaults_match_contract_and_budget():
    s = Settings(_env_file=None)
    assert (s.host, s.port) == ("127.0.0.1", 8000)
    assert (s.limit_items, s.limit_lines, s.limit_line_chars, s.limit_urls) == (8, 200, 500, 5)
    assert (s.timeout_script_s, s.timeout_link_s, s.timeout_cases_s, s.timeout_deepfake_s) == (12, 5, 3, 15)
    assert s.check_deadline_s == 20 and s.retention_days == 7 and s.cross_check == "parallel"
    assert s.var_dir.name == "var" and s.database_path.parent == s.var_dir
    assert s.effective_embedding_api_key is None


def test_override_settings():
    with override_settings(mock_mode=True) as s:
        assert get_settings() is s and s.mock_mode
    assert get_settings() is not s


def test_rate_limiter_refills():
    now = [0.0]
    rl = RateLimiter(per_minute=60, burst=2, clock=lambda: now[0])
    assert rl.acquire("a")[0] and rl.acquire("a")[0]
    ok, wait = rl.acquire("a")
    assert not ok and 0.9 <= wait <= 1.01
    now[0] += 1.0
    assert rl.acquire("a")[0]
    assert rl.acquire("b")[0]


def test_ids_are_unique_and_prefixed():
    ids = {new_check_id() for _ in range(1000)}
    assert len(ids) == 1000 and all(i.startswith("c_") and len(i) == 28 for i in ids)
    assert new_trace_id().startswith("t_")


def test_file_log_is_utf8_json(settings):
    setup_logging(settings, console=False, file=True)
    try:
        log_event(logging.getLogger("t"), "测试事件", trace_id="t_1", lines=3)
        for handler in logging.getLogger().handlers:
            handler.flush()
        last = (settings.log_dir / "server.log").read_text(encoding="utf-8").strip().splitlines()[-1]
        record = json.loads(last)
        assert record["event"] == "测试事件" and record["trace_id"] == "t_1" and record["lines"] == 3
    finally:
        setup_logging(settings, console=False, file=False)


def test_source_files_use_explicit_utf8():
    """所有读写文本文件的地方都要显式 encoding="utf-8"（中文 Windows 默认 GBK）。"""
    import re

    from app.paths import SERVER_DIR

    offenders = []
    pattern = re.compile(r"\.(read_text|write_text)\((?![^)]*encoding=)|\bopen\((?![^)]*encoding=)(?![^)]*['\"][rwa]?b)")
    for path in list((SERVER_DIR / "app").rglob("*.py")) + list((SERVER_DIR / "eval").rglob("*.py")):
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line) and "noqa: utf8" not in line:
                offenders.append(f"{path.relative_to(SERVER_DIR)}:{no}")
    assert offenders == []


def test_analyzer_package_cli_entry(settings, capsys):
    """分析器包的命令行入口（python -m app.analyzers.<包>）不需要改 pyproject.toml。"""
    from app.analyzers.cases.__main__ import main

    with override_settings(var_dir=settings.var_dir):
        assert main(["status"]) == 0
    assert "数据库" in capsys.readouterr().out
    assert (settings.var_dir / "antifraud.db").exists()
