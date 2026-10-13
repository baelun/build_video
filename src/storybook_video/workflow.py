"""製作流程：分鏡、圖片、人工確認、影片任務。

每個會產生費用或素材的步驟都先檢查狀態，並在每一幕處理完後立即存檔，
中途中斷也不會遺失已提交的任務資訊。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import (
    Approval,
    AssetVersion,
    AudioMode,
    Character,
    GenerationJob,
    JobStatus,
    Project,
    ProjectSettings,
    Scene,
    utcnow,
)
from .providers.base import (
    CapabilityNotImplementedError,
    ImageGenerator,
    ImageRequest,
    PlanRequest,
    ProviderError,
    StoryPlanner,
    SubmissionUncertainError,
    VideoGenerator,
    VideoRequest,
)
from .storage import ProjectStore


class WorkflowError(Exception):
    pass


@dataclass
class Outcome:
    scene_id: str
    action: str  # generated / submitted / polled / skipped / error / would_* / approved ...
    message: str = ""
    details: dict = field(default_factory=dict)


# ---- 建立專案與分鏡 ----


def init_project(
    store: ProjectStore,
    project_id: str,
    story: str,
    *,
    style: str = "",
    aspect_ratio: str = "16:9",
    provider: str = "mock",
    characters: list[Character] | None = None,
) -> Project:
    if not re.fullmatch(r"\d+:\d+", aspect_ratio):
        raise WorkflowError(f"畫面比例格式應為 W:H，例如 16:9；收到 {aspect_ratio!r}")
    if not story.strip():
        raise WorkflowError("故事內容為空")
    project = Project(
        project_id=project_id,
        source_story=story,
        style=style,
        aspect_ratio=aspect_ratio,
        characters=characters or [],
        settings=ProjectSettings(provider=provider),
    )
    store.create(project)
    store.log(project_id, "init", provider=provider)
    return project


def story_heading(story: str) -> str:
    """故事第一個非空行若是「# 標題」，回傳標題。"""
    for line in story.splitlines():
        if line.strip():
            return line.lstrip("#").strip() if line.lstrip().startswith("#") else ""
    return ""


def _next_scene_numbers(project: Project, count: int) -> list[str]:
    used = [int(m.group(1)) for s in project.scenes if (m := re.fullmatch(r"s(\d+)", s.scene_id))]
    start = max(used, default=0) + 1
    return [f"s{n:02d}" for n in range(start, start + count)]


def plan_story(
    store: ProjectStore, project: Project, planner: StoryPlanner, *, force: bool = False
) -> list[Scene]:
    if project.scenes and not force:
        raise WorkflowError(
            f"專案已有 {len(project.scenes)} 幕分鏡；重新分鏡會取代現有分鏡，請加 --force（舊版會備份到 history/）"
        )
    request = PlanRequest(
        story=project.source_story,
        style=project.style,
        aspect_ratio=project.aspect_ratio,
        characters=project.characters,
        language=project.settings.language,
        default_duration_seconds=project.settings.default_duration_seconds,
    )
    plan = planner.plan(request)

    if project.scenes:
        backup = store.backup(project)
        store.log(project.project_id, "plan_backup", path=str(backup))

    chars = {c.name: c for c in (project.characters or plan.characters)}
    # 重新分鏡時沿用編號往後排，scene_id 永不重複使用
    ids = _next_scene_numbers(project, len(plan.scenes))
    scenes = []
    for order, (sid, ps) in enumerate(zip(ids, plan.scenes), start=1):
        refs = [p for name in ps.characters if name in chars for p in chars[name].reference_paths]
        scene = Scene(
            scene_id=sid,
            order=order,
            display_text=ps.display_text,
            narration_or_dialogue=ps.narration_or_dialogue,
            audio_mode=ps.audio_mode,
            image_prompt=ps.image_prompt,
            motion_prompt=ps.motion_prompt,
            duration_seconds=ps.duration_seconds,
            character_reference_paths=refs,
        )
        scene.sync_revision()
        scenes.append(scene)

    project.scenes = scenes
    if not project.title:
        project.title = plan.title or story_heading(project.source_story)
    if not project.characters and plan.characters:
        project.characters = plan.characters
    if not project.style and plan.style:
        project.style = plan.style
    store.save(project)
    store.log(project.project_id, "plan", provider=planner.name, scenes=[s.scene_id for s in scenes])
    return scenes


# ---- 圖片 ----


def build_image_prompt(project: Project, scene: Scene) -> str:
    parts = []
    if project.style:
        parts.append(f"畫風：{project.style}")
    present = [c for c in project.characters if c.name in scene.display_text + scene.image_prompt]
    if present:
        parts.append("角色：" + "；".join(f"{c.name}（{c.description}）" for c in present))
    parts.append(scene.image_prompt)
    return "\n".join(parts)


