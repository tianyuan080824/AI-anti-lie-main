"""评测脚本：用注入的假分析器和假汇总从头跑到尾，结果确定。"""

from __future__ import annotations

import json

import pytest

from app.analyzers.base import Analyzer, AnalyzerOutput
from app.contract import AnalyzerName, AnalyzerStatus, Confidence, DegradedFlag, Report, Scene, Verdict
from app.summary import Summarizer
from eval.dataset import EvalSample, load_samples, parse_sample
from eval.run_eval import evaluate, format_comparison, format_summary, summarize, warnings_for, write_result

# 每条样本第一行文字里的标记决定它的风险分
SCORES = {"F1": 90, "F2": 70, "F3": 40, "F4": 20, "N1": 65, "N2": 10, "N3": 3, "N4": 0}


class FakeScript(Analyzer):
    name = AnalyzerName.SCRIPT

    def __init__(self, rules_only_markers: set[str] = frozenset()):
        self.rules_only_markers = rules_only_markers
        self.cross_check_seen: list[str] = []

    async def run(self, inp, ctx):
        self.cross_check_seen.append(ctx.settings.cross_check)
        marker = inp.lines[0].text.split()[0]
        degraded = [DegradedFlag.LLM_UNAVAILABLE_RULES_ONLY] if marker in self.rules_only_markers else []
        return AnalyzerOutput(status=AnalyzerStatus.DONE, degraded=degraded, extras={"marker": marker})


class Skip(Analyzer):
    def __init__(self, name: AnalyzerName):
        self.name = name

    def should_run(self, inp):
        return False

    async def run(self, inp, ctx):  # pragma: no cover
        raise AssertionError


class FakeSummarizer(Summarizer):
    async def summarize(self, inp, runs, ctx):
        marker = runs[AnalyzerName.SCRIPT].output.extras["marker"]
        score = SCORES[marker]
        verdict = Verdict.HIGH_RISK if score >= 60 else Verdict.LOW_RISK if score <= 5 else Verdict.SUSPICIOUS
        return Report(
            verdict=verdict,
            risk_score=score,
            confidence=Confidence.MEDIUM,
            scene=Scene.OTHER,
            stage=None,
            headline="测试",
            summary="",
            next_move="",
            disclaimer="测试",
        )


def _samples() -> list[EvalSample]:
    rows = []
    for i, marker in enumerate(SCORES):
        rows.append(
            {
                "id": f"s{i}",
                "label": "fraud" if marker.startswith("F") else "normal",
                "source": "team" if i % 2 == 0 else "synthetic",
                "lines": [{"speaker": "other", "text": f"{marker} 这是一段用于测试的对话内容"}],
            }
        )
    return [parse_sample(r) for r in rows]


@pytest.fixture
def fakes():
    script = FakeScript(rules_only_markers={"F1", "N1", "N2"})
    analyzers = lambda: [script, Skip(AnalyzerName.LINK), Skip(AnalyzerName.CASES), Skip(AnalyzerName.DEEPFAKE)]
    return script, analyzers, FakeSummarizer


async def test_evaluate_end_to_end_with_fakes(settings, fakes):
    script, analyzers, summarizer = fakes
    result = await evaluate(_samples(), settings, cross_check=("off", "parallel"), analyzers=analyzers, summarizer=summarizer)

    assert list(result["configs"]) == ["CROSS_CHECK=off", "CROSS_CHECK=parallel"]
    # 两种设置都真的传到了分析器
    assert script.cross_check_seen == ["off"] * 8 + ["parallel"] * 8

    s = result["configs"]["CROSS_CHECK=off"]
    assert s["n_ok"] == 8 and s["labels"] == {"fraud": 4, "normal": 4}
    at60 = next(c for c in s["confusion"] if c["threshold"] == 0.6)
    assert (at60["tp"], at60["fp"], at60["tn"], at60["fn"]) == (2, 1, 3, 2)
    r25 = next(r for r in s["recall_at_fpr"] if r["max_fpr"] == 0.10)
    assert (r25["threshold"], r25["recall"]) == (0.7, 0.5)
    assert s["three_way"]["fraud"] == {"high_risk": 2, "suspicious": 2}
    assert s["three_way"]["normal"] == {"high_risk": 1, "low_risk": 2, "suspicious": 1}
    assert s["verdicts"]["normal"] == {"high_risk": 1, "low_risk": 2, "suspicious": 1}
    assert s["rules_only"] == 3 and s["degraded"] == {"llm_unavailable_rules_only": 3}
    assert s["cases_participated"] == 0 and s["placeholder_reports"] == 0
    assert s["analyzer_status"]["cases"] == {"skipped": 8} and s["analyzer_status"]["script"] == {"done": 8}
    assert s["latency_ms"]["n"] == 8

    # 按来源分组：team = F1 F3 N1 N3，synthetic = F2 F4 N2 N4
    team = s["by_source"]["team"]["at_high_threshold"]
    assert (team["tp"], team["fp"], team["tn"], team["fn"]) == (1, 1, 1, 1)
    syn = s["by_source"]["synthetic"]["at_high_threshold"]
    assert (syn["tp"], syn["fp"], syn["tn"], syn["fn"]) == (1, 0, 2, 1)

    text = "\n".join(format_summary("CROSS_CHECK=off", s))
    assert "3/8 条带 llm_unavailable_rules_only" in text and "只代表规则层" in text
    assert "相似案例这一路一条都没有参与" in text
    comparison = "\n".join(format_comparison(result["configs"]))
    assert "CROSS_CHECK=off" in comparison and "CROSS_CHECK=parallel" in comparison


