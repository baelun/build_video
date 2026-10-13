"""製作專案與分鏡的資料模型。

重點規則：
- scene.revision 代表「內容版本」。圖片、台詞、提示詞、角色參考、時長等內容一變，revision 就加一。
- 確認（approval）與影片都綁定某個 revision；revision 不一致即視為失效／過期。
- 手動編輯 project.json 也會被偵測：載入時比對 content_fingerprint，不同就自動升 revision。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

SCHEMA_VERSION = 1


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AudioMode(StrEnum):
    voiceover = "voiceover"  # 畫外旁白
    dialogue = "dialogue"  # 人物台詞／對嘴
    none = "none"


class JobStatus(StrEnum):
    submitting = "submitting"  # 已開始送出但尚未得到回應；程式中斷後遇到視為 unknown
    pending = "pending"  # 已提交，供應商處理中
    succeeded = "succeeded"
    failed = "failed"
    unknown = "unknown"  # 提交結果不明，不可自動重送（避免重複計費）


class Approval(BaseModel):
    approved: bool = False
    revision: int | None = None
    approved_at: datetime | None = None


class GenerationJob(BaseModel):
    job_id: str | None = None
    provider: str
    model: str | None = None
    status: JobStatus
    scene_revision: int  # 提交時 scene 的 revision
    submitted_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    error: str | None = None
    is_mock: bool = False


class AssetVersion(BaseModel):
    """一個不會被覆蓋的素材版本（圖片或影片）。"""

    path: str  # 相對於專案資料夾
    scene_revision: int  # 對應的 scene 內容版本
    created_at: datetime = Field(default_factory=utcnow)
    provider: str
    model: str | None = None
    job_id: str | None = None
    is_mock: bool = False


# 影響「確認內容」的欄位；改動任一欄位都會讓該幕 revision 加一並使確認失效
CONTENT_FIELDS = (
    "display_text",
    "narration_or_dialogue",
    "audio_mode",
    "image_prompt",
    "motion_prompt",
    "duration_seconds",
    "character_reference_paths",
    "image_path",
)


class Scene(BaseModel):
    scene_id: str
    order: int
    display_text: str
    narration_or_dialogue: str = ""
    audio_mode: AudioMode = AudioMode.voiceover
    image_prompt: str
    motion_prompt: str = ""
    duration_seconds: float = Field(default=6, gt=0, le=60)
    character_reference_paths: list[str] = Field(default_factory=list)
    image_path: str | None = None
    video_path: str | None = None
    video_revision: int | None = None  # video_path 是用哪個 revision 生成的
    revision: int = 1
    content_fingerprint: str | None = None
    approval: Approval = Field(default_factory=Approval)
    generation_job: GenerationJob | None = None
    job_history: list[GenerationJob] = Field(default_factory=list)  # 被取代的舊任務
    image_history: list[AssetVersion] = Field(default_factory=list)
    video_history: list[AssetVersion] = Field(default_factory=list)

    def compute_fingerprint(self) -> str:
        data = self.model_dump(mode="json", include=set(CONTENT_FIELDS))
        raw = json.dumps(data, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def sync_revision(self) -> bool:
        """內容與上次記錄的指紋不同時升 revision。回傳是否有變更。"""
        fp = self.compute_fingerprint()
        if self.content_fingerprint is None:
            self.content_fingerprint = fp
            return False
        if fp != self.content_fingerprint:
            self.revision += 1
            self.content_fingerprint = fp
            return True
        return False

    def apply_changes(self, **changes: object) -> bool:
        """套用內容修改；有實際變更時升 revision（因此確認自動失效）。"""
        for key in changes:
            if key not in CONTENT_FIELDS:
                raise ValueError(f"不可透過內容修改變更欄位：{key}")
        updated = self.model_validate({**self.model_dump(), **changes})
        for key in changes:
            setattr(self, key, getattr(updated, key))
        return self.sync_revision()

    @property
    def is_approved(self) -> bool:
        return self.approval.approved and self.approval.revision == self.revision

    @property
    def has_current_video(self) -> bool:
        return self.video_path is not None and self.video_revision == self.revision

    @property
    def current_video_is_mock(self) -> bool:
        if not self.has_current_video:
            return False
        for v in reversed(self.video_history):
            if v.path == self.video_path:
                return v.is_mock
        return False

    @property
    def active_job(self) -> GenerationJob | None:
        """針對當前 revision、尚未結束的任務。"""
        job = self.generation_job
        if job and job.scene_revision == self.revision and job.status in (
            JobStatus.submitting,
            JobStatus.pending,
            JobStatus.unknown,
        ):
            return job
        return None


class Character(BaseModel):
    name: str
    description: str = ""
    voice: str = ""  # 說話聲音特色，用於 dialogue 幕的影片提示
    reference_paths: list[str] = Field(default_factory=list)


class ProjectSettings(BaseModel):
    provider: Literal["mock", "xai"] = "mock"
    text_model: str | None = None
    image_model: str = "grok-imagine-image-2.0"
    video_model: str = "grok-imagine-video-1.5"
    video_resolution: str = "720p"
    default_duration_seconds: float = 6
    language: str = "zh-TW"
    # 旁白語氣：寫進影片提示，讓內建聲音生成有說故事的感覺
    narration_style: str = (
        "一位溫暖的女性說書人，像睡前講童話給小朋友聽，親切生動、帶著笑意與好奇，"
        "語調起伏明顯，節奏放慢，關鍵詞稍微加重"
    )


class Project(BaseModel):
    schema_version: int = SCHEMA_VERSION
    project_id: str
    title: str = ""
    source_story: str
    characters: list[Character] = Field(default_factory=list)
    style: str = ""
    aspect_ratio: str = "16:9"
    settings: ProjectSettings = Field(default_factory=ProjectSettings)
    scenes: list[Scene] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def ordered_scenes(self) -> list[Scene]:
        return sorted(self.scenes, key=lambda s: s.order)

    def get_scene(self, scene_id: str) -> Scene:
        for s in self.scenes:
            if s.scene_id == scene_id:
                return s
        known = ", ".join(s.scene_id for s in self.ordered_scenes()) or "（尚無分鏡）"
        raise KeyError(f"找不到 scene_id={scene_id}；現有：{known}")

    def select_scenes(self, scene_ids: list[str] | None) -> list[Scene]:
        if not scene_ids:
            return self.ordered_scenes()
        return [self.get_scene(sid) for sid in scene_ids]


# ---- LLM 分鏡的結構化輸出格式 ----


class PlannedScene(BaseModel):
    display_text: str = Field(min_length=1)
    narration_or_dialogue: str = ""
    audio_mode: AudioMode = AudioMode.voiceover
    image_prompt: str = Field(min_length=1)
    motion_prompt: str = ""
    duration_seconds: float = Field(default=6, gt=0, le=60)
    characters: list[str] = Field(default_factory=list, description="出場角色名稱")


class StoryPlan(BaseModel):
    title: str = ""
    style: str = ""
    characters: list[Character] = Field(default_factory=list)
    scenes: list[PlannedScene] = Field(min_length=1)
