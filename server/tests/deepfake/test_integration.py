"""媒体接进主流程后的端到端测试：上传 → 鉴定 → 报告。模型用假引擎，HTTP、流水线、汇总都是真的。"""

from __future__ import annotations

import asyncio
import io
import time

import numpy as np
import pytest

pytest.importorskip("PIL")
pytest.importorskip("python_multipart")

from PIL import Image

from app.analyzers import load_analyzers
from app.analyzers.deepfake import analyzer as analyzer_module
from app.analyzers.deepfake import router as router_module
from app.analyzers.deepfake import runtime as runtime_module
from app.analyzers.deepfake.analyzer import DeepfakeAnalyzer
from app.analyzers.deepfake.engines import Engine, Engines
from app.analyzers.deepfake.settings import MediaSettings
from app.analyzers.deepfake.text import OcrBox
from app.analyzers.script import ScriptAnalyzer
from app.contract import AnalyzerName, DegradedFlag, EvidenceSource
from app.pipeline import open_pipeline
from app.pipeline.runner import Pipeline
from tests.conftest import AUTH, chat_request, poll_until_done
from tests.llm_fakes import FakeBackend, quote_reply

OCR_TEXT = "你先下载这个软件，然后转账给我"
QUOTE = "转账给我"


def _png(width: int = 8, height: int = 8, *, noise: bool = False) -> bytes:
    if noise:
        pixels = np.random.default_rng(0).integers(0, 256, (height, width, 3), dtype=np.uint8)
    else:
        pixels = np.full((height, width, 3), 255, dtype=np.uint8)
    buf = io.BytesIO()
    Image.fromarray(pixels).save(buf, format="PNG", compress_level=0)
    return buf.getvalue()


class _Ocr:
    def __init__(self, text: str = OCR_TEXT, delay_s: float = 0.0) -> None:
        self.text = text
        self.delay_s = delay_s

    def recognize(self, bgr):
        if self.delay_s:
            time.sleep(self.delay_s)
        return [OcrBox(self.text, 0.99, ((0, 0), (200, 0), (200, 20), (0, 20)))]


def _engines(ocr) -> Engines:
    engines = Engines(
        ocr=Engine("ocr", lambda: ocr),
        asr=Engine("asr", lambda: None, unavailable="测试里不可用"),
        spoof=Engine("spoof", lambda: None, unavailable="测试里不可用"),
    )
    engines.start_all()
    return engines


def _analyzers(deepfake: DeepfakeAnalyzer, script: ScriptAnalyzer | None = None):
    out = []
    for a in load_analyzers():
        if a.name == AnalyzerName.DEEPFAKE:
            out.append(deepfake)
        elif a.name == AnalyzerName.SCRIPT and script is not None:
            out.append(script)
        else:
            out.append(a)
    return out


def _upload(client, data: bytes, kind: str = "image"):
    return client.post(
        "/v1/media",
        headers=AUTH,
        data={"kind": kind},
        files={"file": ("x.bin", data, "application/octet-stream")},
    )


def _media_request(media_id: str, *texts: str, kind: str = "image") -> dict:
    body = chat_request(*texts)
    body["items"].append({"type": kind, "media_id": media_id})
    return body


def _media_files(settings) -> list:
    directory = settings.var_dir / "media"
    return [p for p in directory.iterdir() if p.is_file()] if directory.is_dir() else []


# ---------------------------------------------------------------------------
# 派生行进入话术分析、证据回查、derived_lines
# ---------------------------------------------------------------------------


def test_ocr_lines_reach_script_and_evidence_passes_verification(make_client, settings) -> None:
    backend = FakeBackend("primary", [quote_reply("brushing_rebate", "s4_invest", [("request_payment", QUOTE)])])
    script = ScriptAnalyzer(backends={"primary": backend})
    deepfake = DeepfakeAnalyzer(ms=MediaSettings(preload=False), engines=_engines(_Ocr()))
    client = make_client(settings, pipeline_factory=lambda services: Pipeline(services, _analyzers(deepfake, script)))

    up = _upload(client, _png())
    assert up.status_code == 201, up.text
    media_id = up.json()["media_id"]
    assert len(_media_files(settings)) == 1

    res = client.post("/v1/checks", headers=AUTH, json=_media_request(media_id))
    assert res.status_code == 202, res.text
    body = poll_until_done(client, res.json()["check_id"])
    assert body["status"] == "succeeded", body

    derived = body["derived_lines"]
    assert derived and derived[0]["line_id"] == "d0-0"
    assert derived[0]["text"] == OCR_TEXT and derived[0]["item_index"] == 0

    # 模型看到了派生行（它就在 <chat_data> 里），给出的证据指向派生行，并通过了逐字回查
    assert any(OCR_TEXT in m["content"] for call in backend.calls for m in call)
    llm_evidence = [e for e in body["report"]["evidence"] if e["source"] == EvidenceSource.LLM.value]
    assert llm_evidence, body["report"]
    assert all(e["line_id"].startswith("d") for e in llm_evidence)
    assert llm_evidence[0]["quote"] == QUOTE and QUOTE in derived[0]["text"]

    media = body["report"]["media"]
    assert media[0]["media_id"] == media_id and media[0]["label"] == "not_analyzed"
    assert DegradedFlag.DEEPFAKE_UNAVAILABLE.value not in body["report"]["degraded"]
    # 分析后原文件已删除
    assert _media_files(settings) == []


