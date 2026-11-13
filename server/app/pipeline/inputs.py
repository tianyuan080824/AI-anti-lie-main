"""校验请求并构造分析器输入：数量和长度限制（契约第 1 节）+ 二次脱敏（契约第 2 节）。"""

from __future__ import annotations

import re
from typing import Any

from app.analyzers.base import AnalysisInput, InputLine, MediaInput, UrlInput
from app.config import Settings
from app.contract import ChatItem, CheckRequest, ErrorCode, MediaItem, MediaKind, UrlItem
from app.errors import ApiError
from app.redact import redact_text


#: 服务端派生行的编号（契约 4.3：转写和 OCR 得到的行以 d 开头，如 d2-0）
_DERIVED_ID = re.compile(r"d\d+-\d+")


def _invalid(message: str) -> ApiError:
    return ApiError(ErrorCode.INVALID_INPUT, message)


def build_input(
    req: CheckRequest, *, check_id: str, trace_id: str, settings: Settings
) -> AnalysisInput:
    """校验 `req`，返回二次脱敏后的 `AnalysisInput`。超限时抛 `ApiError(INVALID_INPUT)`。

    不认识的条目类型按兼容规则忽略，也不入库（它们可能带着未脱敏的内容）。
    """
    if not req.items:
        raise _invalid("至少需要提交一条内容")
    if len(req.items) > settings.limit_items:
        raise _invalid(f"一次最多提交 {settings.limit_items} 条内容")

    lines: list[InputLine] = []
    urls: list[UrlInput] = []
    media: list[MediaInput] = []
    stored_items: list[dict[str, Any]] = []
    seen_line_ids: set[str] = set()
    seen_media_ids: set[str] = set()

    for index, item in enumerate(req.items):
        if isinstance(item, ChatItem):
            stored_lines = []
            for line in item.lines:
                if len(line.text) > settings.limit_line_chars:
                    raise _invalid(f"单行文字不能超过 {settings.limit_line_chars} 字")
                if _DERIVED_ID.match(line.line_id):
                    raise _invalid("行编号不能是 d<数字>-<数字> 的形式（留给服务端转写和识别出的行）")
                if line.line_id in seen_line_ids:
                    raise _invalid("同一次提交里的行编号不能重复")
                seen_line_ids.add(line.line_id)
                text = redact_text(line.text)
                lines.append(
                    InputLine(
                        line_id=line.line_id,
                        speaker=line.speaker,
                        text=text,
                        item_index=index,
                        origin=item.origin,
                    )
                )
                stored_lines.append({"line_id": line.line_id, "speaker": line.speaker.value, "text": text})
                if len(lines) > settings.limit_lines:
                    raise _invalid(f"一次最多提交 {settings.limit_lines} 行文字")
            stored_items.append({"type": "chat", "origin": item.origin, "lines": stored_lines})
        elif isinstance(item, UrlItem):
            url = item.url.strip()
            if not url:
                raise _invalid("链接不能为空")
            if len(url) > settings.limit_url_chars:
                raise _invalid(f"链接不能超过 {settings.limit_url_chars} 个字符")
            urls.append(UrlInput(url=url, origin=item.origin, item_index=index))
            if len(urls) > settings.limit_urls:
                raise _invalid(f"一次最多提交 {settings.limit_urls} 个链接")
            stored_items.append({"type": "url", "url": url, "origin": item.origin})
        elif isinstance(item, MediaItem):
            if item.media_id in seen_media_ids:
                raise _invalid("同一个媒体不能在一次提交里出现两次")
            seen_media_ids.add(item.media_id)
            media.append(
                MediaInput(media_id=item.media_id, kind=MediaKind(item.type), item_index=index, masked=item.masked)
            )
            stored = {"type": item.type, "media_id": item.media_id}
            if item.masked is not None:
                stored["masked"] = item.masked
            stored_items.append(stored)
        # UnknownItem：忽略

    stored_request: dict[str, Any] = {
        "client": req.client.model_dump(mode="json") if req.client else None,
        "source": req.source,
        "items": stored_items,
    }
    return AnalysisInput(
        check_id=check_id,
        trace_id=trace_id,
        lines=tuple(lines),
        urls=tuple(urls),
        media=tuple(media),
        source=req.source,
        client=req.client,
        request=stored_request,
    )
