"""固定文案（`templates.yaml`）：headline / summary / next_move / advice / actions / disclaimer。"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

from app.contract import Intent, ReportAction, Scene, Verdict

TEMPLATES_FILE = Path(__file__).resolve().parent / "templates.yaml"


class Templates(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scene_names: dict[Scene, str]
    headline: dict[Verdict, dict[str, str]]
    summary: dict[Verdict, dict[str, str]]
    rules_only_note: str
    next_move: dict[Scene, str]
    advice: dict[Verdict, dict[str, list[str]]]
    actions: dict[str, list[ReportAction]]
    intent_explanations: dict[Intent, str]
    disclaimer: str

    @staticmethod
    def _pick(table: dict, verdict: Verdict, scene: Scene):
        by_scene = table[verdict]
        return by_scene.get(scene.value, by_scene["default"])

    def headline_for(self, verdict: Verdict, scene: Scene) -> str:
        return self._pick(self.headline, verdict, scene)

    def summary_for(self, verdict: Verdict, scene: Scene) -> str:
        return self._pick(self.summary, verdict, scene)

    def advice_for(self, verdict: Verdict, scene: Scene) -> list[str]:
        return list(self._pick(self.advice, verdict, scene))

    def actions_for(self, verdict: Verdict) -> list[ReportAction]:
        return list(self.actions.get(verdict.value, self.actions["default"]))

    def next_move_for(self, scene: Scene) -> str:
        return self.next_move.get(scene, "")


def load_templates(path: Path = TEMPLATES_FILE) -> Templates:
    return _load(path, path.stat().st_mtime_ns)


@lru_cache(maxsize=4)
def _load(path: Path, mtime_ns: int) -> Templates:
    t = Templates.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    for verdict in Verdict:
        for table in (t.headline, t.summary, t.advice):
            if "default" not in table.get(verdict, {}):
                raise ValueError(f"templates.yaml: missing default for {verdict.value}")
    if set(Intent) - set(t.intent_explanations):
        raise ValueError("templates.yaml: intent_explanations must cover every intent")
    return t


# 契约第 6 节第 4 条：任何文案不得出现「安全」「可以放心」一类的保证。
# 大模型写的 summary / next_move / explanation 命中这些说法时整段换成模板。
# 「安全」「放心」单字就算：宁可误换成模板，也不让「内容安全」「你放心吧」这类变体漏过去。
_ASSURANCE = re.compile(r"安全|放心|无风险|没有风险|不存在风险|没有诈骗|不是诈骗|不是骗")


def has_assurance(text: str) -> bool:
    return bool(_ASSURANCE.search(text))
