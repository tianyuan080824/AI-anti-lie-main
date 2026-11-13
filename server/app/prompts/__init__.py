"""提示词：每个提示词是本目录下一个带版本号的独立文件（`<名字>_<版本>.md`），每次调用记录所用版本。"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent

SCRIPT_PROMPT_VERSION = "script_v1"


@dataclass(frozen=True)
class Prompt:
    version: str
    text: str


@lru_cache(maxsize=8)
def load_prompt(version: str) -> Prompt:
    return Prompt(version=version, text=(PROMPTS_DIR / f"{version}.md").read_text(encoding="utf-8"))
