"""依專案設定建立供應商。專案設為 xai 時絕不偷偷退回 mock。"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Settings
from ..models import Project
from .base import ImageGenerator, StoryPlanner, VideoGenerator
from .mock import MockImageGenerator, MockStoryPlanner, MockVideoGenerator


@dataclass
class Providers:
    planner: StoryPlanner
    images: ImageGenerator
    videos: VideoGenerator


def build_providers(project: Project, settings: Settings) -> Providers:
    if project.settings.provider == "mock":
        return Providers(MockStoryPlanner(), MockImageGenerator(), MockVideoGenerator())
    if project.settings.provider == "xai":
        from .xai import XAIImageGenerator, XAIStoryPlanner, XAIVideoGenerator

        key = settings.xai_api_key.get_secret_value() if settings.xai_api_key else None
        ps = settings.effective_project_settings(project.settings)
        args = (key, ps, settings.xai_base_url)
        return Providers(XAIStoryPlanner(*args), XAIImageGenerator(*args), XAIVideoGenerator(*args))
    raise ValueError(f"未知的 provider：{project.settings.provider}")
