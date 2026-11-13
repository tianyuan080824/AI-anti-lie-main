"""汇总：证据回查、打分、契约第 6 节的每条硬性约束、固定文案。直接构造各路结果，不跑流水线。"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from app.analyzers.base import AnalyzerOutput, AnalyzerRun
from app.contract import (
    AnalyzerName,
    AnalyzerStatus,
    CheckRequest,
    DegradedFlag,
    Evidence,
    EvidenceSource,
    Intent,
    LinkReason,
    LinkResult,
    LinkRisk,
    MediaKind,
    MediaLabel,
    MediaResult,
    Scene,
    SimilarCase,
    Verdict,
)
from app.pipeline.inputs import build_input
from app.rules import get_ruleset
from app.scoring import load_weights, risk_score
from app.summary import Summarizer
from app.summary.base import Decision, apply_constraints, verify_evidence
from app.summary.templates import TEMPLATES_FILE, has_assurance, load_templates
from app.scoring import Score
from tests.conftest import chat_request

BRUSH = (
    "你好，我们是电商平台的兼职，日结，点赞截图一单3元",
    "先做三单体验一下，返了你9元，到账了吧",
    "现在有个进阶任务，需要先垫付200元，完成后返260元",
    "下载这个APP做任务，链接发你了",
    "名额有限，今天之内必须完成，不要告诉别人",
)
BRUSH_INTENTS = [
    ("small_rebate", "点赞截图一单"),
    ("small_rebate", "返了你"),
    ("request_payment", "先垫付"),
    ("induce_download", "下载这个APP"),
    ("urgency", "今天之内必须完成"),
    ("secrecy", "不要告诉别人"),
]
PLAIN = ("这周基金又跌了，白酒那只亏了不少", "我那只医药的倒是反弹了一点", "定投就别天天看了，拿长一点")


def make_input(settings, *texts, urls=()):
    req = CheckRequest.model_validate(chat_request(*texts, urls=urls))
    return build_input(req, check_id="c_test", trace_id="t_test", settings=settings)


def _intents(inp, pairs, *, line_id=None):
    out = []
    for intent, quote in pairs:
        lid = line_id or next((l.line_id for l in inp.lines if quote in l.text), "9-9")
        out.append({"intent": intent, "line_id": lid, "quote": quote, "explanation": "测试说明"})
    return out


def script_run(inp, pairs=(), *, scene="brushing_rebate", stage="s4_invest", llm=True, other=None, summary="", next_move=""):
    hits = get_ruleset().match_lines(l for l in inp.lines if l.speaker.value in ("other", "unknown"))
    extras = {"rule_hits": [h.to_dict() for h in hits], "cross_check": {"mode": "parallel", "other": None}}
    if not llm:
        extras["llm_used"] = None
        return AnalyzerRun(
            name=AnalyzerName.SCRIPT,
            status=AnalyzerStatus.DONE,
            output=AnalyzerOutput(degraded=[DegradedFlag.LLM_UNAVAILABLE_RULES_ONLY], extras=extras),
        )
    items = _intents(inp, pairs)
    if other is not None:
        extras["cross_check"]["other"] = {"role": "backup", "scene": "none", "stage": None, "intents": _intents(inp, other)}
    extras.update(llm_used="primary", scene=scene, stage=stage, summary=summary, next_move=next_move, intents=items)
    evidence = [Evidence(source=EvidenceSource.LLM, **i) for i in items]
    return AnalyzerRun(name=AnalyzerName.SCRIPT, status=AnalyzerStatus.DONE, output=AnalyzerOutput(evidence=evidence, extras=extras))


def done(name, **out):
    return AnalyzerRun(name=name, status=AnalyzerStatus.DONE, output=AnalyzerOutput(**out))


def skipped(name):
    return AnalyzerRun(name=name, status=AnalyzerStatus.SKIPPED, output=AnalyzerOutput(status=AnalyzerStatus.SKIPPED))


def runs_for(script, *, links=None, cases=None, media=None):
    return {
        AnalyzerName.SCRIPT: script,
        AnalyzerName.LINK: done(AnalyzerName.LINK, links=links) if links is not None else skipped(AnalyzerName.LINK),
        AnalyzerName.CASES: done(AnalyzerName.CASES, similar_cases=cases) if cases is not None else skipped(AnalyzerName.CASES),
        AnalyzerName.DEEPFAKE: done(AnalyzerName.DEEPFAKE, media=media) if media is not None else skipped(AnalyzerName.DEEPFAKE),
    }


def summarize(inp, runs, **kw):
    return Summarizer(**kw).build(inp, runs)


def weights(**update):
    return load_weights().model_copy(update=update)


def high_link(url="https://evil.example.cn/x", blacklist=False):
    reasons = [LinkReason.BLACKLIST_HIT] if blacklist else [LinkReason.DOMAIN_AGE_LT_30D]
    return LinkResult(url=url, risk=LinkRisk.HIGH, reasons=reasons)


def synthetic(score=0.95):
    return MediaResult(media_id="m_1", kind=MediaKind.AUDIO, label=MediaLabel.LIKELY_SYNTHETIC, spoof_score=score)


def assert_score_matches_verdict(report):
    w = load_weights()
    p = report.risk_score / 100
    if report.verdict == Verdict.HIGH_RISK:
        assert p >= w.thresholds.high_risk
    elif report.verdict == Verdict.LOW_RISK:
        assert p <= w.thresholds.low_risk
    elif report.verdict == Verdict.SUSPICIOUS:
        assert w.thresholds.low_risk < p < w.thresholds.high_risk


# ---------------------------------------------------------------------------
# 基本结论
# ---------------------------------------------------------------------------


def test_brushing_with_llm_is_high_risk(settings):
    inp = make_input(settings, *BRUSH)
    report = summarize(inp, runs_for(script_run(inp, BRUSH_INTENTS)))
    assert report.verdict == Verdict.HIGH_RISK
    assert report.scene == Scene.BRUSHING_REBATE and report.stage.value == "s4_invest"
    assert report.degraded == []
    assert {e.intent for e in report.evidence} >= {Intent.SMALL_REBATE, Intent.REQUEST_PAYMENT}
    for e in report.evidence:
        assert e.quote in inp.line(e.line_id).text
        assert e.source == EvidenceSource.LLM
    assert report.headline == "这很可能是刷单返利诈骗"
    assert [a.value for a in report.actions] == ["96110", "110"]
    assert_score_matches_verdict(report)


def test_plain_chat_is_low_risk(settings):
    inp = make_input(settings, *PLAIN)
    report = summarize(inp, runs_for(script_run(inp, [], scene="none", stage=None)))
    assert report.verdict == Verdict.LOW_RISK
    assert report.headline == "暂未发现明显诈骗特征"
    assert report.scene == Scene.NONE and report.stage is None
    assert report.evidence == [] and report.next_move == ""
    assert_score_matches_verdict(report)


def test_risk_score_is_round_100p(settings):
    inp = make_input(settings, *BRUSH)
    for pairs in ([], BRUSH_INTENTS[:1], BRUSH_INTENTS[:3], BRUSH_INTENTS):
        report = summarize(inp, runs_for(script_run(inp, pairs)))
        assert_score_matches_verdict(report)
    # 未经压制时 risk_score 就是 round(100·p)
    from app.scoring import Signals, score

    w = load_weights()
    ev = [Evidence(source=EvidenceSource.LLM, explanation="", **{k: v for k, v in i.items() if k != "explanation"}) for i in _intents(inp, BRUSH_INTENTS)]
    hits = get_ruleset().match_lines(inp.lines)
    from app.scoring import RuleSignal

    s = score(Signals(evidence=ev, rules=[RuleSignal(h.rule_id, h.weight) for h in hits]), w)
    report = summarize(inp, runs_for(script_run(inp, BRUSH_INTENTS)))
    assert report.risk_score == risk_score(s.p) == round(100 * s.p)


def test_suspicious_score_never_rounds_into_another_band(settings):
    inp = make_input(settings, *PLAIN)
    # 偏置调到只有一点信号时 p 落在 0.596：四舍五入是 60，但结论是 suspicious，分数必须 ≤ 59
    import math

    bias = math.log(0.596 / (1 - 0.596))
    report = summarize(inp, runs_for(script_run(inp, [], scene="none", stage=None)), weights=weights(bias=bias))
    assert report.verdict == Verdict.SUSPICIOUS and report.risk_score == 59


# ---------------------------------------------------------------------------
# 证据回查
# ---------------------------------------------------------------------------


def test_unmatched_quote_is_dropped_and_not_scored(settings):
    inp = make_input(settings, *BRUSH)
    fake = [("request_payment", "请把银行卡密码告诉我")]
    with_fake = summarize(inp, runs_for(script_run(inp, BRUSH_INTENTS[:2] + fake)))
    without = summarize(inp, runs_for(script_run(inp, BRUSH_INTENTS[:2])))
    assert all(e.quote != "请把银行卡密码告诉我" for e in with_fake.evidence)
    assert with_fake.risk_score == without.risk_score
    assert with_fake.verdict == without.verdict


def test_wrong_line_id_is_corrected(settings):
    inp = make_input(settings, *BRUSH)
    ev = Evidence(line_id="0-0", quote="先垫付", intent=Intent.REQUEST_PAYMENT, source=EvidenceSource.LLM, explanation="")
    [fixed] = verify_evidence([ev], inp)
    assert fixed.line_id == "0-2"


def test_quote_trimmed_and_deduplicated(settings):
    inp = make_input(settings, *BRUSH)
    mk = lambda q, lid="0-2": Evidence(line_id=lid, quote=q, intent=Intent.REQUEST_PAYMENT, source=EvidenceSource.LLM, explanation="")
    kept = verify_evidence([mk("「先垫付」"), mk("先垫付"), mk("  "), mk("先垫付", "nope")], inp)
    assert [(e.line_id, e.quote) for e in kept] == [("0-2", "先垫付")]


def test_quote_with_placeholder_is_verbatim(settings):
    inp = make_input(settings, *BRUSH)
    line = inp.line("0-2")
    assert "<AMOUNT:200>" in line.text  # 服务端已脱敏
    ev = Evidence(line_id="0-2", quote="先垫付200元", intent=Intent.REQUEST_PAYMENT, source=EvidenceSource.LLM, explanation="")
    assert verify_evidence([ev], inp) == []  # 未脱敏的原文对不上，丢弃
    ev2 = ev.model_copy(update={"quote": "先垫付<AMOUNT:200>"})
    assert len(verify_evidence([ev2], inp)) == 1


# ---------------------------------------------------------------------------
# 硬性约束（契约第 6 节第 3 条），每条一个测试
# ---------------------------------------------------------------------------


def test_constraint_high_risk_needs_evidence_or_high_link(settings):
    inp = make_input(settings, *PLAIN)
    w = weights(bias=1.0)  # 没有任何信号 p 也有 0.73
    report = summarize(inp, runs_for(script_run(inp, [], scene="none", stage=None)), weights=w)
    assert report.verdict == Verdict.SUSPICIOUS
    assert report.risk_score <= 59
    # 有一条 high 风险链接就可以给 high_risk
    inp2 = make_input(settings, *PLAIN, urls=("https://evil.example.cn/x",))
    report2 = summarize(inp2, runs_for(script_run(inp2, [], scene="none", stage=None), links=[high_link()]), weights=w)
    assert report2.verdict == Verdict.HIGH_RISK


def test_constraint_unit_no_evidence():
    w = load_weights()
    s = Score(bias=2.0, intents=0, rules=0, links=0, cases=0, deepfake=0)
    d = apply_constraints(Decision(Verdict.HIGH_RISK, s.p, s), has_evidence=False, links=[], llm_available=True, other_verdict=None, w=w)
    assert d.verdict == Verdict.SUSPICIOUS and d.p <= w.capped_p and d.capped_by == ["no_evidence"]


def test_constraint_deepfake_alone_not_high_risk(settings):
    inp = make_input(settings, "视频里是我，你先转我两万应急")
    w = weights(deepfake=load_weights().deepfake.model_copy(update={"weight": 6.0, "cap": 6.0}))
    pairs = [("request_payment", "你先转我两万应急")]
    report = summarize(inp, runs_for(script_run(inp, pairs, scene="impersonation_acquaintance", stage=None), media=[synthetic()]), weights=w)
    # 有证据，但去掉深伪后够不上 high_risk
    assert report.verdict == Verdict.SUSPICIOUS
    assert report.risk_score <= 59
    # 默认权重下深伪分量本身封顶 0.8
    assert load_weights().deepfake.cap < 1.0


def test_constraint_deepfake_with_enough_other_signals_is_allowed(settings):
    inp = make_input(settings, *BRUSH)
    report = summarize(inp, runs_for(script_run(inp, BRUSH_INTENTS), media=[synthetic()]))
    assert report.verdict == Verdict.HIGH_RISK


def test_constraint_cross_check_high_vs_low_disagrees(settings):
    inp = make_input(settings, *BRUSH)
    w = weights(rules=load_weights().rules.model_copy(update={"factor_with_llm": 0.0}))
    report = summarize(inp, runs_for(script_run(inp, BRUSH_INTENTS, other=[])), weights=w)
    assert report.verdict == Verdict.SUSPICIOUS
    assert DegradedFlag.CROSS_CHECK_DISAGREED in report.degraded
    assert report.risk_score <= 59


def test_constraint_cross_check_high_vs_suspicious_is_not_disagreement(settings):
    inp = make_input(settings, *BRUSH)
    report = summarize(inp, runs_for(script_run(inp, BRUSH_INTENTS, other=BRUSH_INTENTS[2:3])))
    assert report.verdict == Verdict.HIGH_RISK
    assert DegradedFlag.CROSS_CHECK_DISAGREED not in report.degraded


def test_constraint_cross_check_other_side_evidence_is_verified(settings):
    inp = make_input(settings, *BRUSH)
    w = weights(rules=load_weights().rules.model_copy(update={"factor_with_llm": 0.0}))
    # 另一方给的引文全是编的：回查后是空的 → low_risk → 不一致
    report = summarize(inp, runs_for(script_run(inp, BRUSH_INTENTS, other=[("request_payment", "编造的话")])), weights=w)
    assert DegradedFlag.CROSS_CHECK_DISAGREED in report.degraded


def test_constraint_llm_unavailable_caps_at_suspicious(settings):
    inp = make_input(settings, *BRUSH, urls=("https://evil.example.cn/x",))
    report = summarize(inp, runs_for(script_run(inp, llm=False), links=[high_link()]))
    assert report.verdict == Verdict.SUSPICIOUS
    assert DegradedFlag.LLM_UNAVAILABLE_RULES_ONLY in report.degraded
    assert report.risk_score <= 59
    assert report.confidence.value == "low"
    # 规则命中作为证据列出（来源 rule），引文逐字
    assert report.evidence and all(e.source == EvidenceSource.RULE for e in report.evidence)
    assert all(e.quote in inp.line(e.line_id).text for e in report.evidence)
    assert report.scene == Scene.BRUSHING_REBATE and report.stage is None
    assert "大模型暂时不可用" in report.summary


def test_constraint_llm_unavailable_blacklist_exception(settings):
    inp = make_input(settings, *BRUSH, urls=("https://evil.example.cn/x",))
    report = summarize(inp, runs_for(script_run(inp, llm=False), links=[high_link(blacklist=True)]))
    assert report.verdict == Verdict.HIGH_RISK
    assert DegradedFlag.LLM_UNAVAILABLE_RULES_ONLY in report.degraded


def test_constraint_script_failed_still_uses_local_rules(settings):
    inp = make_input(settings, *BRUSH)
    failed = AnalyzerRun(
        name=AnalyzerName.SCRIPT,
        status=AnalyzerStatus.FAILED,
        error="timeout",
        output=AnalyzerOutput(status=AnalyzerStatus.FAILED, degraded=[DegradedFlag.LLM_UNAVAILABLE_RULES_ONLY, DegradedFlag.PARTIAL_TIMEOUT]),
    )
    report = summarize(inp, runs_for(failed))
    assert report.verdict == Verdict.SUSPICIOUS
    assert {DegradedFlag.LLM_UNAVAILABLE_RULES_ONLY, DegradedFlag.PARTIAL_TIMEOUT} <= set(report.degraded)
    assert report.evidence  # 规则在汇总里补算


@pytest.mark.parametrize("texts", [("在吗",), ("你好",), ("", "嗯"), ("在吗？在吗？在吗",)])
def test_constraint_too_little_text_is_inconclusive(settings, texts):
    inp = make_input(settings, *texts)
    report = summarize(inp, runs_for(script_run(inp, [("request_payment", texts[0] or "x")])))
    assert report.verdict == Verdict.INCONCLUSIVE
    assert report.scene == Scene.NONE and report.stage is None and report.risk_score == 0
    assert report.headline == "内容太少，暂时无法判断"


def test_system_lines_do_not_count_as_content(settings):
    raw = chat_request("2026-11-11 10:23:45 星期三", "对方撤回了一条消息", speaker="system")
    raw["items"][0]["lines"].append({"line_id": "0-9", "speaker": "other", "text": "在吗"})
    inp = build_input(CheckRequest.model_validate(raw), check_id="c_test", trace_id="t_test", settings=settings)
    assert inp.char_count() >= 10
    report = summarize(inp, runs_for(script_run(inp, [("request_payment", "在吗")])))
    assert report.verdict == Verdict.INCONCLUSIVE


def test_short_text_with_url_is_analyzed(settings):
    inp = make_input(settings, "看这个", urls=("https://evil.example.cn/x",))
    report = summarize(inp, runs_for(script_run(inp, [], scene="none", stage=None), links=[high_link()]))
    assert report.verdict != Verdict.INCONCLUSIVE


# ---------------------------------------------------------------------------
# 降级标记
# ---------------------------------------------------------------------------


def test_skipped_cases_is_not_degradation(settings):
    inp = make_input(settings, *BRUSH)
    report = summarize(inp, runs_for(script_run(inp, BRUSH_INTENTS)))
    assert report.degraded == []


def test_analyzer_flags_are_passed_through(settings):
    inp = make_input(settings, *BRUSH)
    runs = runs_for(script_run(inp, BRUSH_INTENTS))
    runs[AnalyzerName.CASES] = AnalyzerRun(
        name=AnalyzerName.CASES, status=AnalyzerStatus.FAILED, output=AnalyzerOutput(status=AnalyzerStatus.FAILED, degraded=[DegradedFlag.CASES_UNAVAILABLE])
    )
    runs[AnalyzerName.LINK] = done(AnalyzerName.LINK, degraded=[DegradedFlag.INTEL_UNAVAILABLE])
    assert set(summarize(inp, runs).degraded) == {DegradedFlag.CASES_UNAVAILABLE, DegradedFlag.INTEL_UNAVAILABLE}


def test_link_failure_lists_user_urls_as_unknown(settings):
    inp = make_input(settings, *PLAIN, urls=("https://a.example.cn/1",))
    runs = runs_for(script_run(inp, [], scene="none", stage=None))
    runs[AnalyzerName.LINK] = AnalyzerRun(name=AnalyzerName.LINK, status=AnalyzerStatus.FAILED, output=AnalyzerOutput(status=AnalyzerStatus.FAILED))
    report = summarize(inp, runs)
    assert [(l.url, l.risk) for l in report.links] == [("https://a.example.cn/1", LinkRisk.UNKNOWN)]


def test_similar_cases_raise_score(settings):
    inp = make_input(settings, *BRUSH)
    case = SimilarCase(case_id="k1", title="t", scene=Scene.BRUSHING_REBATE, snippet="s", source_name="n", similarity=0.9)
    pairs = BRUSH_INTENTS[:1]
    a = summarize(inp, runs_for(script_run(inp, pairs)))
    b = summarize(inp, runs_for(script_run(inp, pairs), cases=[case]))
    assert b.risk_score > a.risk_score
    assert b.similar_cases == [case]


# ---------------------------------------------------------------------------
# 文案
# ---------------------------------------------------------------------------


def _all_strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from _all_strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _all_strings(v)


def test_templates_never_promise_safety():
    raw = yaml.safe_load(Path(TEMPLATES_FILE).read_text(encoding="utf-8"))
    raw.pop("disclaimer")
    for text in _all_strings(raw):
        assert "安全" not in text and "放心" not in text, text
        assert not has_assurance(text)


def test_template_lookup_covers_every_verdict_and_scene():
    t = load_templates()
    for verdict in Verdict:
        for scene in Scene:
            assert t.headline_for(verdict, scene)
            assert len(t.advice_for(verdict, scene)) >= 2
            assert t.actions_for(verdict)
    assert t.headline_for(Verdict.LOW_RISK, Scene.INVESTMENT) == "暂未发现明显诈骗特征"
    assert t.disclaimer == "本结果由算法生成，仅供参考。未发现风险不代表安全。"


@pytest.mark.parametrize(
    "text",
    ["这是安全的，可以放心转账", "对方没有诈骗行为", "请放心，不是骗子", "这个链接很安全", "放心下载就行", "内容安全", "你放心吧", "无风险", "资金安全有保障"],
)
def test_llm_text_with_assurance_is_replaced(settings, text):
    inp = make_input(settings, *PLAIN)
    pairs = []
    report = summarize(inp, runs_for(script_run(inp, pairs, scene="none", stage=None, summary=text, next_move=text)))
    for field in (report.headline, report.summary, report.next_move, *report.advice, *(e.explanation for e in report.evidence)):
        assert not has_assurance(field) and "安全" not in field and "放心" not in field


@pytest.mark.parametrize("text", ["这句话是安全的", "内容安全", "你放心吧", "无风险", "资金安全"])
def test_llm_explanation_with_assurance_uses_template(settings, text):
    inp = make_input(settings, *BRUSH)
    run = script_run(inp, BRUSH_INTENTS)
    run.output.evidence[0] = run.output.evidence[0].model_copy(update={"explanation": text})
    report = summarize(inp, runs_for(run))
    assert report.evidence
    assert all(not has_assurance(e.explanation) for e in report.evidence)


def test_report_wording_never_contains_assurance_in_any_verdict(settings):
    inp_brush = make_input(settings, *BRUSH)
    inp_plain = make_input(settings, *PLAIN)
    reports = [
        summarize(inp_brush, runs_for(script_run(inp_brush, BRUSH_INTENTS))),
        summarize(inp_brush, runs_for(script_run(inp_brush, llm=False))),
        summarize(inp_plain, runs_for(script_run(inp_plain, [], scene="none", stage=None))),
        summarize(make_input(settings, "在吗"), runs_for(script_run(make_input(settings, "在吗"), []))),
    ]
    assert {r.verdict for r in reports} == {Verdict.HIGH_RISK, Verdict.SUSPICIOUS, Verdict.LOW_RISK, Verdict.INCONCLUSIVE}
    for r in reports:
        text = "".join([r.headline, r.summary, r.next_move, *r.advice, *(a.label for a in r.actions)])
        assert "安全" not in text and "放心" not in text
        assert r.disclaimer.endswith("未发现风险不代表安全。")


def test_llm_summary_used_when_clean(settings):
    inp = make_input(settings, *BRUSH)
    report = summarize(inp, runs_for(script_run(inp, BRUSH_INTENTS, summary="对方以兼职为名先返小额佣金，再要求垫付。", next_move="会以卡单为由要求继续垫付。")))
    assert report.summary == "对方以兼职为名先返小额佣金，再要求垫付。"
    assert report.next_move == "会以卡单为由要求继续垫付。"
    assert re.fullmatch(r".+", report.headline)


def test_rule_evidence_title_with_anquan_is_kept(settings):
    # 规则标题「要求转入安全账户」是在描述诈骗手法，不是保证；规则证据不过保证性措辞的检查
    inp = make_input(settings, "我是市公安局的民警，你的账户涉嫌洗钱", "请把资金转入安全账户配合调查，不要告诉家人")
    report = summarize(inp, runs_for(script_run(inp, llm=False)))
    [e] = [e for e in report.evidence if e.quote == "安全账户"]
    assert e.source == EvidenceSource.RULE and e.explanation.startswith("要求转入安全账户：")
