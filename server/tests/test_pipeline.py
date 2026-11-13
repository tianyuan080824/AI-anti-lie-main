"""流水线：占位分析器下的完整流程；单个分析器超时、抛异常、返回非法结果时报告照常产出。"""

from __future__ import annotations

import asyncio

import pytest

from app.analyzers import load_analyzers
from app.analyzers.base import Analyzer, AnalysisInput, AnalyzerContext, AnalyzerOutput
from app.contract import AnalyzerName, AnalyzerStatus, DegradedFlag, Report
from app.pipeline import open_pipeline, run_check
from app.pipeline.runner import Pipeline
from tests.conftest import AUTH, chat_request, poll_until_done


def replace(name: AnalyzerName, analyzer: Analyzer) -> list[Analyzer]:
    return [analyzer if a.name == name else a for a in load_analyzers()]


class Hang(Analyzer):
    def __init__(self, name: AnalyzerName, swallow_cancel: bool = False):
        self.name = name
        self.swallow_cancel = swallow_cancel

    async def run(self, inp, ctx):
        if not self.swallow_cancel:
            await asyncio.sleep(3600)
        # 吞掉取消的坏分析器：流水线也不应该被它拖住
        end = asyncio.get_running_loop().time() + 2
        while asyncio.get_running_loop().time() < end:
            try:
                await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                pass
        return AnalyzerOutput()


class Boom(Analyzer):
    def __init__(self, name: AnalyzerName):
        self.name = name

    async def run(self, inp, ctx):
        raise RuntimeError("secret text must not leak")


class BadOutput(Analyzer):
    def __init__(self, name: AnalyzerName):
        self.name = name

    async def run(self, inp, ctx):
        return {"status": "done", "links": [{"url": "x", "risk": "very-high"}]}


class Recorder(Analyzer):
    name = AnalyzerName.LINK

    def __init__(self):
        self.seen: list[AnalysisInput] = []
        self.ctx: AnalyzerContext | None = None

    def should_run(self, inp):
        return bool(inp.urls)

    async def run(self, inp, ctx):
        self.seen.append(inp)
        self.ctx = ctx
        return AnalyzerOutput(status=AnalyzerStatus.DONE, extras={"n": len(inp.urls)})


REQ = chat_request("日结兼职，点赞截图一单3元", "先垫付返利", urls=("https://t.example.cn/a1",))
# 测试配置里没有大模型密钥，话术这一路只跑规则，报告里总带着这个标记
NO_KEYS = {DegradedFlag.LLM_UNAVAILABLE_RULES_ONLY}


async def test_run_check_without_http(settings):
    result = await run_check(REQ, settings)
    assert isinstance(result.report, Report)
    assert [a.name for a in result.analyzers] == list(AnalyzerName)
    assert set(result.runs) == {AnalyzerName.SCRIPT, AnalyzerName.LINK, AnalyzerName.CASES, AnalyzerName.DEEPFAKE}
    assert result.runs[AnalyzerName.DEEPFAKE].status == AnalyzerStatus.SKIPPED  # 没有媒体
    assert result.runs[AnalyzerName.LINK].output.links[0].url == "https://t.example.cn/a1"
    assert set(result.report.degraded) == NO_KEYS
    assert result.summary_error is None
    assert result.elapsed_ms >= 0


async def test_open_pipeline_reuses_analyzers_and_exposes_extras(settings):
    recorder = Recorder()
    async with open_pipeline(settings, analyzers=replace(AnalyzerName.LINK, recorder)) as pipe:
        r1 = await pipe.analyze(REQ)
        r2 = await pipe.analyze(chat_request("只有文字没有链接的一段话"))
    assert len(recorder.seen) == 1  # 第二次没有链接，link 被跳过
    assert r1.runs[AnalyzerName.LINK].output.extras == {"n": 1}
    assert r2.runs[AnalyzerName.LINK].status == AnalyzerStatus.SKIPPED
    assert recorder.ctx is not None and recorder.ctx.settings is settings
    assert recorder.ctx.remaining_s() is not None