async def test_all_rules_only_comparison_is_flagged(settings):
    script = FakeScript(rules_only_markers=set(SCORES))
    analyzers = lambda: [script, Skip(AnalyzerName.LINK), Skip(AnalyzerName.CASES), Skip(AnalyzerName.DEEPFAKE)]
    result = await evaluate(_samples(), settings, analyzers=analyzers, summarizer=FakeSummarizer)
    s = result["configs"]["CROSS_CHECK=off"]
    assert warnings_for(s)[0].startswith("8/8 条带 llm_unavailable_rules_only（全部")
    assert "不能用来决定开不开交叉复核" in "\n".join(format_comparison(result["configs"]))


async def test_invalid_request_is_recorded_not_fatal(settings, fakes):
    _, analyzers, summarizer = fakes
    too_long = parse_sample({"id": "bad", "label": "fraud", "source": "x", "lines": ["F1 " + "长" * 5000]})
    result = await evaluate(
        [too_long] + _samples()[:1], settings, cross_check=("off",), analyzers=analyzers, summarizer=summarizer
    )
    s = result["configs"]["CROSS_CHECK=off"]
    assert s["n_total"] == 2 and s["n_ok"] == 1
    assert s["invalid"][0]["id"] == "bad" and s["invalid"][0]["error"].startswith("INVALID_INPUT")


def test_summarize_handles_no_results():
    s = summarize([])
    assert s["n_ok"] == 0 and warnings_for(s) == ["没有成功跑完的样本"]


def test_write_result_goes_under_var_dir(settings):
    path = write_result({"configs": {}}, settings.var_dir, {"files": ["中文.jsonl"]})
    assert path.parent == settings.var_dir / "eval"
    assert json.loads(path.read_text(encoding="utf-8"))["meta"]["files"] == ["中文.jsonl"]


# ---- 数据格式 ----


def test_load_samples_validates_rows(tmp_path):
    f = tmp_path / "d.jsonl"
    rows = [
        {"id": "a", "label": "fraud", "source": "team", "lines": ["粘贴的文字"], "urls": ["https://x.example/1"]},
        {"id": "b", "label": "maybe", "source": "team", "lines": ["x"]},
        {"id": "c", "label": "normal", "lines": ["x"]},
        {"id": "a", "label": "normal", "source": "team", "lines": ["重复"]},
        {"id": "d", "label": "normal", "source": "team", "lines": [{"speaker": "boss", "text": "x"}]},
        {"id": "e", "label": "normal", "source": "team", "lines": []},
        {"id": "f", "label": "normal", "source": "team", "lines": ["x"], "extra": 1},
    ]
    f.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n{坏行\n", encoding="utf-8")
    loaded = load_samples([f])
    assert [s.id for s in loaded.samples] == ["a"]
    a = loaded.samples[0]
    assert a.lines == (("unknown", "粘贴的文字"),) and a.scene == "other" and a.positive
    assert [e.split()[0] for e in loaded.errors] == ["d.jsonl:2", "d.jsonl:3", "d.jsonl:4", "d.jsonl:5", "d.jsonl:6", "d.jsonl:7", "d.jsonl:8"]
    req = a.to_request()
    assert req["items"][0]["lines"][0] == {"line_id": "0-0", "speaker": "unknown", "text": "粘贴的文字"}
    assert req["items"][1] == {"type": "url", "url": "https://x.example/1", "origin": "typed"}


def test_shipped_synthetic_eval_data_is_valid():
    from eval.dataset import find_data_files

    loaded = load_samples(find_data_files())
    assert loaded.errors == []
    assert loaded.samples and all(s.source == "synthetic" for s in loaded.samples)
    assert {s.label for s in loaded.samples} == {"fraud", "normal"}


# ---- 命令行 ----


def test_cli_runs_with_real_pipeline_without_keys(make_settings, tmp_path, capsys):
    from app.config import override_settings
    from eval.run_eval import main

    settings = make_settings()
    data = tmp_path / "d.jsonl"
    data.write_text(
        json.dumps({"id": "x1", "label": "fraud", "source": "t", "lines": ["先垫付两百返两百六，下载这个应用接单"]}, ensure_ascii=False)
        + "\n"
        + json.dumps({"id": "x2", "label": "normal", "source": "t", "lines": ["周末一起去爬山吗，早上八点见"]}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    with override_settings(var_dir=settings.var_dir):
        assert main([str(data), "--cross-check", "off"]) == 0
    out = capsys.readouterr().out
    assert "===== 设置：CROSS_CHECK=off =====" in out and "混淆矩阵" in out
    written = list((settings.var_dir / "eval").glob("eval-*.json"))
    assert len(written) == 1 and str(settings.var_dir) in out
