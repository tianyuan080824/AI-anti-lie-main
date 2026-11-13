"""契约示例的解析与序列化：shared/contract-examples/ 下每个文件都必须能被模型解析，并原样序列化回去。"""

import json

import pytest
from pydantic import ValidationError

from app import contract as c
from app.paths import CONTRACT_EXAMPLES_DIR

EXAMPLES = {
    "media-upload-response.json": c.MediaUploadResponse,
    "check-request.json": c.CheckRequest,
    "check-created.json": c.CheckCreated,
    "check-status-running.json": c.CheckStatusResponse,
    "feedback-request.json": c.FeedbackRequest,
    "rules-response.json": c.RulesFile,
    "health.json": c.HealthResponse,
    "report.json": c.Report,
    "error.json": c.ErrorResponse,
}


def load(name: str):
    return json.loads((CONTRACT_EXAMPLES_DIR / name).read_text(encoding="utf-8"))


def test_every_example_file_is_covered():
    on_disk = {p.name for p in CONTRACT_EXAMPLES_DIR.glob("*.json")}
    assert on_disk == set(EXAMPLES), "新增示例文件后请在这里登记对应模型"


@pytest.mark.parametrize("name,model", sorted(EXAMPLES.items()))
def test_example_roundtrip(name, model):
    raw = load(name)
    parsed = model.model_validate(raw)
    assert parsed.model_dump(mode="json") == raw


def test_request_ignores_unknown_fields_and_values():
    raw = load("check-request.json")
    raw["future_field"] = 1
    raw["items"][0]["lines"][0]["speaker"] = "bot"
    raw["items"][0]["lines"][0]["extra"] = "x"
    raw["items"].append({"type": "sticker", "sticker_id": "s1"})
    req = c.CheckRequest.model_validate(raw)
    assert req.items[0].lines[0].speaker == c.Speaker.UNKNOWN
    assert isinstance(req.items[-1], c.UnknownItem)


def test_report_scene_none_requires_null_stage():
    raw = load("report.json")
    raw["scene"] = "none"
    with pytest.raises(ValidationError):
        c.Report.model_validate(raw)
    raw["stage"] = None
    c.Report.model_validate(raw)


def test_synthetic_case_must_not_have_source_url():
    raw = load("report.json")["similar_cases"][0]
    raw["synthetic"] = True
    with pytest.raises(ValidationError):
        c.SimilarCase.model_validate(raw)


def test_report_rejects_unknown_verdict():
    raw = load("report.json")
    raw["verdict"] = "safe"
    with pytest.raises(ValidationError):
        c.Report.model_validate(raw)
