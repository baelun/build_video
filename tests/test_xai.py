"""xAI adapter 離線測試：用 httpx.MockTransport，絕不連線真實 API。"""

import base64
import json

import httpx
import pytest

from storybook_video.models import AudioMode, Character, JobStatus, ProjectSettings
from storybook_video.providers.base import (
    ImageRequest,
    PlanFormatError,
    PlanRequest,
    ProviderError,
    SubmissionUncertainError,
    VideoRequest,
)
from storybook_video.providers.xai import XAIImageGenerator, XAIStoryPlanner, XAIVideoGenerator

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 16
SETTINGS = ProjectSettings(provider="xai", text_model="grok-test")


def make(cls, handler):
    calls = []

    def wrapped(request: httpx.Request):
        calls.append(request)
        return handler(request)

    return cls("test-key", SETTINGS, "https://api.x.ai/v1", transport=httpx.MockTransport(wrapped)), calls


def video_request(tmp_path, audio_mode=AudioMode.voiceover, duration=6.0, suffix=".png"):
    img = tmp_path / f"s01{suffix}"
    img.write_bytes(PNG)
    return VideoRequest(
        scene_id="s01", scene_revision=2, image_path=img, prompt="鏡頭推近", duration_seconds=duration,
        aspect_ratio="16:9", audio_mode=audio_mode, resolution="720p", model="grok-imagine-video-1.5",
    )


# ---- 分鏡 ----


def test_plan_sends_strict_schema_and_parses():
    plan_json = {
        "title": "小燈籠",
        "style": "水彩",
        "characters": [{"name": "米米", "description": "白兔", "reference_paths": []}],
        "scenes": [
            {
                "display_text": "第一頁", "narration_or_dialogue": "旁白", "audio_mode": "voiceover",
                "image_prompt": "畫面", "motion_prompt": "推近", "duration_seconds": 20, "characters": ["米米"],
            }
        ],
    }

    def handler(req):
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(plan_json)}}]})

    planner, calls = make(XAIStoryPlanner, handler)
    plan = planner.plan(PlanRequest(story="故事", style="水彩", aspect_ratio="16:9", characters=[Character(name="米米")]))
    body = json.loads(calls[0].content)
    assert calls[0].url.path == "/v1/chat/completions"
    assert calls[0].headers["authorization"] == "Bearer test-key"
    assert body["model"] == "grok-test"
    rf = body["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is True
    assert '"default"' not in json.dumps(rf["json_schema"]["schema"])
    assert plan.scenes[0].duration_seconds == 15  # 超過上限被夾到 15


def test_plan_format_error_is_readable():
    def handler(req):
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"scenes": []}'}}]})

    planner, _ = make(XAIStoryPlanner, handler)
    with pytest.raises(PlanFormatError, match="scenes"):
        planner.plan(PlanRequest(story="x", style="", aspect_ratio="16:9", characters=[]))


def test_plan_requires_text_model():
    planner = XAIStoryPlanner("k", ProjectSettings(provider="xai"), "https://api.x.ai/v1")
    with pytest.raises(ProviderError, match="XAI_TEXT_MODEL"):
        planner.plan(PlanRequest(story="x", style="", aspect_ratio="16:9", characters=[]))


# ---- 圖片 ----


def test_image_generation_b64(tmp_path):
    def handler(req):
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG).decode(), "mime_type": "image/png"}]})

    gen, calls = make(XAIImageGenerator, handler)
    result = gen.generate(ImageRequest(scene_id="s01", prompt="兔子", aspect_ratio="16:9"))
    body = json.loads(calls[0].content)
    assert calls[0].url.path == "/v1/images/generations"
    assert body["response_format"] == "b64_json" and body["aspect_ratio"] == "16:9"
    assert result.data == PNG and result.suffix == ".png" and not result.is_mock


def test_image_with_references_uses_edits(tmp_path):
    ref = tmp_path / "mimi.png"
    ref.write_bytes(PNG)

    def handler(req):
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG).decode(), "mime_type": "image/jpeg"}]})

    gen, calls = make(XAIImageGenerator, handler)
    result = gen.generate(ImageRequest(scene_id="s01", prompt="兔子", aspect_ratio="16:9", reference_paths=[ref]))
    body = json.loads(calls[0].content)
    assert calls[0].url.path == "/v1/images/edits"
    assert body["images"][0]["url"].startswith("data:image/png;base64,")
    assert "<IMAGE_0>" in body["prompt"]
    assert result.suffix == ".jpg"


def test_image_http_error_is_provider_error():
    gen, _ = make(XAIImageGenerator, lambda req: httpx.Response(400, json={"error": "bad prompt"}))
    with pytest.raises(ProviderError, match="400"):
        gen.generate(ImageRequest(scene_id="s01", prompt="x", aspect_ratio="16:9"))