async def test_analyzer_receives_validated_redacted_input(settings):
    recorder = Recorder()
    async with open_pipeline(settings, analyzers=replace(AnalyzerName.LINK, recorder)) as pipe:
        await pipe.analyze(REQ)
    inp = recorder.seen[0]
    assert [l.line_id for l in inp.lines] == ["0-0", "0-1"]
    assert inp.lines[0].item_index == 0 and inp.lines[0].origin == "pasted_text"
    assert inp.urls[0].item_index == 1 and inp.urls[0].origin == "typed"
    assert inp.check_id.startswith("c_") and inp.trace_id.startswith("t_")


@pytest.mark.parametrize(
    "name,flags",
    [
        (AnalyzerName.SCRIPT, {DegradedFlag.LLM_UNAVAILABLE_RULES_ONLY}),
        (AnalyzerName.LINK, {DegradedFlag.INTEL_UNAVAILABLE}),
        (AnalyzerName.CASES, {DegradedFlag.CASES_UNAVAILABLE}),
    ],
)
async def test_one_analyzer_raises_report_still_produced(settings, name, flags):
    async with open_pipeline(settings, analyzers=replace(name, Boom(name))) as pipe:
        result = await pipe.analyze(REQ)
    assert result.runs[name].status == AnalyzerStatus.FAILED
    assert result.runs[name].error == "RuntimeError"
    assert set(result.report.degraded) == flags | NO_KEYS
    others = [n for n in result.runs if n not in (name, AnalyzerName.DEEPFAKE)]
    # 其他路不受影响（案例库为空时 cases 是 skipped，也算正常）
    assert all(result.runs[n].status in (AnalyzerStatus.DONE, AnalyzerStatus.SKIPPED) for n in others)
    assert result.analyzers[-1].status == AnalyzerStatus.DONE  # 汇总照常完成


@pytest.mark.parametrize("swallow", [False, True])
async def test_one_analyzer_times_out(make_settings, swallow):
    settings = make_settings(timeout_cases_s=0.2)
    loop = asyncio.get_running_loop()
    async with open_pipeline(settings, analyzers=replace(AnalyzerName.CASES, Hang(AnalyzerName.CASES, swallow))) as pipe:
        t0 = loop.time()
        result = await pipe.analyze(REQ)
        took = loop.time() - t0
    assert took < 1.0, took
    run = result.runs[AnalyzerName.CASES]
    assert run.status == AnalyzerStatus.FAILED and run.error == "timeout"
    assert 150 <= run.elapsed_ms <= 900
    assert set(result.report.degraded) == {DegradedFlag.CASES_UNAVAILABLE, DegradedFlag.PARTIAL_TIMEOUT} | NO_KEYS
    assert result.runs[AnalyzerName.SCRIPT].status == AnalyzerStatus.DONE


async def test_whole_check_deadline_caps_analyzer_timeouts(make_settings):
    settings = make_settings(check_deadline_s=0.6, timeout_summary_s=0.2, timeout_script_s=12)
    loop = asyncio.get_running_loop()
    async with open_pipeline(settings, analyzers=replace(AnalyzerName.SCRIPT, Hang(AnalyzerName.SCRIPT))) as pipe:
        t0 = loop.time()
        result = await pipe.analyze(REQ)
        assert loop.time() - t0 < 1.0
    assert result.runs[AnalyzerName.SCRIPT].error == "timeout"


async def test_invalid_analyzer_output_is_a_failure(settings):
    async with open_pipeline(settings, analyzers=replace(AnalyzerName.LINK, BadOutput(AnalyzerName.LINK))) as pipe:
        result = await pipe.analyze(REQ)
    assert result.runs[AnalyzerName.LINK].status == AnalyzerStatus.FAILED
    assert result.runs[AnalyzerName.LINK].error.startswith("invalid_output")
    # 链接这一路失败，用户提交的链接仍然出现在报告里（风险未知）
    assert [l.url for l in result.report.links] == ["https://t.example.cn/a1"]
    assert result.report.links[0].risk.value == "unknown"


async def test_should_run_raising_is_a_failure(settings):
    class BadGate(Boom):
        def should_run(self, inp):
            raise ValueError

    async with open_pipeline(settings, analyzers=replace(AnalyzerName.CASES, BadGate(AnalyzerName.CASES))) as pipe:
        result = await pipe.analyze(REQ)
    assert result.runs[AnalyzerName.CASES].status == AnalyzerStatus.FAILED
    assert DegradedFlag.CASES_UNAVAILABLE in result.report.degraded