async def test_fabricated_quote_against_derived_line_is_dropped(settings) -> None:
    backend = FakeBackend("primary", [quote_reply("brushing_rebate", None, [("request_payment", "根本没说过的话")])])
    deepfake = DeepfakeAnalyzer(ms=MediaSettings(preload=False), engines=_engines(_Ocr()))
    async with open_pipeline(settings, analyzers=_analyzers(deepfake, ScriptAnalyzer(backends={"primary": backend}))) as pipe:
        result = await pipe.analyze(_media_request("m_missing"))
    # 媒体不存在：没有派生行，编造的引文对不上任何一行，被丢掉
    assert result.derived_lines == []
    assert not [e for e in result.report.evidence if e.source == EvidenceSource.LLM]


# ---------------------------------------------------------------------------
# 预处理超时降级、预算不挤占话术
# ---------------------------------------------------------------------------


def test_preprocess_budget_exceeded_degrades_but_report_is_produced(make_client, settings, monkeypatch) -> None:
    async def must_not_run(*args, **kwargs):
        raise AssertionError("预处理已经给出结果，run() 不应再分析")

    monkeypatch.setattr(analyzer_module, "analyze_item", must_not_run)
    deepfake = DeepfakeAnalyzer(ms=MediaSettings(preload=False, preprocess_budget_s=0.3), engines=_engines(_Ocr(delay_s=0.8)))
    client = make_client(settings, pipeline_factory=lambda services: Pipeline(services, _analyzers(deepfake)))
    media_id = _upload(client, _png()).json()["media_id"]

    started = time.monotonic()
    res = client.post("/v1/checks", headers=AUTH, json=_media_request(media_id, "日结兼职，先垫付再返利"))
    body = poll_until_done(client, res.json()["check_id"])
    assert time.monotonic() - started < 5
    assert body["status"] == "succeeded"
    report = body["report"]
    assert report["media"][0]["label"] == "not_analyzed"
    assert DegradedFlag.DEEPFAKE_UNAVAILABLE.value in report["degraded"]
    assert body["derived_lines"] == []
    states = {a["name"]: a["status"] for a in body["analyzers"]}
    assert states["script"] == "done" and states["deepfake"] == "done"
    assert report["evidence"], "其余各路照常出结果"


async def test_hung_prepare_is_abandoned_and_does_not_eat_script_budget(settings, monkeypatch) -> None:
    """预处理卡死：到点放弃并取消，媒体记 not_analyzed（分析超时）+ deepfake_unavailable，run() 不再重新分析；
    预算压在「截止 − 汇总 − 话术」之内。"""
    seen = {"entered": False, "cancelled": False}

    async def hang(*args, **kwargs):
        seen["entered"] = True
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            seen["cancelled"] = True
            raise

    async def must_not_run(*args, **kwargs):
        raise AssertionError("预处理被放弃后 run() 不应再分析")

    monkeypatch.setattr(analyzer_module, "preprocess_media", hang)
    monkeypatch.setattr(analyzer_module, "analyze_item", must_not_run)
    tight = settings.model_copy(update={"check_deadline_s": 3.0, "timeout_summary_s": 0.5, "timeout_script_s": 2.0})
    deepfake = DeepfakeAnalyzer(ms=MediaSettings(preload=False, preprocess_budget_s=6), engines=_engines(_Ocr()))
    async with open_pipeline(tight, analyzers=_analyzers(deepfake)) as pipe:
        t0 = time.monotonic()
        result = await pipe.analyze(_media_request("m_any", "日结兼职，先垫付再返利"))
        elapsed = time.monotonic() - t0
        await asyncio.sleep(0)
    # 预处理只能用 3 − 0.5 − 2 = 0.5 秒
    assert elapsed < 1.5
    assert seen == {"entered": True, "cancelled": True}
    assert result.runs[AnalyzerName.SCRIPT].status == "done"
    media = result.report.media[0]
    assert media.label == "not_analyzed" and "超时" in (media.note or "")
    assert DegradedFlag.DEEPFAKE_UNAVAILABLE in result.report.degraded


