"""评测脚本：不经过 HTTP 跑完整条流水线，统计指标。

在 `server/` 目录下执行：

    uv run python -m eval.run_eval                       # eval/data/*.jsonl，CROSS_CHECK off 与 parallel 各跑一遍
    uv run python -m eval.run_eval 路径.jsonl 目录/ ...    # 指定数据
    uv run python -m eval.run_eval --cross-check config   # 只按 .env 里的设置跑一遍
    uv run python -m eval.run_eval --isolated             # 用临时数据库（案例库为空，相似案例不参与）

风险概率 p = report.risk_score / 100。结果 JSON 写到 `<VAR_DIR>/eval/`。说明见 eval/README.md。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence

if __package__ in (None, ""):  # 允许 `uv run python eval/run_eval.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.analyzers.base import Analyzer  # noqa: E402
from app.config import Settings, get_settings  # noqa: E402
from app.contract import ANALYZER_ORDER, AnalyzerName, DegradedFlag  # noqa: E402
from app.errors import ApiError  # noqa: E402
from app.pipeline import open_pipeline  # noqa: E402
from app.summary import Summarizer  # noqa: E402
from eval.dataset import EvalSample, find_data_files, load_samples  # noqa: E402
from eval.metrics import (  # noqa: E402
    DEFAULT_FPR_TARGETS,
    DEFAULT_THRESHOLDS,
    HIGH_THRESHOLD,
    LOW_THRESHOLD,
    confusion_at,
    count,
    latency_summary,
    recall_at_fpr,
    three_way,
)

RULES_ONLY = DegradedFlag.LLM_UNAVAILABLE_RULES_ONLY.value
PLACEHOLDER_MARK = "【占位】"
ANALYZERS = [n for n in ANALYZER_ORDER if n != AnalyzerName.SUMMARY]

AnalyzersFactory = Callable[[], list[Analyzer]]
SummarizerFactory = Callable[[], Summarizer]


@dataclass
class SampleResult:
    id: str
    label: str
    scene: str
    source: str
    #: 请求不合法时为 None
    p: float | None = None
    verdict: str | None = None
    predicted_scene: str | None = None
    degraded: list[str] = field(default_factory=list)
    elapsed_ms: int | None = None
    #: 各分析器 {status, elapsed_ms, error}
    runs: dict[str, dict[str, Any]] = field(default_factory=dict)
    placeholder: bool = False
    error: str | None = None

    @property
    def positive(self) -> bool:
        return self.label == "fraud"


async def run_samples(
    samples: Sequence[EvalSample],
    settings: Settings,
    *,
    analyzers: AnalyzersFactory | None = None,
    summarizer: SummarizerFactory | None = None,
) -> list[SampleResult]:
    """在同一个 open_pipeline() 里顺序跑完所有样本（顺序跑，时延才有意义）。"""
    results: list[SampleResult] = []
    async with open_pipeline(
        settings,
        analyzers=analyzers() if analyzers else None,
        summarizer=summarizer() if summarizer else None,
    ) as pipe:
        for s in samples:
            r = SampleResult(id=s.id, label=s.label, scene=s.scene, source=s.source)
            try:
                result = await pipe.analyze(s.to_request())
            except ApiError as exc:
                r.error = f"{exc.code.value}: {exc.message}"
                results.append(r)
                continue
            report = result.report
            r.p = report.risk_score / 100
            r.verdict = report.verdict.value
            r.predicted_scene = report.scene.value
            r.degraded = [f.value for f in report.degraded]
            r.elapsed_ms = result.elapsed_ms
            r.placeholder = report.headline.startswith(PLACEHOLDER_MARK)
            r.runs = {
                name.value: {"status": run.status.value, "elapsed_ms": run.elapsed_ms, "error": run.error}
                for name, run in result.runs.items()
            }
            results.append(r)
    return results


def summarize(
    results: Sequence[SampleResult],
    *,
    thresholds: Sequence[float] = DEFAULT_THRESHOLDS,
    fpr_targets: Sequence[float] = DEFAULT_FPR_TARGETS,
) -> dict[str, Any]:
    ok = [r for r in results if r.p is not None]
    probs = [r.p for r in ok]
    pos = [r.positive for r in ok]

    def block(rs: Sequence[SampleResult]) -> dict[str, Any]:
        ps = [r.p for r in rs]
        ys = [r.positive for r in rs]
        return {
            "n": len(rs),
            "fraud": sum(ys),
            "normal": len(ys) - sum(ys),
            "at_high_threshold": confusion_at(ps, ys, HIGH_THRESHOLD).as_dict(),
            "recall_at_fpr": [recall_at_fpr(ps, ys, t).as_dict() for t in fpr_targets],
            "three_way": {
                "fraud": count(three_way(r.p) for r in rs if r.positive),
                "normal": count(three_way(r.p) for r in rs if not r.positive),
            },
        }

    analyzer_latency = {
        name.value: latency_summary(
            [r.runs[name.value]["elapsed_ms"] for r in ok if r.runs.get(name.value, {}).get("elapsed_ms") is not None]
        )
        for name in ANALYZERS
    }
    analyzer_status = {name.value: count(r.runs.get(name.value, {}).get("status", "missing") for r in ok) for name in ANALYZERS}

    return {
        "n_total": len(results),
        "n_ok": len(ok),
        "invalid": [{"id": r.id, "error": r.error} for r in results if r.p is None],
        "labels": count(r.label for r in ok),
        "confusion": [confusion_at(probs, pos, t).as_dict() for t in thresholds],
        "recall_at_fpr": [recall_at_fpr(probs, pos, t).as_dict() for t in fpr_targets],
        "three_way": {
            "high": HIGH_THRESHOLD,
            "low": LOW_THRESHOLD,
            "fraud": count(three_way(r.p) for r in ok if r.positive),
            "normal": count(three_way(r.p) for r in ok if not r.positive),
        },
        "verdicts": {
            "fraud": count(r.verdict for r in ok if r.positive),
            "normal": count(r.verdict for r in ok if not r.positive),
        },
        "by_source": {src: block([r for r in ok if r.source == src]) for src in sorted({r.source for r in ok})},
        "latency_ms": latency_summary([r.elapsed_ms for r in ok if r.elapsed_ms is not None]),
        "analyzer_latency_ms": analyzer_latency,
        "analyzer_status": analyzer_status,
        "degraded": count(f for r in ok for f in r.degraded),
        "rules_only": sum(1 for r in ok if RULES_ONLY in r.degraded),
        "placeholder_reports": sum(1 for r in ok if r.placeholder),
        "cases_participated": sum(1 for r in ok if r.runs.get("cases", {}).get("status") == "done"),
    }


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


def _pct(x: float | None) -> str:
    return "  -  " if x is None else f"{x * 100:5.1f}%"


def _num(x: float | None) -> str:
    return "-" if x is None else f"{x:.0f}"


def warnings_for(summary: dict[str, Any]) -> list[str]:
    """必须醒目打印的提醒：只用规则、占位实现、相似案例没参与。"""
    n = summary["n_ok"]
    out: list[str] = []
    if n == 0:
        return ["没有成功跑完的样本"]
    if summary["rules_only"]:
        scope = "全部" if summary["rules_only"] == n else "部分"
        out.append(
            f"{summary['rules_only']}/{n} 条带 {RULES_ONLY}（{scope}样本没有用到大模型，只用了规则）："
            "这些数字只代表规则层，不能当作整个系统的效果"
        )
    if summary["placeholder_reports"]:
        out.append(f"{summary['placeholder_reports']}/{n} 条报告来自占位实现（headline 带「{PLACEHOLDER_MARK}」）：指标没有意义")
    if summary["cases_participated"] == 0:
        out.append("相似案例这一路一条都没有参与（案例库为空、没有密钥或向量接口不可用）")
    return out


def format_summary(name: str, summary: dict[str, Any]) -> list[str]:
    lines = [f"===== 设置：{name} =====", f"样本 {summary['n_ok']}/{summary['n_total']} 条，标签 {summary['labels']}"]
    for w in warnings_for(summary):
        lines.append(f"！！！ {w}")
    for inv in summary["invalid"]:
        lines.append(f"  请求不合法：{inv['id']}  {inv['error']}")

    lines.append("")
    lines.append("混淆矩阵（p = risk_score/100，p ≥ 阈值判为诈骗）")
    lines.append("  阈值    TP   FP   TN   FN   召回     误报率   精确率")
    for c in summary["confusion"]:
        lines.append(
            f"  {c['threshold']:.2f}  {c['tp']:4d} {c['fp']:4d} {c['tn']:4d} {c['fn']:4d}  "
            f"{_pct(c['recall'])}  {_pct(c['fpr'])}  {_pct(c['precision'])}"
        )
    lines.append("固定误报率下的召回")
    for r in summary["recall_at_fpr"]:
        th = "无可行阈值" if r["threshold"] is None else f"阈值 {r['threshold']:.2f}"
        lines.append(f"  误报率 ≤ {r['max_fpr'] * 100:4.1f}%：召回 {_pct(r['recall'])}（{th}，实际误报率 {_pct(r['fpr'])}）")

    tw = summary["three_way"]
    lines.append(f"三支结论分布（只按阈值 {tw['high']:.2f} / {tw['low']:.2f}，不含硬性约束）")
    lines.append(f"  诈骗样本：{tw['fraud']}")
    lines.append(f"  正常样本：{tw['normal']}")
    lines.append("报告实际 verdict 分布（含硬性约束）")
    lines.append(f"  诈骗样本：{summary['verdicts']['fraud']}")
    lines.append(f"  正常样本：{summary['verdicts']['normal']}")

    lines.append("按数据来源")
    for src, b in summary["by_source"].items():
        c = b["at_high_threshold"]
        lines.append(
            f"  {src}: {b['n']} 条（诈骗 {b['fraud']} / 正常 {b['normal']}），"
            f"阈值 {HIGH_THRESHOLD:.2f} 召回 {_pct(c['recall']).strip()} 误报率 {_pct(c['fpr']).strip()}；"
            f"三支 诈骗{b['three_way']['fraud']} 正常{b['three_way']['normal']}"
        )

    lat = summary["latency_ms"]
    lines.append(f"整单时延（毫秒）：P50 {_num(lat['p50'])}  P90 {_num(lat['p90'])}  P95 {_num(lat['p95'])}  最大 {_num(lat['max'])}")
    for name, a in summary["analyzer_latency_ms"].items():
        lines.append(
            f"  {name:<9} P50 {_num(a['p50'])}  P95 {_num(a['p95'])}  状态 {summary['analyzer_status'][name]}"
        )
    lines.append(f"降级标记出现次数：{summary['degraded'] or '无'}")
    return lines


def format_comparison(summaries: dict[str, dict[str, Any]]) -> list[str]:
    if len(summaries) < 2:
        return []
    lines = ["===== 对照（CROSS_CHECK）====="]
    names = list(summaries)
    lines.append("  指标" + "".join(f"  {n:>14}" for n in names))

    def row(label: str, values: list[str]) -> None:
        lines.append(f"  {label}" + "".join(f"  {v:>14}" for v in values))

    def at(s: dict, t: float) -> dict:
        return next((c for c in s["confusion"] if abs(c["threshold"] - t) < 1e-9), confusion_at([], [], t).as_dict())

    row(f"召回@{HIGH_THRESHOLD:.2f}", [_pct(at(summaries[n], HIGH_THRESHOLD)["recall"]) for n in names])
    row(f"误报率@{HIGH_THRESHOLD:.2f}", [_pct(at(summaries[n], HIGH_THRESHOLD)["fpr"]) for n in names])
    for i, t in enumerate(summaries[names[0]]["recall_at_fpr"]):
        row(f"召回@误报≤{t['max_fpr'] * 100:.0f}%", [_pct(summaries[n]["recall_at_fpr"][i]["recall"]) for n in names])
    row("时延P50", [_num(summaries[n]["latency_ms"]["p50"]) for n in names])
    row("时延P95", [_num(summaries[n]["latency_ms"]["p95"]) for n in names])
    row("复核不一致", [str(summaries[n]["degraded"].get(DegradedFlag.CROSS_CHECK_DISAGREED.value, 0)) for n in names])
    row("只用规则", [str(summaries[n]["rules_only"]) for n in names])
    if all(summaries[n]["rules_only"] == summaries[n]["n_ok"] for n in names):
        lines.append("  ！！！ 所有设置下都只用了规则，大模型没有参与：这个对照不能用来决定开不开交叉复核")
    return lines


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def _force_utf8_console() -> None:
    """中文 Windows 控制台默认 GBK，强制 UTF-8 避免乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def cross_check_configs(choice: str, settings: Settings) -> list[str]:
    if choice == "both":
        return ["off", "parallel"]
    if choice == "config":
        return [settings.cross_check]
    return [choice]


