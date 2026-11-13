"""对外接口：发起鉴定、查询进度与结果、健康检查（P0），用户反馈、规则集下发（P1）。"""

from __future__ import annotations

import logging
import math
import re

from fastapi import APIRouter, Header, Request, Response, status
from fastapi.responses import JSONResponse

from app import __version__
from app.contract import (
    CheckCreated,
    CheckRequest,
    CheckStatusResponse,
    ErrorCode,
    ErrorResponse,
    FeedbackRequest,
    HealthResponse,
    RulesFile,
)
from app.errors import ApiError, trace_id_of
from app.logs import log_event
from app.redact import redact_text
from app.rules import load_rules_file

logger = logging.getLogger("app.api")

router = APIRouter(prefix="/v1")

_DEVICE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

ERRORS = {
    400: {"model": ErrorResponse},
    401: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    429: {"model": ErrorResponse},
    500: {"model": ErrorResponse},
}


def _device_id(value: str | None, *, required: bool) -> str | None:
    if value is None or not value.strip():
        if required:
            raise ApiError(ErrorCode.INVALID_INPUT, "缺少设备标识 X-Device-Id")
        return None
    value = value.strip()
    if not _DEVICE_ID.match(value):
        raise ApiError(ErrorCode.INVALID_INPUT, "设备标识 X-Device-Id 格式不正确")
    return value


@router.post(
    "/checks",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CheckCreated,
    responses=ERRORS,
    summary="发起鉴定",
)
async def create_check(
    body: CheckRequest,
    request: Request,
    x_device_id: str | None = Header(default=None),
) -> CheckCreated:
    device_id = _device_id(x_device_id, required=True)
    allowed, wait = request.app.state.rate_limiter.acquire(device_id)
    if not allowed:
        raise ApiError(ErrorCode.RATE_LIMITED, headers={"Retry-After": str(max(1, math.ceil(wait)))})
    return await request.app.state.checks.submit(body, device_id=device_id, trace_id=trace_id_of(request))


@router.get(
    "/checks/{check_id}",
    response_model=CheckStatusResponse,
    response_model_exclude_none=False,
    responses=ERRORS,
    summary="查询进度与结果",
)
async def get_check(check_id: str, request: Request) -> CheckStatusResponse:
    result = await request.app.state.checks.get(check_id)
    if result is None:
        raise ApiError(ErrorCode.NOT_FOUND)
    return result


@router.get("/health", response_model=HealthResponse, responses={401: {"model": ErrorResponse}}, summary="健康检查")
async def health(request: Request) -> HealthResponse:
    state = request.app.state
    db_ok = await state.services.db.ping()
    return HealthResponse(
        status="ok",
        version=__version__,
        rules_version=state.rules_meta.version,
        mock=state.settings.mock_mode,
        llm=state.services.llm_health.status,
        db="ok" if db_ok else "down",
    )


@router.post(
    "/checks/{check_id}/feedback",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses=ERRORS,
    summary="用户反馈（P1）",
)
async def post_feedback(
    check_id: str,
    body: FeedbackRequest,
    request: Request,
    x_device_id: str | None = Header(default=None),
) -> Response:
    device_id = _device_id(x_device_id, required=False)
    store = request.app.state.store
    record = await store.get(check_id)
    if record is None:
        raise ApiError(ErrorCode.NOT_FOUND)
    note = body.note.strip() if body.note else None
    note = redact_text(note) if note else None
    await store.save_feedback(record, device_id=device_id, label=body.label.value, note=note)
    log_event(
        logger,
        "feedback.saved",
        trace_id=trace_id_of(request),
        check_id=check_id,
        label=body.label.value,
        has_note=note is not None,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _etag_matches(header: str | None, etag: str) -> bool:
    """`If-None-Match` 是否命中（弱比较：`W/` 前缀不计；支持逗号分隔多个值和 `*`）。"""
    if not header:
        return False
    for raw in header.split(","):
        tag = raw.strip()
        if tag == "*":
            return True
        if tag.startswith("W/"):
            tag = tag[2:]
        if tag == etag:
            return True
    return False


@router.get(
    "/rules",
    response_model=RulesFile,
    responses={**ERRORS, 304: {"description": "规则集没有更新"}, 503: {"model": ErrorResponse}},
    summary="规则集下发（P1）",
)
async def get_rules(request: Request, if_none_match: str | None = Header(default=None)) -> Response:
    try:
        rules = load_rules_file()
    except (OSError, ValueError) as exc:
        log_event(logger, "rules.unavailable", logging.WARNING, error=type(exc).__name__)
        raise ApiError(ErrorCode.UNAVAILABLE, "规则集暂时不可用，请继续使用 App 内置的规则")
    etag = f'"{rules.version}"'
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if _etag_matches(if_none_match, etag):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    return JSONResponse(rules.model_dump(mode="json"), headers=headers)