async def test_prepare_gets_no_time_when_deadline_leaves_none(settings, monkeypatch) -> None:
    calls = []

    async def record(inp, services, engines, ms):
        calls.append(ms.preprocess_budget_s)
        return inp, analyzer_module.Prepared()

    monkeypatch.setattr(analyzer_module, "preprocess_media", record)
    tight = settings.model_copy(update={"check_deadline_s": 2.0, "timeout_summary_s": 0.5, "timeout_script_s": 2.0})
    deepfake = DeepfakeAnalyzer(ms=MediaSettings(preload=False), engines=_engines(_Ocr()))
    async with open_pipeline(tight, analyzers=_analyzers(deepfake)) as pipe:
        result = await pipe.analyze(_media_request("m_any", "日结兼职，先垫付再返利"))
    assert calls in ([], [0.0])
    assert result.report.media[0].label == "not_analyzed"
    assert DegradedFlag.DEEPFAKE_UNAVAILABLE in result.report.degraded


# ---------------------------------------------------------------------------
# 上传大小：不再被 1 MB 的请求体上限拦住
# ---------------------------------------------------------------------------


def test_upload_over_one_megabyte_within_type_limit_is_accepted(client, settings) -> None:
    data = _png(700, 700, noise=True)
    assert settings.limit_body_bytes < len(data) < settings.limit_image_bytes
    res = _upload(client, data)
    assert res.status_code == 201, res.text
    assert res.json()["size_bytes"] == len(data)


def test_upload_over_type_limit_is_rejected(make_client, make_settings) -> None:
    data = _png(700, 700, noise=True)
    client = make_client(make_settings(limit_image_bytes=1_200_000))
    res = _upload(client, data)
    assert res.status_code == 413
    assert res.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


def test_upload_over_every_type_limit_is_rejected_before_reading(make_client, make_settings) -> None:
    small = make_settings(limit_image_bytes=100_000, limit_audio_bytes=100_000, limit_video_bytes=100_000)
    res = _upload(make_client(small), _png(700, 700, noise=True))
    assert res.status_code == 413
    assert res.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


def test_checks_body_limit_is_not_raised_with_media(make_client, make_settings) -> None:
    """放宽的只有 /v1/media：鉴定请求仍按 limit_body_bytes 卡（这里调小到 2000 字节，内容本身合法）。"""
    client = make_client(make_settings(limit_body_bytes=2000))
    body = chat_request(*["日结兼职先垫付再返利" * 5] * 10)
    res = client.post("/v1/checks", headers=AUTH, json=body)
    assert res.status_code == 400
    assert res.json()["error"]["message"].startswith("提交的内容太多")
    up = _upload(client, _png(64, 64, noise=True))
    assert up.status_code == 201, up.text


def test_media_upload_without_content_length_is_rejected(client, settings) -> None:
    boundary = "b0undary"
    payload = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"kind\"\r\n\r\nimage\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"x\"\r\n"
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + _png() + f"\r\n--{boundary}--\r\n".encode()

    def chunks():
        yield payload

    res = client.post(
        "/v1/media",
        headers={**AUTH, "Content-Type": f"multipart/form-data; boundary={boundary}"},
        content=chunks(),
    )
    assert res.status_code == 400
    assert "Content-Length" in res.json()["error"]["message"]
    assert _media_files(settings) == []


def _ffmpeg_ok() -> bool:
    from app.analyzers.deepfake.env import find_tools
    from app.config import Settings

    return find_tools(Settings(_env_file=None)).ok


@pytest.mark.skipif(not _ffmpeg_ok(), reason="需要 ffmpeg")
def test_audio_upload_over_one_megabyte_is_accepted(client, settings) -> None:
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16_000)
        w.writeframes(b"\x00\x00" * 16_000 * 40)
    data = buf.getvalue()
    assert settings.limit_body_bytes < len(data) < settings.limit_audio_bytes
    res = _upload(client, data, kind="audio")
    assert res.status_code == 201, res.text


# ---------------------------------------------------------------------------
# 没装 media 依赖组、模拟模式
# ---------------------------------------------------------------------------


def test_without_media_extra_upload_is_unavailable(client, monkeypatch) -> None:
    monkeypatch.setattr(router_module, "has_module", lambda name: False)
    res = _upload(client, _png())
    assert res.status_code == 503
    assert res.json()["error"]["code"] == "UNAVAILABLE"


def test_without_models_check_reports_not_analyzed(client, settings, monkeypatch) -> None:
    media_id = _upload(client, _png()).json()["media_id"]
    # 默认的分析器在第一次用到时才建引擎（测试里不预加载），这时假装依赖都没装
    monkeypatch.setattr(runtime_module, "has_module", lambda name: False)
    res = client.post("/v1/checks", headers=AUTH, json=_media_request(media_id, "日结兼职，先垫付再返利"))
    body = poll_until_done(client, res.json()["check_id"])
    assert body["status"] == "succeeded"
    assert body["report"]["media"][0]["label"] == "not_analyzed"
    assert DegradedFlag.DEEPFAKE_UNAVAILABLE.value in body["report"]["degraded"]
    assert body["derived_lines"] == []


