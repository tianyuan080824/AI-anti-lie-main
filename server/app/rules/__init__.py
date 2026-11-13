"""规则集（`AI anti lie/shared/rules.json`，格式见契约 4.5）：加载、归一化、匹配。

归一化和 Android 端一致：Unicode NFKC → 去掉 `normalize.strip` 里的字符 → `normalize.phrases` 短语替换
（长的短语优先，只做短语级替换）。匹配在归一化后的文本上做，命中位置再映射回原文，
所以 `RuleHit.quote` 一定是这一行原文的逐字子串（契约第 5 节对 evidence 的保证）。
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from app.contract import Intent, Rule, RulesFile, Scene
from app.paths import RULES_FILE


@dataclass(frozen=True)
class RulesMeta:
    version: str
    count: int
    path: Path
    error: str | None = None


def load_rules_meta(path: Path = RULES_FILE) -> RulesMeta:
    if not path.exists():
        return RulesMeta(version="none", count=0, path=path, error="missing")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("rules", []), list):
            raise ValueError("rules.json must be an object with a rules list")
        return RulesMeta(version=str(data.get("version", "none")), count=len(data.get("rules", [])), path=path)
    except (OSError, ValueError) as exc:
        return RulesMeta(version="none", count=0, path=path, error=type(exc).__name__)


@dataclass(frozen=True)
class RuleHit:
    rule_id: str
    line_id: str
    quote: str
    intent: Intent
    scene: Scene
    weight: float
    title: str

    def to_dict(self) -> dict:
        return {k: str(v) if k in ("intent", "scene") else v for k, v in asdict(self).items()}

    @classmethod
    def from_dict(cls, data: dict) -> "RuleHit":
        return cls(
            rule_id=str(data["rule_id"]),
            line_id=str(data["line_id"]),
            quote=str(data["quote"]),
            intent=Intent(data["intent"]),
            scene=Scene(data["scene"]),
            weight=float(data["weight"]),
            title=str(data["title"]),
        )


# 契约 4.5：只用 Kotlin 和 Python 都支持的公共子集
_FORBIDDEN_SYNTAX = re.compile(r"\(\?<[=!]|\(\?P?<[A-Za-z_]|\(\?[aiLmsux-]+[):]|[*+?}]\+")


def pattern_problem(pattern: str) -> str | None:
    """规则正则是否用了契约禁止的语法（后行断言、命名分组、内联标志、占有量词）。"""
    if _FORBIDDEN_SYNTAX.search(pattern):
        return "uses syntax outside the Kotlin/Python common subset"
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        return f"invalid: {exc}"
    if compiled.match(""):
        return "matches the empty string"
    return None


class Normalizer:
    def __init__(self, phrases: dict[str, str], strip: Iterable[str]):
        self.strip = frozenset(strip)
        nfkc = {unicodedata.normalize("NFKC", k): v for k, v in phrases.items() if k}
        self.phrases = sorted(nfkc.items(), key=lambda kv: -len(kv[0]))

    def normalize(self, text: str) -> str:
        return self.normalize_with_map(text)[0]

    def normalize_with_map(self, text: str) -> tuple[str, list[tuple[int, int]]]:
        """返回归一化文本，以及它每个字符对应的原文区间 [start, end)。"""
        chars: list[str] = []
        spans: list[tuple[int, int]] = []
        for i, ch in enumerate(text):
            for c in unicodedata.normalize("NFKC", ch):
                if c in self.strip or ch in self.strip:
                    continue
                chars.append(c)
                spans.append((i, i + 1))
        source = "".join(chars)
        out: list[str] = []
        out_spans: list[tuple[int, int]] = []
        j = 0
        while j < len(source):
            for key, value in self.phrases:
                if source.startswith(key, j):
                    span = (spans[j][0], spans[j + len(key) - 1][1])
                    out.extend(value)
                    out_spans.extend([span] * len(value))
                    j += len(key)
                    break
            else:
                out.append(source[j])
                out_spans.append(spans[j])
                j += 1
        return "".join(out), out_spans


def _widen_to_placeholders(text: str, start: int, end: int) -> tuple[int, int]:
    """命中边界落在 `<AMOUNT:200>` 这类占位符中间时，扩到整个占位符。"""
    open_before = text.rfind("<", 0, start)
    if open_before != -1 and text.find(">", open_before, start) == -1 and text.find(">", start) != -1:
        start = open_before
    last_open = text.rfind("<", start, end)
    if last_open != -1 and text.find(">", last_open, end) == -1:
        close = text.find(">", end)
        if close != -1:
            end = close + 1
    return start, end


class RuleSet:
    def __init__(self, data: RulesFile):
        self.version = data.version
        self.rules: list[Rule] = list(data.rules)
        self.normalizer = Normalizer(data.normalize.phrases, data.normalize.strip)
        self._compiled = [(rule, re.compile(rule.pattern)) for rule in self.rules]

    @classmethod
    def load(cls, path: Path = RULES_FILE) -> "RuleSet":
        return cls(RulesFile.model_validate_json(path.read_text(encoding="utf-8")))

    def match_text(self, text: str) -> list[tuple[Rule, str]]:
        """一段文本命中的规则和原文引文（每条规则只取第一处）。"""
        norm, spans = self.normalizer.normalize_with_map(text)
        hits: list[tuple[Rule, str]] = []
        for rule, regex in self._compiled:
            m = regex.search(norm)
            if m is None or m.end() == m.start():
                continue
            start, end = spans[m.start()][0], spans[m.end() - 1][1]
            start, end = _widen_to_placeholders(text, start, end)
            hits.append((rule, text[start:end]))
        return hits

    def match_lines(self, lines: Iterable) -> list[RuleHit]:
        """`lines` 是带 `line_id`、`text` 的对象（`InputLine`）。"""
        hits: list[RuleHit] = []
        for line in lines:
            for rule, quote in self.match_text(line.text):
                hits.append(
                    RuleHit(
                        rule_id=rule.id,
                        line_id=line.line_id,
                        quote=quote,
                        intent=rule.intent,
                        scene=rule.scene,
                        weight=rule.weight,
                        title=rule.title,
                    )
                )
        return hits


@lru_cache(maxsize=4)
def _cached_file(path: Path, mtime_ns: int) -> RulesFile:
    return RulesFile.model_validate_json(path.read_text(encoding="utf-8"))


def load_rules_file(path: Path = RULES_FILE) -> RulesFile:
    """规则集原样（按契约 4.5 校验过），给 `GET /v1/rules` 下发。文件改动后重新读取。"""
    return _cached_file(path, path.stat().st_mtime_ns)


@lru_cache(maxsize=4)
def _cached(path: Path, mtime_ns: int) -> RuleSet:
    return RuleSet.load(path)


def get_ruleset(path: Path = RULES_FILE) -> RuleSet:
    """进程内缓存；文件改动后（mtime 变化）重新加载。"""
    return _cached(path, path.stat().st_mtime_ns)
