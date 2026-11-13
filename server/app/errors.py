"""统一错误格式（契约第 7 节）。

所有错误响应都是 `{"error": {"code", "message", "retryable", "trace_id"}}`。
`message` 是可以直接展示给用户的中文句子，不含堆栈、密钥和正文内容。
"""

from __future__ import annotations

import logging
import traceback

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.contract import ErrorBody, ErrorCode, ErrorResponse
from app.ids import new_trace_id

logger = logging.getLogger("app.errors")

HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.INVALID_INPUT: 400,
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.PAYLOAD_TOO_LARGE: 413,
    ErrorCode.UNSUPPORTED_MEDIA: 415,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.INTERNAL: 500,
    ErrorCode.UNAVAILABLE: 503,
}

RETRYABLE: frozenset[ErrorCode] = frozenset(
    {ErrorCode.RATE_LIMITED, ErrorCode.INTERNAL, ErrorCode.UNAVAILABLE}
)

DEFAULT_MESSAGE: dict[ErrorCode, str] = {
    ErrorCode.INVALID_INPUT: "提交的内容格式不正确",
    ErrorCode.UNAUTHORIZED: "访问令牌缺失或错误，请检查 App 里的访问令牌设置",
    ErrorCode.NOT_FOUND: "鉴定记录不存在或已过期",
    ErrorCode.PAYLOAD_TOO_LARGE: "文件太大或太长",
    ErrorCode.UNSUPPORTED_MEDIA: "不支持这种文件类型",
    ErrorCode.RATE_LIMITED: "请求太频繁，请稍后再试",
    ErrorCode.INTERNAL: "服务出现内部错误，请稍后重试",
    ErrorCode.UNAVAILABLE: "服务暂时不可用，请稍后重试",
}


class ApiError(Exception):
    """在任何处理函数里抛出，都会被转换成契约第 7 节的错误响应。"""

    def __init__(
        self,
        code: ErrorCode,
        message: str | None = None,
        *,
        status_code: int | None = None,
        retryable: bool | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.code = code
        self.message = message or DEFAULT_MESSAGE[code]
        self.status_code = status_code or HTTP_STATUS[code]
        self.retryable = (code in RETRYABLE) if retryable is None else retryable
        self.headers = headers or {}
        super().__init__(self.message)


def error_body(
    code: ErrorCode,
    trace_id: str,
    message: str | None = None,
    retryable: bool | None = None,
) -> ErrorBody:
    return ErrorBody(
        code=code,
        message=message or DEFAULT_MESSAGE[code],
        retryable=(code in RETRYABLE) if retryable is None else retryable,
        trace_id=trace_id,
    )


def error_response(
    code: ErrorCode,
    trace_id: str,
    message: str | None = None,
    *,
    status_code: int | None = None,
    retryable: bool | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = ErrorResponse(error=error_body(code, trace_id, message, retryable))
    all_headers = {"X-Trace-Id": trace_id, **(headers or {})}
    return JSONResponse(
        status_code=status_code or HTTP_STATUS[code],
        content=body.model_dump(mode="json"),
        headers=all_headers,
    )


def trace_id_of(request: Request) -> str:
    trace_id = getattr(request.state, "trace_id", None)
    if not trace_id:
        trace_id = new_trace_id()
        request.state.trace_id = trace_id
    return trace_id


_REASON = {
    "missing": "缺失",
    "json_invalid": "不是合法的 JSON",
    "string_too_short": "不能为空",
    "string_too_long": "太长",
    "string_type": "应为字符串",
    "list_type": "应为数组",
    "int_type": "应为整数",
    "bool_type": "应为布尔值",
    "model_type": "应为对象",
    "dict_type": "应为对象",
    "enum": "取值不在允许范围内",
    "literal_error": "取值不在允许范围内",
}


def describe_validation_error(exc: RequestValidationError) -> str:
    """把第一条校验错误转成中文句子。只用字段路径和错误类型，不回显用户提交的内容。"""
    errors = exc.errors()
    if not errors:
        return DEFAULT_MESSAGE[ErrorCode.INVALID_INPUT]
    first = errors[0]
    loc = [str(part) for part in first.get("loc", ()) if part not in ("body",)]
    path = ".".join(loc)
    reason = _REASON.get(first.get("type", ""), "取值不合法")
    if first.get("type") == "json_invalid":
        return "请求内容不是合法的 JSON"
    if not path:
        return f"请求内容{reason}"
    return f"请求格式不正确：{path} {reason}"


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return error_response(
            exc.code,
            trace_id_of(request),
            exc.message,
            status_code=exc.status_code,
            retryable=exc.retryable,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(
            ErrorCode.INVALID_INPUT, trace_id_of(request), describe_validation_error(exc)
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        mapping = {
            400: ErrorCode.INVALID_INPUT,
            401: ErrorCode.UNAUTHORIZED,
            404: ErrorCode.NOT_FOUND,
            405: ErrorCode.INVALID_INPUT,
            413: ErrorCode.PAYLOAD_TOO_LARGE,
            415: ErrorCode.UNSUPPORTED_MEDIA,
            429: ErrorCode.RATE_LIMITED,
            503: ErrorCode.UNAVAILABLE,
        }
        code = mapping.get(exc.status_code, ErrorCode.INTERNAL)
        message = "接口不存在" if exc.status_code == 404 else None
        if exc.status_code == 405:
            message = "不支持这种请求方法"
        return error_response(
            code, trace_id_of(request), message, status_code=exc.status_code
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        trace_id = trace_id_of(request)
        # 只记异常类型和调用栈，不记异常消息（可能带有正文）。
        logger.error(
            "request.unhandled_exception",
            extra={
                "fields": {
                    "trace_id": trace_id,
                    "error": type(exc).__name__,
                    "stack": "".join(traceback.format_tb(exc.__traceback__))[-4000:],
                }
            },
        )
        return error_response(ErrorCode.INTERNAL, trace_id)
