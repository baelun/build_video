"""離線 mock 供應商：不需 API key、不連網、不產生假 MP4。

所有輸出都清楚標示 MOCK：
- 分鏡：依段落機械切分，不是 LLM 結果。
- 圖片：SVG 占位圖，畫面上印有「MOCK」字樣。
- 影片：只模擬任務狀態，結果是一份 .mock.json 說明檔，不含任何影像或聲音。
"""

from __future__ import annotations

import json
import re
import uuid
from html import escape

from ..models import AudioMode, JobStatus, PlannedScene, StoryPlan
from .base import (
    ImageGenerator,
    ImageRequest,
    ImageResult,
    PlanRequest,
    ProviderError,
    StoryPlanner,
    VideoGenerator,
    VideoJobState,
    VideoRequest,
    VideoResult,
    VideoSubmission,
)

MOCK_JOB_PREFIX = "mock-"


class MockStoryPlanner(StoryPlanner):
    name = "mock"

    def plan(self, request: PlanRequest) -> StoryPlan:
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", request.story.strip()) if p.strip()]
        title = ""
        if paragraphs and paragraphs[0].startswith("#"):
            title = paragraphs.pop(0).lstrip("#").strip()
        names = [c.name for c in request.characters]
        scenes = []
        for i, para in enumerate(paragraphs, start=1):
            text = " ".join(line.strip() for line in para.splitlines())
            quotes = re.findall(r"「([^」]+)」", text)
            present = [n for n in names if n in text]
            if quotes and present:
                mode, speech = AudioMode.dialogue, "".join(f"「{q}」" for q in quotes)
            else:
                mode, speech = AudioMode.voiceover, text
            scenes.append(
                PlannedScene(
                    display_text=text,
                    narration_or_dialogue=speech,
                    audio_mode=mode,
                    image_prompt=f"[MOCK] 第 {i} 幕插畫：{text[:60]}",
                    motion_prompt="[MOCK] 鏡頭緩慢推近，角色輕微動作",
                    duration_seconds=request.default_duration_seconds,
                    characters=present,
                )
            )
        if not scenes:
            raise ProviderError("故事內容為空，無法分鏡")
        return StoryPlan(title=title, style=request.style, characters=request.characters, scenes=scenes)


_PALETTE = ["#f6d6ad", "#c9e4de", "#c6def1", "#dbcdf0", "#f2c6de", "#faedcb"]


class MockImageGenerator(ImageGenerator):
    name = "mock"

    def generate(self, request: ImageRequest) -> ImageResult:
        w, h = _canvas(request.aspect_ratio)
        color = _PALETTE[sum(map(ord, request.scene_id)) % len(_PALETTE)]
        caption = escape(request.caption[:40])
        prompt = escape(request.prompt[:70])
        svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">
  <rect width="100%" height="100%" fill="{color}"/>
  <rect x="16" y="16" width="{w - 32}" height="{h - 32}" fill="none" stroke="#555" stroke-dasharray="12 8" stroke-width="3"/>
  <text x="50%" y="38%" text-anchor="middle" font-size="{h // 8}" font-family="sans-serif" fill="#333" font-weight="bold">MOCK 占位圖</text>
  <text x="50%" y="52%" text-anchor="middle" font-size="{h // 22}" font-family="sans-serif" fill="#333">{escape(request.scene_id)} · {caption}</text>
  <text x="50%" y="62%" text-anchor="middle" font-size="{h // 30}" font-family="sans-serif" fill="#555">{prompt}</text>
  <text x="50%" y="90%" text-anchor="middle" font-size="{h // 30}" font-family="sans-serif" fill="#a00">非真實生成圖片</text>
</svg>
"""
        return ImageResult(data=svg.encode("utf-8"), suffix=".svg", provider=self.name, is_mock=True)


def _canvas(aspect_ratio: str) -> tuple[int, int]:
    try:
        a, b = (int(x) for x in aspect_ratio.split(":"))
    except ValueError:
        a, b = 16, 9
    return (960, round(960 * b / a)) if a >= b else (round(960 * a / b), 960)


class MockVideoGenerator(VideoGenerator):
    """模擬任務生命週期：submit → pending；poll → succeeded；fetch → .mock.json 說明檔。"""

    name = "mock"

    def submit(self, request: VideoRequest) -> VideoSubmission:
        job_id = f"{MOCK_JOB_PREFIX}{request.scene_id}-r{request.scene_revision}-{uuid.uuid4().hex[:8]}"
        return VideoSubmission(job_id=job_id, provider=self.name, model="mock-video", is_mock=True)

    def poll(self, job_id: str) -> VideoJobState:
        if not job_id.startswith(MOCK_JOB_PREFIX):
            return VideoJobState(status=JobStatus.failed, error=f"不是 mock 任務：{job_id}")
        return VideoJobState(status=JobStatus.succeeded)

    def fetch_result(self, job_id: str) -> VideoResult:
        doc = {
            "mock": True,
            "job_id": job_id,
            "notice": "這是 mock 模擬結果，沒有生成任何真實影片或中文旁白，無法用於合成。",
        }
        data = json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8")
        return VideoResult(data=data, suffix=".mock.json", is_mock=True)
