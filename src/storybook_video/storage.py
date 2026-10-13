"""專案檔案管理：原子寫入、版本化素材路徑、生成紀錄。

專案資料夾結構（皆以相對路徑記錄於 project.json）：

    projects/<project_id>/
      project.json                 製作資料
      story.txt                    原始故事副本
      assets/images/<scene_id>/v001.png
      assets/videos/<scene_id>/v001.mp4
      preview/index.html           分鏡預覽頁
      output/story_v001.mp4        完整合成影片
      history/project_<時間>.json  覆寫分鏡前的備份
      logs/generation.jsonl        每次生成／提交／查詢紀錄（只追加）
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from .models import Project, utcnow

PROJECT_FILE = "project.json"
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class StorageError(Exception):
    pass


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)  # mkstemp 預設 0600，改成一般檔案權限
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def validate_project_id(project_id: str) -> str:
    if not _ID_RE.match(project_id):
        raise StorageError(
            f"project_id 不合法：{project_id!r}（只允許英數、- 與 _，最長 64 字，需英數開頭）"
        )
    return project_id


class ProjectStore:
    def __init__(self, projects_dir: Path):
        self.projects_dir = Path(projects_dir)

    def project_dir(self, project_id: str) -> Path:
        return self.projects_dir / validate_project_id(project_id)

    def exists(self, project_id: str) -> bool:
        return (self.project_dir(project_id) / PROJECT_FILE).exists()

    def create(self, project: Project) -> Path:
        d = self.project_dir(project.project_id)
        if (d / PROJECT_FILE).exists():
            raise StorageError(f"專案已存在：{d}")
        for sub in ("assets/images", "assets/videos", "preview", "output", "logs", "history"):
            (d / sub).mkdir(parents=True, exist_ok=True)
        atomic_write_text(d / "story.txt", project.source_story)
        self.save(project)
        return d

    def load(self, project_id: str) -> Project:
        path = self.project_dir(project_id) / PROJECT_FILE
        if not path.exists():
            raise StorageError(f"找不到專案：{path}（請先執行 init）")
        try:
            project = Project.model_validate_json(path.read_text(encoding="utf-8"))
        except ValueError as e:
            raise StorageError(f"project.json 格式錯誤：{path}\n{e}") from e
        # 偵測手動編輯：內容變了就升 revision，使確認與舊影片失效
        changed = [s.scene_id for s in project.scenes if s.sync_revision()]
        if changed:
            self.log(project.project_id, "manual_edit_detected", scenes=changed)
            self.save(project)
        return project

    def save(self, project: Project) -> None:
        for s in project.scenes:
            s.sync_revision()
        project.updated_at = utcnow()
        path = self.project_dir(project.project_id) / PROJECT_FILE
        atomic_write_text(path, project.model_dump_json(indent=2) + "\n")

    def backup(self, project: Project) -> Path:
        """覆寫分鏡前保存整份 project.json。"""
        stamp = utcnow().strftime("%Y%m%dT%H%M%S%f")
        dest = self.project_dir(project.project_id) / "history" / f"project_{stamp}.json"
        atomic_write_text(dest, project.model_dump_json(indent=2) + "\n")
        return dest

    # ---- 素材 ----

    def next_version_path(self, project_id: str, kind: str, scene_id: str, suffix: str) -> str:
        """回傳下一個未使用的版本路徑（相對路徑），絕不覆蓋舊檔。"""
        base = self.project_dir(project_id)
        folder = Path("assets") / kind / scene_id
        (base / folder).mkdir(parents=True, exist_ok=True)
        existing = [
            int(m.group(1))
            for p in (base / folder).iterdir()
            if (m := re.match(r"^v(\d+)\.", p.name))
        ]
        n = max(existing, default=0) + 1
        return (folder / f"v{n:03d}{suffix}").as_posix()

    def write_asset(self, project_id: str, rel_path: str, data: bytes) -> Path:
        dest = self.resolve(project_id, rel_path)
        if dest.exists():
            raise StorageError(f"素材已存在，拒絕覆蓋：{dest}")
        atomic_write_bytes(dest, data)
        return dest

    def next_output_path(self, project_id: str) -> str:
        out = self.project_dir(project_id) / "output"
        out.mkdir(parents=True, exist_ok=True)
        existing = [
            int(m.group(1)) for p in out.iterdir() if (m := re.match(r"^story_v(\d+)\.mp4$", p.name))
        ]
        return f"output/story_v{max(existing, default=0) + 1:03d}.mp4"

    def resolve(self, project_id: str, rel_path: str) -> Path:
        base = self.project_dir(project_id).resolve()
        p = (base / rel_path).resolve()
        if not p.is_relative_to(base):
            raise StorageError(f"路徑超出專案資料夾：{rel_path}")
        return p

    # ---- 紀錄 ----

    def log(self, project_id: str, event: str, **data: Any) -> None:
        path = self.project_dir(project_id) / "logs" / "generation.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"time": utcnow().isoformat(), "event": event, **data}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            f.flush()
            os.fsync(f.fileno())