# ---- 影片 ----


def test_video_submit_body(tmp_path):
    gen, calls = make(XAIVideoGenerator, lambda req: httpx.Response(200, json={"request_id": "req-1"}))
    sub = gen.submit(video_request(tmp_path, audio_mode=AudioMode.none, duration=6.4))
    body = json.loads(calls[0].content)
    assert sub.job_id == "req-1" and not sub.is_mock
    assert calls[0].url.path == "/v1/videos/generations"
    assert body["image"]["url"].startswith("data:image/png;base64,")
    assert body["duration"] == 6 and body["resolution"] == "720p"
    assert body["generate_audio"] is False
    assert "aspect_ratio" not in body


def test_video_submit_with_audio_omits_generate_audio(tmp_path):
    gen, calls = make(XAIVideoGenerator, lambda req: httpx.Response(200, json={"request_id": "r"}))
    gen.submit(video_request(tmp_path))
    assert "generate_audio" not in json.loads(calls[0].content)


def test_video_rejects_svg_and_bad_duration_before_sending(tmp_path):
    gen, calls = make(XAIVideoGenerator, lambda req: httpx.Response(200, json={"request_id": "r"}))
    with pytest.raises(ProviderError, match="SVG"):
        gen.submit(video_request(tmp_path, suffix=".svg"))
    with pytest.raises(ProviderError, match="時長"):
        gen.submit(video_request(tmp_path, duration=20))
    assert calls == []


@pytest.mark.parametrize(
    "handler,expected",
    [
        (lambda req: (_ for _ in ()).throw(httpx.ReadTimeout("t", request=req)), SubmissionUncertainError),
        (lambda req: (_ for _ in ()).throw(httpx.RemoteProtocolError("x", request=req)), SubmissionUncertainError),
        (lambda req: httpx.Response(502, text="bad gateway"), SubmissionUncertainError),
        (lambda req: httpx.Response(200, json={}), SubmissionUncertainError),
        (lambda req: (_ for _ in ()).throw(httpx.ConnectError("no route", request=req)), ProviderError),
        (lambda req: httpx.Response(429, json={"error": "rate limited"}), ProviderError),
    ],
)
def test_video_submit_error_classification(tmp_path, handler, expected):
    gen, _ = make(XAIVideoGenerator, handler)
    with pytest.raises(expected) as exc:
        gen.submit(video_request(tmp_path))
    if expected is ProviderError:
        assert not isinstance(exc.value, SubmissionUncertainError)


@pytest.mark.parametrize(
    "payload,status,error_part",
    [
        ({"status": "pending", "progress": 40}, JobStatus.pending, None),
        ({"status": "done", "video": {"url": "https://v/x.mp4", "respect_moderation": True},
          "usage": {"cost_in_usd_ticks": 4_800_000_000}}, JobStatus.succeeded, None),
        ({"status": "done", "video": {"url": None, "respect_moderation": False}}, JobStatus.failed, "審核"),
        ({"status": "failed", "error": {"code": "invalid_argument", "message": "bad"}}, JobStatus.failed, "invalid_argument"),
        ({"status": "expired"}, JobStatus.failed, "expired"),
    ],
)
def test_video_poll_mapping(payload, status, error_part):
    gen, calls = make(XAIVideoGenerator, lambda req: httpx.Response(200, json=payload))
    state = gen.poll("req-1")
    assert calls[0].url.path == "/v1/videos/req-1"
    assert state.status == status
    if error_part:
        assert error_part in state.error
    if status == JobStatus.succeeded:
        assert state.cost_usd == pytest.approx(0.48)


def test_video_fetch_downloads_mp4_without_api_key():
    def handler(req):
        if req.url.host == "api.x.ai":
            return httpx.Response(200, json={"status": "done", "video": {"url": "https://vidgen.x.ai/v.mp4"}})
        assert "authorization" not in req.headers
        return httpx.Response(200, content=MP4)

    gen, calls = make(XAIVideoGenerator, handler)
    result = gen.fetch_result("req-1")
    assert result.data == MP4 and result.suffix == ".mp4" and not result.is_mock
    assert [c.url.host for c in calls] == ["api.x.ai", "vidgen.x.ai"]


def test_video_fetch_rejects_non_mp4():
    def handler(req):
        if req.url.host == "api.x.ai":
            return httpx.Response(200, json={"status": "done", "video": {"url": "https://vidgen.x.ai/v.mp4"}})
        return httpx.Response(200, content=b"<html>error</html>")

    gen, _ = make(XAIVideoGenerator, handler)
    with pytest.raises(ProviderError, match="MP4"):
        gen.fetch_result("req-1")