def test_mock_mode_accepts_upload_and_returns_mock_report(make_client, make_settings) -> None:
    client = make_client(make_settings(mock_mode=True, mock_step_ms=1))
    up = _upload(client, _png())
    assert up.status_code == 201, up.text
    res = client.post("/v1/checks", headers=AUTH, json=_media_request(up.json()["media_id"], "日结兼职"))
    body = poll_until_done(client, res.json()["check_id"])
    assert body["status"] == "succeeded"
    assert body["derived_lines"] == []


# ---------------------------------------------------------------------------
# 链接那一路也能看到派生行；同一个媒体只用一次；保留的行编号
# ---------------------------------------------------------------------------


async def test_url_in_ocr_text_reaches_link_analyzer(settings) -> None:
    deepfake = DeepfakeAnalyzer(ms=MediaSettings(preload=False), engines=_engines(_Ocr("点这个 http://10.0.0.1/pay 领取返利")))
    async with open_pipeline(settings, analyzers=_analyzers(deepfake)) as pipe:
        from app.analyzers.deepfake.storage import add, file_path, new_file_name
        from app.analyzers.deepfake.models import MediaRecord
        from app.db import utcnow
        from datetime import timedelta

        services = pipe.pipeline.services
        name = new_file_name()
        path = file_path(services.settings, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_png())
        now = utcnow()
        await add(
            services.db,
            MediaRecord(media_id="m_link", kind="image", file_name=name, size_bytes=1, format_name="png",
                        created_at=now, expires_at=now + timedelta(minutes=10)),
        )
        result = await pipe.analyze(_media_request("m_link"))
    assert result.derived_lines and result.derived_lines[0].line_id == "d0-0"
    link = result.runs[AnalyzerName.LINK]
    assert link.status == "done"
    assert any("10.0.0.1" in item.url for item in link.output.links)


def test_media_is_single_use_across_checks(make_client, settings) -> None:
    deepfake = DeepfakeAnalyzer(ms=MediaSettings(preload=False), engines=_engines(_Ocr()))
    client = make_client(settings, pipeline_factory=lambda services: Pipeline(services, _analyzers(deepfake)))
    media_id = _upload(client, _png()).json()["media_id"]
    first = poll_until_done(client, client.post("/v1/checks", headers=AUTH, json=_media_request(media_id)).json()["check_id"])
    second = poll_until_done(client, client.post("/v1/checks", headers=AUTH, json=_media_request(media_id)).json()["check_id"])
    assert first["derived_lines"]
    assert second["derived_lines"] == []
    assert "已经分析过" in second["report"]["media"][0]["note"]


def test_duplicate_media_id_in_one_check_is_rejected(client) -> None:
    body = _media_request("m_same")
    body["items"].append({"type": "image", "media_id": "m_same"})
    res = client.post("/v1/checks", headers=AUTH, json=body)
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "INVALID_INPUT"


def test_client_line_ids_cannot_use_derived_form(client) -> None:
    body = chat_request("日结兼职")
    body["items"][0]["lines"][0]["line_id"] = "d1-0"
    res = client.post("/v1/checks", headers=AUTH, json=body)
    assert res.status_code == 400
    ok = chat_request("日结兼职")
    ok["items"][0]["lines"][0]["line_id"] = "d-note"
    assert client.post("/v1/checks", headers=AUTH, json=ok).status_code == 202


async def test_cancelled_check_cancels_running_prepare(settings, monkeypatch) -> None:
    seen = {"cancelled": False}
    started = asyncio.Event()

    async def hang(*args, **kwargs):
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            seen["cancelled"] = True
            raise

    monkeypatch.setattr(analyzer_module, "preprocess_media", hang)
    deepfake = DeepfakeAnalyzer(ms=MediaSettings(preload=False), engines=_engines(_Ocr()))
    async with open_pipeline(settings, analyzers=_analyzers(deepfake)) as pipe:
        task = asyncio.create_task(pipe.analyze(_media_request("m_any", "日结兼职")))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        for _ in range(5):
            await asyncio.sleep(0)
    assert seen["cancelled"]


def test_weight_manifest_keys_match_engines() -> None:
    """runtime 用来找权重的 used_by 必须和清单里的一致，否则语音转写永远不可用。"""
    ms = MediaSettings(preload=False)
    assert runtime_module.manifest_files(ms, "asr")
    assert runtime_module.manifest_files(ms, "spoof")
