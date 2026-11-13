"""评测数据：`eval/data/*.jsonl`，每行一条对话。格式见 README.md。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.contract import Scene, Speaker
from app.paths import SERVER_DIR

DATA_DIR = SERVER_DIR / "eval" / "data"
LABELS = ("fraud", "normal")


@dataclass(frozen=True)
class EvalSample:
    id: str
    label: str
    scene: str
    source: str
    lines: tuple[tuple[str, str], ...]
    urls: tuple[str, ...] = ()
    file: str = ""
    line_no: int = 0

    @property
    def positive(self) -> bool:
        return self.label == "fraud"

    def to_request(self) -> dict[str, Any]:
        """转成与 POST /v1/checks 同构的请求体。"""
        items: list[dict[str, Any]] = []
        if self.lines:
            items.append(
                {
                    "type": "chat",
                    "origin": "pasted_text",
                    "lines": [{"line_id": f"0-{i}", "speaker": sp, "text": text} for i, (sp, text) in enumerate(self.lines)],
                }
            )
        items.extend({"type": "url", "url": u, "origin": "typed"} for u in self.urls)
        return {"client": {"app_version": "eval"}, "source": "eval", "items": items}


@dataclass
class LoadResult:
    samples: list[EvalSample] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _parse_line(data: Any) -> tuple[str, str]:
    if isinstance(data, str):
        return (Speaker.UNKNOWN.value, data)
    if isinstance(data, dict) and isinstance(data.get("text"), str):
        speaker = data.get("speaker", Speaker.UNKNOWN.value)
        if speaker not in Speaker._value2member_map_:
            raise ValueError(f"speaker 不认识：{speaker!r}")
        return (speaker, data["text"])
    raise ValueError("lines 的每一项要么是字符串，要么是 {speaker, text}")


def parse_sample(data: Any, file: str = "", line_no: int = 0) -> EvalSample:
    if not isinstance(data, dict):
        raise ValueError("每行必须是一个 JSON 对象")
    unknown = set(data) - {"id", "label", "scene", "source", "lines", "urls", "note"}
    if unknown:
        raise ValueError(f"不认识的字段：{', '.join(sorted(unknown))}")
    sid = data.get("id")
    if not isinstance(sid, str) or not sid.strip():
        raise ValueError("缺少 id")
    label = data.get("label")
    if label not in LABELS:
        raise ValueError(f"label 必须是 {' / '.join(LABELS)}")
    scene = data.get("scene", Scene.NONE.value if label == "normal" else Scene.OTHER.value)
    if scene not in Scene._value2member_map_:
        raise ValueError(f"scene 不认识：{scene!r}")
    source = data.get("source")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("缺少 source（数据来源，如 synthetic / team_v1 / tft5）")
    raw_lines = data.get("lines", [])
    urls = data.get("urls", [])
    if not isinstance(raw_lines, list) or not isinstance(urls, list) or not all(isinstance(u, str) for u in urls):
        raise ValueError("lines 和 urls 必须是数组")
    lines = tuple(_parse_line(x) for x in raw_lines)
    if not lines and not urls:
        raise ValueError("lines 和 urls 不能都为空")
    return EvalSample(
        id=sid.strip(),
        label=label,
        scene=scene,
        source=source.strip(),
        lines=lines,
        urls=tuple(urls),
        file=file,
        line_no=line_no,
    )


def load_samples(files: list[Path]) -> LoadResult:
    """逐行解析；不合格的行记成「文件:行号 原因」并跳过。"""
    out = LoadResult()
    seen: dict[str, str] = {}
    for path in files:
        with path.open("r", encoding="utf-8-sig") as fh:
            for no, raw in enumerate(fh, 1):
                text = raw.strip()
                if not text or text.startswith("//"):
                    continue
                where = f"{path.name}:{no}"
                try:
                    sample = parse_sample(json.loads(text), path.name, no)
                except json.JSONDecodeError as exc:
                    out.errors.append(f"{where}  不是合法的 JSON（{exc.msg}）")
                    continue
                except ValueError as exc:
                    out.errors.append(f"{where}  {exc}")
                    continue
                if sample.id in seen:
                    out.errors.append(f"{where}  id {sample.id} 重复（已在 {seen[sample.id]} 出现）")
                    continue
                seen[sample.id] = where
                out.samples.append(sample)
    return out


def find_data_files(paths: list[Path] | None = None) -> list[Path]:
    """参数可以是文件或目录；缺省为 eval/data/*.jsonl。"""
    if not paths:
        return sorted(DATA_DIR.glob("*.jsonl"))
    files: list[Path] = []
    for p in paths:
        files.extend(sorted(p.glob("*.jsonl")) if p.is_dir() else [p])
    return files
