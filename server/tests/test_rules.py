"""规则集 shared/rules.json：格式、覆盖面、每条规则的命中/不命中样例、归一化、引文逐字。"""

from __future__ import annotations

import json
from collections import Counter

import pytest

from app.analyzers.base import InputLine
from app.contract import Intent, RulesFile, Scene, Speaker
from app.paths import SHARED_DIR
from app.rules import Normalizer, RuleSet, get_ruleset, load_rules_meta, pattern_problem

RULES = RulesFile.model_validate_json((SHARED_DIR / "rules.json").read_text(encoding="utf-8"))
SAMPLES = json.loads((SHARED_DIR / "rules-samples.json").read_text(encoding="utf-8"))
RULESET = RuleSet(RULES)


def test_meta_reports_version_and_count():
    meta = load_rules_meta()
    assert meta.error is None and meta.version == RULES.version and meta.count == len(RULES.rules)


def test_at_least_60_rules_with_unique_ids():
    ids = [r.id for r in RULES.rules]
    assert len(ids) >= 60 and len(set(ids)) == len(ids)


def test_covers_both_main_scenes_and_all_intents():
    scenes = Counter(r.scene for r in RULES.rules)
    assert scenes[Scene.BRUSHING_REBATE] >= 10 and scenes[Scene.INVESTMENT] >= 10
    assert {r.intent for r in RULES.rules} == set(Intent)


@pytest.mark.parametrize("rule", RULES.rules, ids=lambda r: r.id)
def test_pattern_in_common_regex_subset(rule):
    assert pattern_problem(rule.pattern) is None
    assert rule.weight > 0 and rule.title


@pytest.mark.parametrize(
    "pattern",
    [r"(?<=a)b", r"(?<!a)b", r"(?P<x>a)", r"(?<x>a)", r"(?i)abc", r"a++", r"a*+", r"(?s:a.b)", r"a?"],
)
def test_forbidden_syntax_detected(pattern):
    assert pattern_problem(pattern) is not None


def test_every_rule_has_samples():
    assert set(SAMPLES["rules"]) == {r.id for r in RULES.rules}


@pytest.mark.parametrize("rule", RULES.rules, ids=lambda r: r.id)
def test_rule_hit_and_miss_samples(rule):
    sample = SAMPLES["rules"][rule.id]
    assert sample["hit"] and sample["miss"]
    for text in sample["hit"]:
        hits = {r.id: q for r, q in RULESET.match_text(text)}
        assert rule.id in hits, f"{rule.id} should hit {text!r}"
        assert hits[rule.id] in text
    for text in sample["miss"]:
        assert rule.id not in {r.id for r, _ in RULESET.match_text(text)}, f"{rule.id} should miss {text!r}"


@pytest.mark.parametrize("case", SAMPLES["normalize"], ids=lambda c: c["input"])
def test_normalize_samples(case):
    assert RULESET.normalizer.normalize(case["input"]) == case["expected"]


def test_phrase_replacement_is_phrase_level():
    n = Normalizer({"威信": "微信"}, [])
    assert n.normalize("不要威胁我") == "不要威胁我"
    assert n.normalize("加我威信") == "加我微信"


def test_longer_phrase_wins():
    n = Normalizer({"V信": "微信", "V信号": "VX号"}, [])
    assert n.normalize("加V信号") == "加VX号"


def test_quote_is_verbatim_from_original_line():
    # 归一化（零宽字符、短语替换、全角）后命中，引文仍然取原文
    text = "先垫​付<AMOUNT:200>返<AMOUNT:260>，冲值到ＡＰＰ平台"
    quotes = {r.id: q for r, q in RULESET.match_text(text)}
    assert quotes["BR-003"] in text and quotes["BR-003"].startswith("先垫")
    assert quotes["IV-011"] in text and "冲值" in quotes["IV-011"]


def test_quote_never_cuts_a_placeholder():
    text = "点赞截图一单<AMOUNT:3>"
    quote = dict((r.id, q) for r, q in RULESET.match_text(text))["BR-001"]
    assert quote.endswith("<AMOUNT:3>")


def test_match_lines_returns_rule_hits():
    lines = [
        InputLine(line_id="0-0", speaker=Speaker.OTHER, text="先垫付<AMOUNT:200>返<AMOUNT:260>", item_index=0, origin="pasted_text"),
        InputLine(line_id="0-1", speaker=Speaker.SELF, text="好的", item_index=0, origin="pasted_text"),
    ]
    hits = RULESET.match_lines(lines)
    assert hits and all(h.line_id == "0-0" for h in hits)
    hit = next(h for h in hits if h.rule_id == "BR-003")
    assert hit.intent == Intent.REQUEST_PAYMENT and hit.scene == Scene.BRUSHING_REBATE
    assert type(hit).from_dict(json.loads(json.dumps(hit.to_dict()))) == hit


@pytest.mark.parametrize(
    "text",
    [
        "这只基金最近跌了不少，你还拿着吗",
        "周末一起吃饭吧",
        "妈，我这个月生活费不够了，能转我点吗",
        "我们公司招前台，下周二来面试，不收任何费用",
        "收到，谢谢",
    ],
)
def test_ordinary_chat_hits_little(text):
    assert sum(r.weight for r, _ in RULESET.match_text(text)) < 1.0


def test_get_ruleset_is_cached():
    assert get_ruleset() is get_ruleset()
    assert get_ruleset().version == RULES.version


def test_rules_meta_rejects_wrong_shape(tmp_path):
    for body in ("[]", '"x"', '{"rules": 5}'):
        f = tmp_path / "rules.json"
        f.write_text(body, encoding="utf-8")
        meta = load_rules_meta(f)
        assert meta.version == "none" and meta.error == "ValueError"
