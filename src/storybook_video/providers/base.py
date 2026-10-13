"""供應商介面：文字分鏡、圖片生成、影片生成三種能力分開定義，流程不依賴特定 SDK。"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from ..models import AudioMode, Character, JobStatus, StoryPlan


class ProviderError(Exception):
    """供應商明確回報失敗（例如參數錯誤），可確定請求未被受理。"""


class SubmissionUncertainError(ProviderError):
    """請求可能已送達但沒收到確定回應（逾時、連線中斷）。不可自動重送。"""


class CapabilityNotImplementedError(ProviderError):
    """此供應商尚未實作該能力。不會退回 mock。"""


class PlanFormatError(ProviderError):
    """LLM 回傳的分鏡格式不符。"""


# ---- 分鏡 ----


@dataclass
class PlanRequest:
    story: str
    style: str
    aspect_ratio: str
    characters: list[Character]
    language: str = "zh-TW"
    default_duration_seconds: float = 6
    brief: str = ""  # 創作說明（情緒、視覺、分鏡骨架）


class StoryPlanner(ABC):
    name: str

    @abstractmethod
    def plan(self, request: PlanRequest) -> StoryPlan: ...


def parse_story_plan(raw: str | dict) -> StoryPlan:
    """驗證 LLM 的結構化輸出；格式不符時給出可讀的錯誤。"""
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError as e:
        raise PlanFormatError(f"分鏡輸出不是合法 JSON（第 {e.lineno} 行第 {e.colno} 欄）：{e.msg}") from e
    try:
        return StoryPlan.model_validate(data)
    except ValidationError as e:
        lines = []
        for err in e.errors():
            loc = ".".join(str(x) for x in err["loc"]) or "(根)"
            lines.append(f"  - {loc}: {err['msg']}")
        raise PlanFormatError("分鏡輸出格式不符：\n" + "\n".join(lines)) from e


# ---- 圖片 ----


@dataclass
class ImageRequest:
    scene_id: str
    prompt: str
    aspect_ratio: str
    reference_paths: list[Path] = field(default_factory=list)
    reference_labels: list[str] = field(default_factory=list)  # 與 reference_paths 對應的說明
    caption: str = ""  # 僅供 mock 占位圖顯示


@dataclass
class ImageResult:
    data: bytes
    suffix: str  # 例如 ".png"、".svg"
    provider: str
    model: str | None = None
    is_mock: bool = False


class ImageGenerator(ABC):
    name: str

    @abstractmethod
    def generate(self, request: ImageRequest) -> ImageResult: ...


# ---- 影片 ----


@dataclass
class VideoRequest:
    scene_id: str
    scene_revision: int
    image_path: Path
    prompt: str
    duration_seconds: float
    aspect_ratio: str
    audio_mode: AudioMode
    resolution: str
    model: str | None = None


@dataclass
class VideoSubmission:
    job_id: str
    provider: str
    model: str | None = None
    is_mock: bool = False


@dataclass
class VideoJobState:
    status: JobStatus  # pending / succeeded / failed
    error: str | None = None
    cost_usd: float | None = None  # 供應商回報的費用（若有）
    progress: int | None = None  # 0-100（若有）


@dataclass
class VideoResult:
    data: bytes
    suffix: str  # ".mp4"；mock 為 ".mock.json"
    is_mock: bool = False


class VideoGenerator(ABC):
    """影片生成分三步：提交、查詢、取得結果。"""

    name: str

    @abstractmethod
    def submit(self, request: VideoRequest) -> VideoSubmission: ...

    @abstractmethod
    def poll(self, job_id: str) -> VideoJobState: ...

    @abstractmethod
    def fetch_result(self, job_id: str) -> VideoResult: ...
