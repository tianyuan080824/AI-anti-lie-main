"""统一配置。

所有配置项都在这里声明，从环境变量和 `server/.env` 读取（变量名是字段名的大写）。
第二阶段各分析器会用到的配置项已经提前声明在下面各自的分组里；
分析器通过 `ctx.settings`（运行时）或 `get_settings()`（命令行脚本）读取，不要自己读环境变量。

确实需要新配置项、又不能改本文件时：在自己的包里定义一个
`pydantic_settings.BaseSettings` 子类，`env_prefix` 用自己的前缀（如 `LINK_`），
`env_file=ENV_FILE`，第三阶段集成时再并进来。见 ARCHITECTURE.md。
"""

from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Iterator, Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.paths import SERVER_DIR

ENV_FILE = SERVER_DIR / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- 运行 ----
    host: str = "127.0.0.1"
    """监听地址。改成 0.0.0.0 用于「手机开热点、笔记本连热点」的备用连接方式。"""
    port: int = 8000
    app_token: str = "dev-token"
    """固定访问令牌（契约第 1 节：只挡误访问，不是安全边界）。"""
    mock_mode: bool = False
    """MOCK_MODE=1：不调用任何外部服务，按固定延迟点亮各分析器，返回契约示例报告。路演时不要开。"""
    mock_step_ms: int = 600
    """模拟模式下每个分析器从 running 到 done 的间隔。"""
    log_level: str = "INFO"

    # ---- 存储 ----
    var_dir: Path = SERVER_DIR / "var"
    db_path: Path | None = None
    """默认 var/antifraud.db。"""
    retention_days: float = 7
    cleanup_interval_s: float = 3600

    # ---- 契约第 1 节的限制 ----
    poll_after_ms: int = 800
    limit_items: int = 8
    limit_lines: int = 200
    limit_line_chars: int = 500
    limit_urls: int = 5
    limit_url_chars: int = 2048
    limit_body_bytes: int = 1_000_000
    limit_image_bytes: int = 5 * 1024 * 1024
    limit_audio_bytes: int = 10 * 1024 * 1024
    limit_audio_seconds: float = 60
    limit_video_bytes: int = 50 * 1024 * 1024
    limit_video_seconds: float = 30

    # ---- 限流与并发 ----
    rate_limit_per_minute: float = 20
    """每个设备每分钟可发起的鉴定次数（令牌桶补充速率）。"""
    rate_limit_burst: int = 10
    max_concurrent_checks: int = 4

    # ---- 时延预算（秒） ----
    check_deadline_s: float = 20
    timeout_script_s: float = 12
    timeout_link_s: float = 5
    timeout_cases_s: float = 3
    timeout_deepfake_s: float = 15
    timeout_summary_s: float = 2

    # ---- 大模型（OpenAI 兼容接口）----
    llm_primary_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    llm_primary_api_key: str | None = None
    llm_primary_model: str = "qwen3.7-plus"
    llm_backup_base_url: str = "https://open.bigmodel.cn/api/paas/v4/"
    llm_backup_api_key: str | None = None
    llm_backup_model: str = "glm-5.3-flash"
    llm_connect_timeout_s: float = 3
    """连接超时要比读取超时短：连不上就尽快放弃。读取超时由各分析器的预算决定。"""
    cross_check: Literal["off", "parallel"] = "parallel"

    # ---- 文本向量（案例检索）----
    embedding_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    embedding_api_key: str | None = None
    """不填时沿用 LLM_PRIMARY_API_KEY（同一个百炼账号）。"""
    embedding_model: str = "text-embedding-v4"
    embedding_dim: int = 1024
    embedding_timeout_s: float = 2.5

    # ---- 相似案例 ----
    cases_top_k: int = 3
    cases_similarity_threshold: float = 0.5
    """低于此相似度的案例不返回。用评测数据调。"""
    cases_query_max_chars: int = 1500

    # ---- 链接体检 ----
    link_expand_short_links: bool = True
    link_max_hops: int = 5
    link_hop_timeout_s: float = 3
    link_rdap_timeout_s: float = 3
    link_whois_timeout_s: float = 3
    link_rdap_base_url: str | None = None
    """覆盖 IANA 表里所有 RDAP 服务地址。只用于断网模拟（指到连不上的地址），平时不要设。"""
    link_whois_server: str | None = None
    """覆盖 `.cn` 的 WHOIS 服务器（43 端口）。只用于断网模拟，平时不要设。"""
    link_domain_cache_days: float = 7
    intel_timeout_s: float = 3
    intel_threatbook_api_key: str | None = None
    intel_qianxin_api_key: str | None = None
    intel_tencent_secret_id: str | None = None
    intel_tencent_secret_key: str | None = None

    # ---- 媒体（P1）----
    ffmpeg_path: str | None = None
    media_max_age_s: float = 3600

    # ---- 派生值 ----
    @property
    def database_path(self) -> Path:
        return self.db_path or (self.var_dir / "antifraud.db")

    @property
    def log_dir(self) -> Path:
        return self.var_dir / "logs"

    @property
    def effective_embedding_api_key(self) -> str | None:
        return self.embedding_api_key or self.llm_primary_api_key

    def analyzer_timeout(self, name: str) -> float:
        return float(getattr(self, f"timeout_{name}_s"))


_override: Settings | None = None


@lru_cache(maxsize=1)
def _load() -> Settings:
    return Settings()


def get_settings() -> Settings:
    return _override if _override is not None else _load()


@contextmanager
def override_settings(**values) -> Iterator[Settings]:
    """测试用：临时替换全局配置。`values` 以字段名给出，其余沿用默认值（不读 .env）。"""
    global _override
    previous = _override
    _override = Settings(_env_file=None, **values)
    try:
        yield _override
    finally:
        _override = previous