async def evaluate(
    samples: Sequence[EvalSample],
    settings: Settings,
    *,
    cross_check: Sequence[str] = ("off", "parallel"),
    analyzers: AnalyzersFactory | None = None,
    summarizer: SummarizerFactory | None = None,
) -> dict[str, Any]:
    """对每种 CROSS_CHECK 设置各跑一遍，返回可写成 JSON 的结果。"""
    out: dict[str, Any] = {"configs": {}, "samples": {}}
    for mode in cross_check:
        s = settings.model_copy(update={"cross_check": mode})
        results = await run_samples(samples, s, analyzers=analyzers, summarizer=summarizer)
        name = f"CROSS_CHECK={mode}"
        out["configs"][name] = summarize(results)
        out["samples"][name] = [r.__dict__ for r in results]
    return out


def write_result(result: dict[str, Any], var_dir: Path, meta: dict[str, Any]) -> Path:
    out_dir = var_dir / "eval"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"eval-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    payload = json.dumps({"meta": meta, **result}, ensure_ascii=False, indent=2)
    path.write_text(payload, encoding="utf-8")
    return path


async def _main(args: argparse.Namespace) -> int:
    settings = get_settings()
    out_var_dir = settings.var_dir
    files = find_data_files(args.paths)
    if not files:
        print("没有找到评测数据（eval/data/*.jsonl）")
        return 1
    loaded = load_samples(files)
    for err in loaded.errors:
        print(f"跳过不合格的行：{err}")
    if not loaded.samples:
        print("没有可用的样本")
        return 1

    tmp: tempfile.TemporaryDirectory | None = None
    if args.isolated:
        tmp = tempfile.TemporaryDirectory(prefix="antifraud-eval-", ignore_cleanup_errors=True)
        settings = settings.model_copy(update={"var_dir": Path(tmp.name), "db_path": None})
    try:
        print(f"数据文件：{', '.join(f.name for f in files)}；样本 {len(loaded.samples)} 条")
        print(f"数据库：{settings.database_path}" + ("（临时，案例库为空）" if args.isolated else ""))
        modes = cross_check_configs(args.cross_check, settings)
        result = await evaluate(loaded.samples, settings, cross_check=modes)
    finally:
        if tmp is not None:
            tmp.cleanup()

    for name, summary in result["configs"].items():
        print()
        for line in format_summary(name, summary):
            print(line)
    comparison = format_comparison(result["configs"])
    if comparison:
        print()
        for line in comparison:
            print(line)

    meta = {
        "files": [f.name for f in files],
        "skipped_rows": loaded.errors,
        "isolated": args.isolated,
        "database": None if args.isolated else str(settings.database_path),
        "models": {"primary": settings.llm_primary_model, "backup": settings.llm_backup_model, "embedding": settings.embedding_model},
        "cases_similarity_threshold": settings.cases_similarity_threshold,
    }
    path = write_result(result, out_var_dir, meta)
    print()
    print(f"结果已写入：{path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    _force_utf8_console()
    parser = argparse.ArgumentParser(prog="python -m eval.run_eval", description="不经过 HTTP 跑流水线并统计指标")
    parser.add_argument("paths", nargs="*", type=Path, help="评测数据文件或目录，默认 eval/data/*.jsonl")
    parser.add_argument(
        "--cross-check",
        choices=["both", "off", "parallel", "config"],
        default="both",
        help="both：off 和 parallel 各跑一遍做对照（默认）；config：按 .env 的设置跑一遍",
    )
    parser.add_argument("--isolated", action="store_true", help="用临时数据库（案例库为空，相似案例不参与）")
    args = parser.parse_args(argv)
    return asyncio.run(_main(args))


if __name__ == "__main__":
    sys.exit(main())
