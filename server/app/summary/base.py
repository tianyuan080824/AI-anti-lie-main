"""汇总：证据回查 → 打分 → 硬性约束 → 固定模板（契约第 5、6 节）。纯代码，不调大模型。

- 证据回查：大模型的每条 `quote` 必须是 `line_id` 那一行的逐字子串；对不上先在其他行里找并修正行号，
  找不到就丢弃，被丢弃的证据不参与打分。
- 打分：`app/scoring`（权重在 `app/scoring/weights.yaml`），`risk_score = round(100·p)`。
- 硬性约束（契约第 6 节第 3 条）逐条在 `apply_constraints()` 里实现；把结论压到 `suspicious` 时
  p 同时压到 `capped_p`，保证 `risk_score / 100` 和结论一致。
- 文案：`headline`、`advice`、`actions`、`disclaimer` 来自 `templates.yaml`；`summary`、`next_move`、
  `explanation` 优先用大模型写的，含「安全」「放心」一类保证时换成模板。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from app.analyzers.base import COUNTERPART_SPEAKERS, AnalysisInput, AnalyzerContext, AnalyzerRun
from app.contract import (
    ActionType,
    AnalyzerName,
    AnalyzerStatus,
    Confidence,
    DegradedFlag,
    Evidence,
    EvidenceSource,
    Intent,
    LinkResult,
    LinkRisk,
    Report,
    ReportAction,
    Scene,
    Speaker,
    Stage,
    Verdict,
    ordered_flags,
)
from app.rules import RuleHit, get_ruleset
from app.scoring import RuleSignal, Score, Signals, Weights, is_blacklisted, load_weights, risk_score, score, verdict_for
from app.summary.templates import Templates, has_assurance, load_templates

DISCLAIMER = "本结果由算法生成，仅供参考。未发现风险不代表安全。"
ACTION_96110 = ReportAction(type=ActionType.DIAL, label="拨打反诈专线 96110", value="96110")
ADVICE_GENERIC = [
    "不要向陌生人转账、垫付或充值",
    "不要安装对方发来的应用或点击陌生链接",
    "拿不准时拨打 96110 咨询，已经转账请立即拨打 110 并联系银行",
]

#: 规则证据在报告里最多列几条（只在大模型不可用时列）
MAX_RULE_EVIDENCE = 8
_MAX_LLM_TEXT = 300
_MAX_EXPLANATION = 120
_QUOTE_TRIM = " \t\r\n\"'“”‘’「」『』"
_FRAUD_SCENES = (
    Scene.BRUSHING_REBATE,
    Scene.INVESTMENT,
    Scene.IMPERSONATION_SERVICE,
    Scene.IMPERSONATION_ACQUAINTANCE,
)


def _collect_flags(runs: dict[AnalyzerName, AnalyzerRun]) -> list[DegradedFlag]:
    return ordered_flags(*(run.output.degraded for run in runs.values()))


def _links(inp: AnalysisInput, runs: dict[AnalyzerName, AnalyzerRun]) -> list[LinkResult]:
    run = runs.get(AnalyzerName.LINK)
    if run is not None and run.status == AnalyzerStatus.DONE:
        return list(run.output.links)
    # 链接这一路失败时，至少把用户提交的链接列出来（风险未知），不让它们从报告里消失。
    return [LinkResult(url=u.url, origin=u.origin, risk=LinkRisk.UNKNOWN) for u in inp.urls]


_CHAT_SPEAKERS = tuple(s for s in Speaker if s != Speaker.SYSTEM)


def _has_content(inp: AnalysisInput) -> bool:
    # 系统行（时间戳、「对方撤回了一条消息」）不算内容
    return inp.char_count(_CHAT_SPEAKERS) >= 10 or bool(inp.urls) or bool(inp.media)


# ---------------------------------------------------------------------------
# 证据回查
# ---------------------------------------------------------------------------


def verify_evidence(evidence: Iterable[Evidence], inp: AnalysisInput) -> list[Evidence]:
    """只保留 `quote` 是某一行逐字子串的证据；行号标错的修正，找不到的丢弃。按原文顺序、去重。"""
    order = {line.line_id: i for i, line in enumerate(inp.lines)}
    search_order = list(inp.iter_lines(COUNTERPART_SPEAKERS)) + [
        l for l in inp.lines if l.speaker not in COUNTERPART_SPEAKERS
    ]
    kept: list[Evidence] = []
    seen: set[tuple[str, str, Intent]] = set()
    for ev in evidence:
        quote = ev.quote.strip(_QUOTE_TRIM)
        if not quote:
            continue
        line = inp.line(ev.line_id)
        if line is None or quote not in line.text:
            line = next((l for l in search_order if quote in l.text), None)
        if line is None:
            continue
        key = (line.line_id, quote, ev.intent)
        if key in seen:
            continue
        seen.add(key)
        kept.append(ev.model_copy(update={"line_id": line.line_id, "quote": quote}))
    kept.sort(key=lambda e: order.get(e.line_id, len(order)))
    return kept


def rule_evidence(hits: Iterable[RuleHit], inp: AnalysisInput, templates: Templates) -> list[Evidence]:
    """规则命中转成 `source=rule` 的证据：同一行同一意图只留权重最高的一条。"""
    best: dict[tuple[str, Intent], RuleHit] = {}
    for h in hits:
        key = (h.line_id, h.intent)
        if key not in best or h.weight > best[key].weight:
            best[key] = h
    top = sorted(best.values(), key=lambda h: -h.weight)[:MAX_RULE_EVIDENCE]
    order = {line.line_id: i for i, line in enumerate(inp.lines)}
    top.sort(key=lambda h: order.get(h.line_id, len(order)))
    return [
        Evidence(
            line_id=h.line_id,
            quote=h.quote,
            intent=h.intent,
            source=EvidenceSource.RULE,
            explanation=f"{h.title}：{templates.intent_explanations[h.intent]}",
        )
        for h in top
    ]


# ---------------------------------------------------------------------------
# 读取各路结果
# ---------------------------------------------------------------------------


@dataclass
class ScriptView:
    """话术这一路交给汇总的信号。"""

    llm_available: bool
    evidence: list[Evidence] = field(default_factory=list)
    other_evidence: list[Evidence] | None = None
    rule_hits: list[RuleHit] = field(default_factory=list)
    scene: Scene = Scene.NONE
    stage: Stage | None = None
    summary: str = ""
    next_move: str = ""


def _evidence_from_intents(items: Iterable[dict[str, Any]]) -> list[Evidence]:
    out = []
    for item in items or []:
        try:
            out.append(
                Evidence(
                    line_id=str(item["line_id"]),
                    quote=str(item["quote"]),
                    intent=Intent(item["intent"]),
                    source=EvidenceSource.LLM,
                    explanation=str(item.get("explanation") or ""),
                )
            )
        except (KeyError, ValueError, TypeError):
            continue
    return out


def _enum(enum, value, default):
    try:
        return enum(value) if value is not None else default
    except ValueError:
        return default


def read_script(inp: AnalysisInput, runs: dict[AnalyzerName, AnalyzerRun]) -> ScriptView:
    run = runs.get(AnalyzerName.SCRIPT)
    extras: dict[str, Any] = run.output.extras if run is not None else {}
    done = run is not None and run.status == AnalyzerStatus.DONE
    flags = set(run.output.degraded) if run is not None else set()

    raw_hits = extras.get("rule_hits") if done else None
    hits: list[RuleHit] = []
    if isinstance(raw_hits, list):
        for item in raw_hits:
            try:
                hits.append(RuleHit.from_dict(item))
            except (KeyError, ValueError, TypeError):
                continue
    else:
        # 话术这一路没跑完（超时、异常）：规则是本地的，在这里补算，保证断网时也有规则信号
        try:
            hits = get_ruleset().match_lines(inp.iter_lines(COUNTERPART_SPEAKERS))
        except Exception:  # noqa: BLE001  规则文件坏了也要出报告
            hits = []

    llm_available = done and DegradedFlag.LLM_UNAVAILABLE_RULES_ONLY not in flags and bool(extras.get("llm_used"))
    view = ScriptView(llm_available=llm_available, rule_hits=hits)
    if not llm_available:
        return view

    view.evidence = list(run.output.evidence)
    view.scene = _enum(Scene, extras.get("scene"), Scene.NONE)
    view.stage = _enum(Stage, extras.get("stage"), None) if view.scene != Scene.NONE else None
    view.summary = str(extras.get("summary") or "")
    view.next_move = str(extras.get("next_move") or "")
    other = (extras.get("cross_check") or {}).get("other")
    if isinstance(other, dict):
        view.other_evidence = _evidence_from_intents(other.get("intents"))
    return view


def _ok_output(runs: dict[AnalyzerName, AnalyzerRun], name: AnalyzerName):
    run = runs.get(name)
    return run.output if run is not None and run.status == AnalyzerStatus.DONE else None


# ---------------------------------------------------------------------------
# 结论与硬性约束
# ---------------------------------------------------------------------------


@dataclass
class Decision:
    verdict: Verdict
    p: float
    score: Score
    flags: list[DegradedFlag] = field(default_factory=list)
    #: 被哪些硬性约束压过（只用于日志和测试）
    capped_by: list[str] = field(default_factory=list)


def _cap(d: Decision, reason: str, w: Weights) -> None:
    if d.verdict == Verdict.HIGH_RISK:
        d.verdict = Verdict.SUSPICIOUS
        d.p = min(d.p, w.capped_p)
        d.capped_by.append(reason)


def apply_constraints(
    d: Decision,
    *,
    has_evidence: bool,
    links: list[LinkResult],
    llm_available: bool,
    other_verdict: Verdict | None,
    w: Weights,
) -> Decision:
    has_high_link = any(link.risk == LinkRisk.HIGH for link in links)
    # 1. high_risk 至少要有一条通过回查的原文证据，或一条 high 风险链接
    if d.verdict == Verdict.HIGH_RISK and not (has_evidence or has_high_link):
        _cap(d, "no_evidence", w)
    # 2. 深伪分数单独不足以给出 high_risk：去掉深伪分量后够不上 high_risk 就不给
    if d.verdict == Verdict.HIGH_RISK and verdict_for(d.score.p_without_deepfake, w) != Verdict.HIGH_RISK:
        _cap(d, "deepfake_alone", w)
    # 3. 交叉复核：一方 high_risk、另一方 low_risk 才算不一致
    if other_verdict is not None and {d.verdict, other_verdict} == {Verdict.HIGH_RISK, Verdict.LOW_RISK}:
        _cap(d, "cross_check", w)
        d.flags.append(DegradedFlag.CROSS_CHECK_DISAGREED)
    # 4. 大模型全部不可用：最高 suspicious（命中黑名单的链接除外）
    if not llm_available:
        if d.verdict == Verdict.HIGH_RISK and not any(is_blacklisted(link) for link in links):
            _cap(d, "llm_unavailable", w)
        d.flags.append(DegradedFlag.LLM_UNAVAILABLE_RULES_ONLY)
    return d


def _confidence(d: Decision, llm_available: bool) -> Confidence:
    if not llm_available or d.capped_by or DegradedFlag.CROSS_CHECK_DISAGREED in d.flags:
        return Confidence.LOW
    if d.verdict == Verdict.HIGH_RISK and d.p >= 0.85:
        return Confidence.HIGH
    return Confidence.MEDIUM


def _scene_from_rules(hits: Iterable[RuleHit]) -> Scene:
    totals: dict[Scene, float] = {}
    for h in hits:
        totals[h.scene] = totals.get(h.scene, 0.0) + h.weight
    fraud = {s: v for s, v in totals.items() if s in _FRAUD_SCENES}
    if fraud:
        return max(fraud, key=fraud.get)
    return Scene.OTHER if totals else Scene.NONE


def _clean(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if not text or has_assurance(text):
        return ""
    return text[:limit]


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------


class Summarizer:
    def __init__(self, *, weights: Weights | None = None, templates: Templates | None = None):
        self._weights = weights
        self._templates = templates

    @property
    def weights(self) -> Weights:
        return self._weights or load_weights()

    @property
    def templates(self) -> Templates:
        return self._templates or load_templates()

    async def summarize(
        self, inp: AnalysisInput, runs: dict[AnalyzerName, AnalyzerRun], ctx: AnalyzerContext
    ) -> Report:
        return self.build(inp, runs)

    def build(self, inp: AnalysisInput, runs: dict[AnalyzerName, AnalyzerRun]) -> Report:
        w, t = self.weights, self.templates
        flags = _collect_flags(runs)

        # 5. 可分析文本不足 10 个字且没有链接和媒体：inconclusive
        if not _has_content(inp):
            return self._report(Verdict.INCONCLUSIVE, 0.0, Confidence.LOW, Scene.NONE, None, flags=flags)

        script = read_script(inp, runs)
        links = _links(inp, runs)
        cases_out = _ok_output(runs, AnalyzerName.CASES)
        media_out = _ok_output(runs, AnalyzerName.DEEPFAKE)
        cases = list(cases_out.similar_cases) if cases_out else []
        media = list(media_out.media) if media_out else []

        verified = verify_evidence(script.evidence, inp) if script.llm_available else []
        rules = [RuleSignal(h.rule_id, h.weight) for h in script.rule_hits]

        def decide(evidence: list[Evidence]) -> Decision:
            s = score(
                Signals(
                    evidence=evidence,
                    rules=rules,
                    links=links,
                    cases=cases,
                    media=media,
                    llm_available=script.llm_available,
                ),
                w,
            )
            return Decision(verdict=verdict_for(s.p, w), p=s.p, score=s)

        other_verdict = None
        if script.other_evidence is not None:
            other_verdict = decide(verify_evidence(script.other_evidence, inp)).verdict

        report_evidence = verified if script.llm_available else rule_evidence(script.rule_hits, inp, t)
        decision = apply_constraints(
            decide(verified),
            has_evidence=bool(report_evidence),
            links=links,
            llm_available=script.llm_available or not any(l.text.strip() for l in inp.lines),
            other_verdict=other_verdict,
            w=w,
        )
        verdict = decision.verdict

        # 场景和阶段：优先大模型；没有大模型时按规则命中的场景
        scene, stage = script.scene, script.stage
        if not script.llm_available:
            scene, stage = _scene_from_rules(script.rule_hits), None
        if verdict == Verdict.LOW_RISK:
            scene, stage = Scene.NONE, None
        elif scene == Scene.NONE:
            scene = Scene.DEEPFAKE if decision.score.deepfake > 0 and not report_evidence else Scene.OTHER
            stage = None

        evidence = report_evidence if verdict != Verdict.LOW_RISK or script.llm_available else []
        # 规则证据的说明来自规则标题（如「要求转入安全账户」）和模板，不过保证性措辞的检查
        evidence = [
            e
            if e.source == EvidenceSource.RULE
            else e.model_copy(
                update={"explanation": _clean(e.explanation, _MAX_EXPLANATION) or t.intent_explanations[e.intent]}
            )
            for e in evidence
        ]

        summary = _clean(script.summary, _MAX_LLM_TEXT) or t.summary_for(verdict, scene)
        if not script.llm_available and any(l.text.strip() for l in inp.lines):
            summary = f"{summary}{t.rules_only_note}"
        if verdict in (Verdict.HIGH_RISK, Verdict.SUSPICIOUS):
            next_move = _clean(script.next_move, _MAX_LLM_TEXT) or t.next_move_for(scene)
        else:
            next_move = ""

        return self._report(
            verdict,
            decision.p,
            _confidence(decision, script.llm_available),
            scene,
            stage,
            summary=summary,
            next_move=next_move,
            evidence=evidence,
            links=links,
            media=media,
            similar_cases=cases,
            flags=ordered_flags(flags, decision.flags),
        )

    def _report(
        self,
        verdict: Verdict,
        p: float,
        confidence: Confidence,
        scene: Scene,
        stage: Stage | None,
        *,
        flags: list[DegradedFlag],
        summary: str | None = None,
        next_move: str = "",
        **lists: Any,
    ) -> Report:
        t = self.templates
        if verdict == Verdict.SUSPICIOUS:
            # 阈值附近四舍五入会让 risk_score / 100 落到另一档（0.596 → 60），这里保证分数和结论同档
            p = min(max(p, 0.06), self.weights.capped_p)
        return Report(
            verdict=verdict,
            risk_score=risk_score(p) if verdict != Verdict.INCONCLUSIVE else 0,
            confidence=confidence,
            scene=scene,
            stage=stage if scene != Scene.NONE else None,
            headline=t.headline_for(verdict, scene),
            summary=summary if summary is not None else t.summary_for(verdict, scene),
            next_move=next_move,
            advice=t.advice_for(verdict, scene),
            actions=t.actions_for(verdict),
            degraded=flags,
            disclaimer=t.disclaimer,
            **lists,
        )


def fallback_report(inp: AnalysisInput, runs: dict[AnalyzerName, AnalyzerRun]) -> Report:
    """汇总失败时的兜底：不下结论（suspicious 是「拿不准」的最保守表述），但链接照列、建议照给。"""
    return Report(
        verdict=Verdict.SUSPICIOUS if _has_content(inp) else Verdict.INCONCLUSIVE,
        risk_score=50 if _has_content(inp) else 0,
        confidence=Confidence.LOW,
        scene=Scene.OTHER if _has_content(inp) else Scene.NONE,
        stage=None,
        headline="分析未能全部完成，请谨慎对待",
        summary="部分分析没有完成，无法给出完整结论。涉及转账、垫付、下载应用时务必先核实。",
        next_move="",
        links=_links(inp, runs),
        advice=ADVICE_GENERIC,
        actions=[ACTION_96110],
        degraded=ordered_flags(_collect_flags(runs), [DegradedFlag.PARTIAL_TIMEOUT]),
        disclaimer=DISCLAIMER,
    )


def create() -> Summarizer:
    return Summarizer()
