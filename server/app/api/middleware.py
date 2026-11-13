"""HTTP 中间件：trace_id、鉴权、请求体大小、访问日志。

鉴权放在中间件而不是依赖里，是为了让「令牌错误」先于「请求体格式错误」返回 401。
"""

from __future__ import annotations

import hmac
import logging
import time

from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from app.contract import ErrorCode
from app.errors import error_response
from app.ids import new_trace_id
from app.logs import log_event

logger = logging.getLogger("app.http")


class RequestMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        trace_id = new_trace_id()
        request.state.trace_id = trace_id
        started = time.perf_counter()
        response = await self._handle(request, call_next, trace_id)
        response.headers.setdefault("X-Trace-Id", trace_id)
        log_event(
            logger,
            "http.request",
            trace_id=trace_id,
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
        return response

    async def _handle(self, request: Request, call_next: RequestResponseEndpoint, trace_id: str) -> Response:
        settings = request.app.state.settings
        if request.url.path.startswith("/v1/"):
            header = request.headers.get("authorization", "")
            scheme, _, token = header.partition(" ")
            if scheme.lower() != "bearer" or not hmac.compare_digest(
                token.strip().encode("utf-8"), settings.app_token.encode("utf-8")
            ):
                return error_response(ErrorCode.UNAUTHORIZED, trace_id)
            if request.method in ("POST", "PUT", "PATCH"):
                length = request.headers.get("content-length")
                if length is None and _is_media(request.url.path):
                    # 不带长度的分块上传会先被整个读进临时文件才轮到大小检查，直接拒绝
                    return error_response(ErrorCode.INVALID_INPUT, trace_id, "上传文件时请带上 Content-Length")
                if length is not None:
                    try:
                        too_big = int(length) > _body_limit(settings, request.url.path)
                    except ValueError:
                        too_big = True
                    if too_big and _is_media(request.url.path):
                        return error_response(ErrorCode.PAYLOAD_TOO_LARGE, trace_id, "文件太大")
                    if too_big:
                        return error_response(ErrorCode.INVALID_INPUT, trace_id, "提交的内容太多，请减少截图或文字后重试")
        return await call_next(request)


#: 媒体上传的表单头、分隔符等额外开销
_MEDIA_FORM_OVERHEAD = 64 * 1024


def _is_media(path: str) -> bool:
    return path.rstrip("/") == "/v1/media"


def _body_limit(settings, path: str) -> int:
    """媒体上传按最大的单类型上限放行，具体类型的上限由上传接口自己卡；其余请求 1 MB。"""
    if _is_media(path):
        return max(settings.limit_image_bytes, settings.limit_audio_bytes, settings.limit_video_bytes) + _MEDIA_FORM_OVERHEAD
    return settings.limit_body_bytes


def install_middleware(app: FastAPI) -> None:
    app.add_middleware(RequestMiddleware)