async def test_analyzer_reported_failure_gets_default_flags(settings):
    class SoftFail(Analyzer):
        name = AnalyzerName.CASES

        async def run(self, inp, ctx):
            return AnalyzerOutput(status=AnalyzerStatus.FAILED)

    async with open_pipeline(settings, analyzers=replace(AnalyzerName.CASES, SoftFail())) as pipe:
        result = await pipe.analyze(REQ)
    assert result.runs[AnalyzerName.CASES].error is None
    assert set(result.report.degraded) == {DegradedFlag.CASES_UNAVAILABLE} | NO_KEYS


async def test_summary_failure_uses_fallback_report(settings):
    class BrokenSummary:
        async def summarize(self, inp, runs, ctx):
            raise KeyError("x")

    async with open_pipeline(settings, summarizer=BrokenSummary()) as pipe:
        result = await pipe.analyze(REQ)
    assert result.summary_error == "KeyError"
    assert result.analyzers[-1].status == AnalyzerStatus.FAILED
    assert result.report.verdict.value == "suspicious"
    assert DegradedFlag.PARTIAL_TIMEOUT in result.report.degraded
    assert [l.url for l in result.report.links] == ["https://t.example.cn/a1"]


async def test_too_little_text_is_inconclusive(settings):
    result = await run_check(chat_request("在吗"), settings)
    assert result.report.verdict.value == "inconclusive"
    assert result.report.scene.value == "none" and result.report.stage is None


async def test_startup_failure_does_not_stop_pipeline(settings):
    class BadStartup(Recorder):
        async def startup(self, services):
            raise OSError("data file missing")

    async with open_pipeline(settings, analyzers=replace(AnalyzerName.LINK, BadStartup())) as pipe:
        assert pipe.pipeline.startup_errors == {AnalyzerName.LINK: "OSError"}
        assert "启动失败" in pipe.pipeline.status_lines()[AnalyzerName.LINK]
        result = await pipe.analyze(REQ)
    assert isinstance(result.report, Report)


def test_broken_analyzer_package_degrades(monkeypatch, settings):
    import app.analyzers as registry

    monkeypatch.setitem(registry.PACKAGES, AnalyzerName.CASES, "app.analyzers.does_not_exist")
    analyzers = load_analyzers()
    cases = next(a for a in analyzers if a.name == AnalyzerName.CASES)
    assert type(cases).__name__ == "BrokenAnalyzer"


def test_pipeline_requires_all_four_analyzers(settings):
    from app.services import Services

    with pytest.raises(ValueError):
        Pipeline(Services(settings=settings, db=None), analyzers=load_analyzers()[:3])  # type: ignore[arg-type]


def test_failure_visible_through_http(make_client, make_settings):
    def factory(services):
        return Pipeline(services, replace(AnalyzerName.SCRIPT, Boom(AnalyzerName.SCRIPT)))

    client = make_client(make_settings(), pipeline_factory=factory)
    check_id = client.post("/v1/checks", json=REQ, headers=AUTH).json()["check_id"]
    body = poll_until_done(client, check_id)
    assert body["status"] == "succeeded"
    states = {a["name"]: a for a in body["analyzers"]}
    assert states["script"]["status"] == "failed" and "elapsed_ms" in states["script"]
    assert "llm_unavailable_rules_only" in body["report"]["degraded"]


def test_task_level_failure_returns_200_with_error(make_client, make_settings):
    class ExplodingPipeline(Pipeline):
        async def run(self, inp, on_update=None):
            raise RuntimeError("boom")

    client = make_client(make_settings(), pipeline_factory=lambda s: ExplodingPipeline(s))
    check_id = client.post("/v1/checks", json=REQ, headers=AUTH).json()["check_id"]
    body = poll_until_done(client, check_id)
    assert body["status"] == "failed"
    assert body["report"] is None
    assert body["error"]["code"] == "INTERNAL" and body["error"]["retryable"] is True
    assert all(a["status"] in ("failed", "done", "skipped") for a in body["analyzers"])