def generate_images(
    store: ProjectStore,
    project: Project,
    generator: ImageGenerator,
    *,
    scene_ids: list[str] | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> list[Outcome]:
    if not project.scenes:
        raise WorkflowError("尚無分鏡，請先執行 plan")
    outcomes = []
    for scene in project.select_scenes(scene_ids):
        if scene.image_path and not force:
            outcomes.append(Outcome(scene.scene_id, "skipped", "已有圖片（重做請加 --force）"))
            continue
        prompt = build_image_prompt(project, scene)
        if dry_run:
            outcomes.append(
                Outcome(
                    scene.scene_id,
                    "would_generate",
                    f"provider={generator.name} 比例={project.aspect_ratio}",
                    {"prompt": prompt, "references": scene.character_reference_paths},
                )
            )
            continue
        request = ImageRequest(
            scene_id=scene.scene_id,
            prompt=prompt,
            aspect_ratio=project.aspect_ratio,
            reference_paths=[store.resolve(project.project_id, p) for p in scene.character_reference_paths],
            caption=scene.display_text,
        )
        try:
            result = generator.generate(request)
        except CapabilityNotImplementedError:
            raise
        except ProviderError as e:
            store.log(project.project_id, "image_error", scene=scene.scene_id, error=str(e))
            outcomes.append(Outcome(scene.scene_id, "error", str(e)))
            continue
        rel = store.next_version_path(project.project_id, "images", scene.scene_id, result.suffix)
        store.write_asset(project.project_id, rel, result.data)
        scene.apply_changes(image_path=rel)  # 新圖片 → revision+1，確認失效
        scene.image_history.append(
            AssetVersion(
                path=rel,
                scene_revision=scene.revision,
                provider=result.provider,
                model=result.model,
                is_mock=result.is_mock,
            )
        )
        store.save(project)
        store.log(project.project_id, "image", scene=scene.scene_id, path=rel, mock=result.is_mock)
        label = "（MOCK 占位圖）" if result.is_mock else ""
        outcomes.append(Outcome(scene.scene_id, "generated", f"{rel}{label}，revision={scene.revision}"))
    return outcomes


# ---- 修改與確認 ----


def edit_scene(store: ProjectStore, project: Project, scene_id: str, **changes: object) -> bool:
    scene = project.get_scene(scene_id)
    changes = {k: v for k, v in changes.items() if v is not None}
    if not changes:
        raise WorkflowError("沒有指定要修改的欄位")
    was_approved = scene.is_approved
    changed = scene.apply_changes(**changes)
    if changed:
        store.save(project)
        store.log(
            project.project_id,
            "edit",
            scene=scene_id,
            fields=sorted(changes),
            revision=scene.revision,
            approval_invalidated=was_approved,
        )
    return changed


def approve_scenes(
    store: ProjectStore,
    project: Project,
    *,
    scene_ids: list[str] | None = None,
    approve_all: bool = False,
) -> list[Outcome]:
    if not scene_ids and not approve_all:
        raise WorkflowError("請指定 --scene，或明確使用 --all 確認全部")
    outcomes = []
    for scene in project.select_scenes(None if approve_all else scene_ids):
        if scene.is_approved:
            outcomes.append(Outcome(scene.scene_id, "skipped", f"revision {scene.revision} 已確認"))
            continue
        if not scene.image_path or not store.resolve(project.project_id, scene.image_path).exists():
            outcomes.append(Outcome(scene.scene_id, "error", "尚無圖片，無法確認（請先執行 images）"))
            continue
        scene.approval = Approval(approved=True, revision=scene.revision, approved_at=utcnow())
        outcomes.append(Outcome(scene.scene_id, "approved", f"已確認 revision {scene.revision}"))
    store.save(project)
    store.log(
        project.project_id,
        "approve",
        scenes={o.scene_id: o.action for o in outcomes},
    )
    return outcomes


def revoke_approval(store: ProjectStore, project: Project, scene_ids: list[str]) -> None:
    for scene in project.select_scenes(scene_ids):
        scene.approval = Approval()
    store.save(project)
    store.log(project.project_id, "revoke", scenes=scene_ids)


# ---- 影片 ----


LANGUAGE_NAMES = {"zh-TW": "台灣華語", "zh-CN": "普通話", "en": "English", "ja": "日本語"}


def build_video_prompt(project: Project, scene: Scene) -> str:
    lines = [scene.motion_prompt or "鏡頭緩慢推近"]
    speech = scene.narration_or_dialogue.strip()
    lang = LANGUAGE_NAMES.get(project.settings.language, project.settings.language)
    style = project.settings.narration_style
    if scene.audio_mode == AudioMode.voiceover and speech:
        lines.append(f"畫外旁白（{style}），用{lang}逐字念出：「{speech}」")
    elif scene.audio_mode == AudioMode.dialogue and speech:
        voices = [f"{c.name}：{c.voice}" for c in project.characters if c.voice and c.name in speech]
        if voices:
            lines.append("角色聲音：" + "；".join(voices))
        lines.append(f"角色開口說話並對嘴，像童話動畫一樣生動有感情，用{lang}逐字說出以下台詞：\n{speech}")
    else:
        lines.append("無人聲，只有輕柔環境音")
    if project.style:
        lines.append(f"畫風維持：{project.style}")
    return "\n".join(lines)


def _recover_interrupted(scene: Scene) -> bool:
    """上次在送出途中中斷（狀態停在 submitting）→ 改記為 unknown，不自動重送。"""
    job = scene.generation_job
    if job and job.status == JobStatus.submitting:
        job.status = JobStatus.unknown
        job.error = "上次提交途中中斷，無法確認供應商是否已受理"
        job.updated_at = utcnow()
        return True
    return False


def submit_videos(
    store: ProjectStore,
    project: Project,
    generator: VideoGenerator,
    *,
    scene_ids: list[str] | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> list[Outcome]:
    if not project.scenes:
        raise WorkflowError("尚無分鏡，請先執行 plan")
    # 供應商帶有已套用環境變數覆寫的設定時以它為準（mock 沒有，用專案設定）
    ps = getattr(generator, "settings", None) or project.settings
    outcomes = []
    for scene in project.select_scenes(scene_ids):
        sid = scene.scene_id
        if _recover_interrupted(scene) and not dry_run:
            store.save(project)

        if not scene.is_approved:
            outcomes.append(
                Outcome(sid, "skipped", f"revision {scene.revision} 尚未確認，不提交（先 review 再 approve）")
            )
            continue

        active = scene.active_job
        if active and active.status == JobStatus.pending:
            # 規則：已有 pending 任務 → 先查詢，絕不重複提交（--force 也不例外）
            if dry_run:
                outcomes.append(Outcome(sid, "would_poll", f"已有任務 {active.job_id}，只會查詢狀態"))
            else:
                outcomes.extend(poll_videos(store, project, generator, scene_ids=[sid]))
            continue
        if active and active.status == JobStatus.unknown and not force:
            outcomes.append(
                Outcome(
                    sid,
                    "skipped",
                    "上次提交結果不明，為避免重複計費不自動重送。請先到供應商後台確認，"
                    "確定沒有在跑的任務後再用 --force 重新提交",
                )
            )
            continue
        if scene.has_current_video and not force:
            outcomes.append(Outcome(sid, "skipped", f"已有 revision {scene.revision} 的影片（重做請加 --force）"))
            continue

        prompt = build_video_prompt(project, scene)
        if dry_run:
            outcomes.append(
                Outcome(
                    sid,
                    "would_submit",
                    f"provider={generator.name} model={ps.video_model} 時長={scene.duration_seconds}s "
                    f"解析度={ps.video_resolution} 比例={project.aspect_ratio} 聲音={scene.audio_mode}",
                    {"prompt": prompt, "image": scene.image_path},
                )
            )
            continue

        request = VideoRequest(
            scene_id=sid,
            scene_revision=scene.revision,
            image_path=store.resolve(project.project_id, scene.image_path),
            prompt=prompt,
            duration_seconds=scene.duration_seconds,
            aspect_ratio=project.aspect_ratio,
            audio_mode=scene.audio_mode,
            resolution=ps.video_resolution,
            model=ps.video_model,
        )
        previous = scene.generation_job
        # 先把「送出中」落盤，送出途中當掉也能知道狀態不明
        job = GenerationJob(
            provider=generator.name, model=ps.video_model, status=JobStatus.submitting,
            scene_revision=scene.revision,
        )
        if previous:
            scene.job_history.append(previous)
        scene.generation_job = job
        store.save(project)
        try:
            sub = generator.submit(request)
        except CapabilityNotImplementedError:
            # 確定沒有送出：還原狀態後中止
            scene.generation_job = previous
            if previous:
                scene.job_history.pop()
            store.save(project)
            raise
        except SubmissionUncertainError as e:
            job.status, job.error = JobStatus.unknown, str(e)
            outcomes.append(Outcome(sid, "uncertain", f"提交結果不明，已記錄，不會自動重送：{e}"))
        except ProviderError as e:
            job.status, job.error = JobStatus.failed, str(e)
            outcomes.append(Outcome(sid, "error", f"提交失敗：{e}"))
        else:
            job.status, job.job_id, job.model, job.is_mock = (
                JobStatus.pending, sub.job_id, sub.model, sub.is_mock,
            )
            note = "（MOCK 模擬任務，不會產生真實影片）" if sub.is_mock else ""
            outcomes.append(Outcome(sid, "submitted", f"任務 {sub.job_id}{note}"))
        job.updated_at = utcnow()
        store.save(project)
        store.log(
            project.project_id, "video_submit", scene=sid, revision=scene.revision,
            status=job.status, job_id=job.job_id, error=job.error, mock=job.is_mock,
        )
    return outcomes


def poll_videos(
    store: ProjectStore,
    project: Project,
    generator: VideoGenerator,
    *,
    scene_ids: list[str] | None = None,
) -> list[Outcome]:
    """查詢 pending 任務；完成時下載結果存成新版本。不提交任何新任務。"""
    outcomes = []
    for scene in project.select_scenes(scene_ids):
        sid = scene.scene_id
        if _recover_interrupted(scene):
            store.save(project)
        job = scene.generation_job
        if not job or job.status != JobStatus.pending or not job.job_id:
            continue
        if job.provider != generator.name:
            outcomes.append(Outcome(sid, "error", f"任務屬於 {job.provider}，目前 provider 是 {generator.name}"))
            continue
        try:
            state = generator.poll(job.job_id)
        except CapabilityNotImplementedError:
            raise
        except ProviderError as e:
            outcomes.append(Outcome(sid, "error", f"查詢失敗（任務保留為 pending）：{e}"))
            continue
        job.updated_at = utcnow()
        if state.status == JobStatus.succeeded:
            try:
                result = generator.fetch_result(job.job_id)
            except CapabilityNotImplementedError:
                raise
            except ProviderError as e:
                job.error = f"下載失敗，稍後可再查詢：{e}"
                outcomes.append(Outcome(sid, "error", job.error))
                store.save(project)
                continue
            rel = store.next_version_path(project.project_id, "videos", sid, result.suffix)
            store.write_asset(project.project_id, rel, result.data)
            scene.video_history.append(
                AssetVersion(
                    path=rel, scene_revision=job.scene_revision, provider=job.provider,
                    model=job.model, job_id=job.job_id, is_mock=result.is_mock,
                )
            )
            scene.video_path, scene.video_revision = rel, job.scene_revision
            job.status, job.error = JobStatus.succeeded, None
            stale = "" if job.scene_revision == scene.revision else f"（注意：對應舊 revision {job.scene_revision}，不是當前版本）"
            mock = "（MOCK：僅為說明檔，非真實影片）" if result.is_mock else ""
            outcomes.append(Outcome(sid, "completed", f"{rel}{mock}{stale}"))
        elif state.status == JobStatus.failed:
            job.status, job.error = JobStatus.failed, state.error or "供應商回報失敗"
            outcomes.append(Outcome(sid, "failed", job.error))
        else:
            progress = f"（{state.progress}%）" if state.progress is not None else ""
            outcomes.append(Outcome(sid, "pending", f"任務 {job.job_id} 仍在處理中{progress}"))
        store.save(project)
        store.log(
            project.project_id, "video_poll", scene=sid, job_id=job.job_id, status=job.status,
            error=job.error, cost_usd=state.cost_usd, progress=state.progress,
        )
    return outcomes


# ---- 狀態顯示 ----


def scene_status(store: ProjectStore, project: Project, scene: Scene) -> dict[str, str]:
    if not scene.image_path:
        image = "無"
    else:
        mock = any(v.path == scene.image_path and v.is_mock for v in scene.image_history)
        image = "MOCK 占位圖" if mock else "有"
    if scene.is_approved:
        approval = f"已確認 r{scene.revision}"
    elif scene.approval.approved:
        approval = f"失效（確認的是 r{scene.approval.revision}）"
    else:
        approval = "未確認"
    job = scene.generation_job
    job_text = "—"
    if job:
        job_text = job.status.value
        if job.scene_revision != scene.revision:
            job_text += f"（舊 r{job.scene_revision}）"
        if job.is_mock:
            job_text += " [mock]"
        if job.error:
            job_text += f"：{job.error}"
    if scene.has_current_video:
        video = "MOCK（非真實影片）" if scene.current_video_is_mock else "當前版本"
    elif scene.video_path:
        video = f"僅舊版 r{scene.video_revision}"
    else:
        video = "無"
    return {
        "scene": scene.scene_id,
        "order": str(scene.order),
        "rev": str(scene.revision),
        "image": image,
        "approval": approval,
        "job": job_text,
        "video": video,
    }
