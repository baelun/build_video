"""環境設定：API key 與全域路徑由環境變數／.env 提供，不寫入專案檔。"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from .models import ProjectSettings


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    xai_api_key: SecretStr | None = None
    xai_base_url: str = "https://api.x.ai/v1"
    projects_dir: Path = Field(default=Path("projects"), alias="STORYBOOK_PROJECTS_DIR")

    # 選填：覆寫專案中的模型設定
    xai_text_model: str | None = None
    xai_image_model: str | None = None
    xai_video_model: str | None = None
    xai_video_resolution: str | None = None

    def effective_project_settings(self, ps: ProjectSettings) -> ProjectSettings:
        """環境變數優先於 project.json 中的模型設定（不寫回檔案）。"""
        overrides = {
            "text_model": self.xai_text_model,
            "image_model": self.xai_image_model,
            "video_model": self.xai_video_model,
            "video_resolution": self.xai_video_resolution,
        }
        return ps.model_copy(update={k: v for k, v in overrides.items() if v})


def load_settings() -> Settings:
    return Settings()
