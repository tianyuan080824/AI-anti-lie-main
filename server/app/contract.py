"""端云接口契约 v1 的 Pydantic 模型。

唯一权威是 `AI anti lie/docs/api-contract.md`；本文件与它逐节对应。
契约里的每个示例 JSON 都在 `AI anti lie/shared/contract-examples/` 下，
`tests/test_contract.py` 会逐个解析并序列化回去比对。

约定：
- 请求模型（客户端发来的）宽松：忽略不认识的字段；不认识的 `speaker` 按 `unknown` 处理；
  `source` / `origin` 只用于统计，接受任意字符串。
- 响应模型（服务端产出的）严格：枚举值只能取契约里列出的。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    field_validator,
    model_validator,
)


# ---------------------------------------------------------------------------
# 3. 枚举
# ---------------------------------------------------------------------------


class Verdict(StrEnum):
    HIGH_RISK = "high_risk"
    SUSPICIOUS = "suspicious"
    LOW_RISK = "low_risk"
    INCONCLUSIVE = "inconclusive"


class Scene(StrEnum):
    BRUSHING_REBATE = "brushing_rebate"
    INVESTMENT = "investment"
    IMPERSONATION_SERVICE = "impersonation_service"
    IMPERSONATION_ACQUAINTANCE = "impersonation_acquaintance"
    DEEPFAKE = "deepfake"
    OTHER = "other"
    NONE = "none"


class Stage(StrEnum):
    S1_LEAD = "s1_lead"
    S2_TRUST = "s2_trust"
    S3_MIGRATE = "s3_migrate"
    S4_INVEST = "s4_invest"
    S5_HARVEST = "s5_harvest"


class Intent(StrEnum):
    HIGH_RETURN_PROMISE = "high_return_promise"
    SMALL_REBATE = "small_rebate"
    INDUCE_DOWNLOAD = "induce_download"
    REQUEST_PAYMENT = "request_payment"
    URGENCY = "urgency"
    SECRECY = "secrecy"
    IDENTITY_PACKAGING = "identity_packaging"
    BLOCK_VERIFICATION = "block_verification"


class Speaker(StrEnum):
    OTHER = "other"
    SELF = "self"
    SYSTEM = "system"
    UNKNOWN = "unknown"


class AnalyzerName(StrEnum):
    SCRIPT = "script"
    LINK = "link"
    CASES = "cases"
    DEEPFAKE = "deepfake"
    SUMMARY = "summary"


#: 轮询响应里 `analyzers` 的固定顺序。
ANALYZER_ORDER: tuple[AnalyzerName, ...] = (
    AnalyzerName.SCRIPT,
    AnalyzerName.LINK,
    AnalyzerName.CASES,
    AnalyzerName.DEEPFAKE,
    AnalyzerName.SUMMARY,
)


class AnalyzerStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    SKIPPED = "skipped"
    FAILED = "failed"


class CheckStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class LinkRisk(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"


class LinkReason(StrEnum):
    BLACKLIST_HIT = "blacklist_hit"
    DOMAIN_AGE_LT_30D = "domain_age_lt_30d"
    BRAND_LOOKALIKE = "brand_lookalike"
    SUSPICIOUS_TLD = "suspicious_tld"
    IP_HOST = "ip_host"
    NONSTANDARD_PORT = "nonstandard_port"
    PUNYCODE = "punycode"
    SHORT_LINK = "short_link"
    REDIRECT_CHAIN = "redirect_chain"
    APK_DIRECT_LINK = "apk_direct_link"


class MediaKind(StrEnum):
    AUDIO = "audio"
    VIDEO = "video"
    IMAGE = "image"


class MediaLabel(StrEnum):
    LIKELY_SYNTHETIC = "likely_synthetic"
    UNCERTAIN = "uncertain"
    LIKELY_GENUINE = "likely_genuine"
    NOT_ANALYZED = "not_analyzed"


class DegradedFlag(StrEnum):
    BACKUP_LLM_USED = "backup_llm_used"
    LLM_UNAVAILABLE_RULES_ONLY = "llm_unavailable_rules_only"
    CROSS_CHECK_DISAGREED = "cross_check_disagreed"
    INTEL_UNAVAILABLE = "intel_unavailable"
    CASES_UNAVAILABLE = "cases_unavailable"
    DEEPFAKE_UNAVAILABLE = "deepfake_unavailable"
    PARTIAL_TIMEOUT = "partial_timeout"


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class EvidenceSource(StrEnum):
    LLM = "llm"
    RULE = "rule"


class ActionType(StrEnum):
    DIAL = "dial"
    OPEN_URL = "open_url"


class ErrorCode(StrEnum):
    INVALID_INPUT = "INVALID_INPUT"
    UNAUTHORIZED = "UNAUTHORIZED"
    NOT_FOUND = "NOT_FOUND"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    UNSUPPORTED_MEDIA = "UNSUPPORTED_MEDIA"
    RATE_LIMITED = "RATE_LIMITED"
    INTERNAL = "INTERNAL"
    UNAVAILABLE = "UNAVAILABLE"


class FeedbackLabel(StrEnum):
    SCAM = "scam"
    NOT_SCAM = "not_scam"
    UNSURE = "unsure"


def ordered_flags(*groups: Any) -> list[DegradedFlag]:
    """把若干组降级标记合并去重，按契约枚举顺序输出。"""
    seen: set[DegradedFlag] = set()
    for group in groups:
        for flag in group or ():
            seen.add(DegradedFlag(flag))
    return [f for f in DegradedFlag if f in seen]


# ---------------------------------------------------------------------------
# 基类
# ---------------------------------------------------------------------------


class RequestModel(BaseModel):
    """客户端发来的对象：忽略不认识的字段。"""

    model_config = ConfigDict(extra="ignore")


class ResponseModel(BaseModel):
    """服务端产出的对象。"""

    model_config = ConfigDict(extra="ignore")


def _omit_none(value: Any) -> bool:
    return value is None


# ---------------------------------------------------------------------------
# 4.2 POST /v1/checks 请求
# ---------------------------------------------------------------------------


class ClientInfo(RequestModel):
    app_version: str | None = None
    os_version: str | None = None
    device_model: str | None = None
    rules_version: str | None = None


class ChatLine(RequestModel):
    line_id: str = Field(min_length=1, max_length=32)
    speaker: Speaker = Speaker.UNKNOWN
    text: str

    @field_validator("speaker", mode="before")
    @classmethod
    def _unknown_speaker(cls, value: Any) -> Any:
        if isinstance(value, str) and value not in Speaker._value2member_map_:
            return Speaker.UNKNOWN
        return value


class ChatItem(RequestModel):
    type: Literal["chat"] = "chat"
    #: `screenshot_ocr` / `pasted_text` / `shared_text`
    origin: str | None = None
    lines: list[ChatLine]


class UrlItem(RequestModel):
    type: Literal["url"] = "url"
    url: str = Field(min_length=1)
    #: `typed` / `shared` / `qrcode`
    origin: str | None = None


class MediaItem(RequestModel):
    """`audio` / `video` / `image`（P1）：引用 `POST /v1/media` 返回的 `media_id`。"""

    type: Literal["audio", "video", "image"]
    media_id: str = Field(min_length=1)
    #: 仅 `image`：敏感区域是否已在端侧涂黑。
    masked: bool | None = Field(default=None, exclude_if=_omit_none)


class UnknownItem(RequestModel):
    """不认识的 `type`：按兼容规则忽略，不报错。"""

    model_config = ConfigDict(extra="allow")
    type: str


def _item_tag(value: Any) -> str:
    kind = value.get("type") if isinstance(value, dict) else getattr(value, "type", None)
    if kind == "chat":
        return "chat"
    if kind == "url":
        return "url"
    if kind in ("audio", "video", "image"):
        return "media"
    return "unknown"


CheckItem = Annotated[
    Union[
        Annotated[ChatItem, Tag("chat")],
        Annotated[UrlItem, Tag("url")],
        Annotated[MediaItem, Tag("media")],
        Annotated[UnknownItem, Tag("unknown")],
    ],
    Discriminator(_item_tag),
]


class CheckRequest(RequestModel):
    client: ClientInfo | None = None
    #: `floating_ball` / `share` / `in_app` / `process_text`，只用于统计。
    source: str | None = None
    items: list[CheckItem]


class CheckCreated(ResponseModel):
    check_id: str
    status: CheckStatus = CheckStatus.QUEUED
    poll_after_ms: int


# ---------------------------------------------------------------------------
# 5. 报告对象
# ---------------------------------------------------------------------------


class Evidence(ResponseModel):
    line_id: str
    quote: str
    intent: Intent
    source: EvidenceSource
    explanation: str


class LinkIntel(ResponseModel):
    provider: str
    verdict: str


class LinkResult(ResponseModel):
    url: str
    final_url: str | None = None
    registrable_domain: str | None = None
    #: 请求里 url 条目的 `origin`；从聊天文本里提取的链接没有来源（契约未定义，见 ARCHITECTURE.md）。
    origin: str | None = None
    risk: LinkRisk
    reasons: list[LinkReason] = Field(default_factory=list)
    domain_age_days: int | None = None
    intel: LinkIntel | None = None


class MediaResult(ResponseModel):
    media_id: str
    kind: MediaKind
    label: MediaLabel
    spoof_score: float | None = None
    note: str | None = None


class SimilarCase(ResponseModel):
    case_id: str
    title: str
    scene: Scene
    snippet: str
    source_name: str
    source_url: str | None = None
    published_at: str | None = None
    similarity: float = Field(ge=0.0, le=1.0)
    synthetic: bool = False

    @model_validator(mode="after")
    def _synthetic_has_no_source(self) -> "SimilarCase":
        if self.synthetic and self.source_url is not None:
            raise ValueError("synthetic case must have source_url = null")
        return self


class ReportAction(ResponseModel):
    type: ActionType
    label: str
    value: str


class Report(ResponseModel):
    verdict: Verdict
    risk_score: int = Field(ge=0, le=100)
    confidence: Confidence
    scene: Scene
    stage: Stage | None
    headline: str
    summary: str
    next_move: str
    evidence: list[Evidence] = Field(default_factory=list)
    links: list[LinkResult] = Field(default_factory=list)
    media: list[MediaResult] = Field(default_factory=list)
    similar_cases: list[SimilarCase] = Field(default_factory=list)
    advice: list[str] = Field(default_factory=list)
    actions: list[ReportAction] = Field(default_factory=list)
    degraded: list[DegradedFlag] = Field(default_factory=list)
    disclaimer: str

    @model_validator(mode="after")
    def _stage_null_when_no_scene(self) -> "Report":
        if self.scene == Scene.NONE and self.stage is not None:
            raise ValueError("stage must be null when scene is none")
        return self


# ---------------------------------------------------------------------------
# 4.3 GET /v1/checks/{check_id}
# ---------------------------------------------------------------------------


class AnalyzerState(ResponseModel):
    name: AnalyzerName
    status: AnalyzerStatus
    #: 只有跑完（done / failed）的分析器才带耗时；没有时整个字段省略。
    elapsed_ms: int | None = Field(default=None, exclude_if=_omit_none)


class DerivedLine(ChatLine):
    """服务端从语音转写或图片 OCR 得到的行，`line_id` 以 `d` 开头。"""

    item_index: int


class ErrorBody(ResponseModel):
    code: ErrorCode
    message: str
    retryable: bool
    trace_id: str


class ErrorResponse(ResponseModel):
    error: ErrorBody


class CheckStatusResponse(ResponseModel):
    check_id: str
    status: CheckStatus
    poll_after_ms: int
    analyzers: list[AnalyzerState]
    derived_lines: list[DerivedLine] = Field(default_factory=list)
    report: Report | None = None
    #: 任务级失败时为第 7 节的错误对象（即 `{"code", "message", "retryable", "trace_id"}`）。
    error: ErrorBody | None = None


# ---------------------------------------------------------------------------
# 4.1 / 4.4 / 4.5 / 4.6
# ---------------------------------------------------------------------------


class MediaUploadResponse(ResponseModel):
    media_id: str
    kind: MediaKind
    size_bytes: int
    expires_at: str


class FeedbackRequest(RequestModel):
    label: FeedbackLabel
    note: str | None = Field(default=None, max_length=200)


class RulesNormalize(ResponseModel):
    phrases: dict[str, str] = Field(default_factory=dict)
    strip: list[str] = Field(default_factory=list)


class Rule(ResponseModel):
    id: str
    pattern: str
    scene: Scene
    intent: Intent
    weight: float
    title: str


class RulesFile(ResponseModel):
    version: str
    normalize: RulesNormalize
    rules: list[Rule]


class HealthResponse(ResponseModel):
    status: str = "ok"
    version: str
    rules_version: str
    mock: bool
    llm: Literal["ok", "down", "unknown"]
    db: Literal["ok", "down"]
