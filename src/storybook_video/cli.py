"""命令列介面：storybook <指令> ...

流程：init → plan → images → review → (edit) → approve → videos → status → render
"""

from __future__ import annotations

import functools
import json
import shutil
from pathlib import Path
from typing import Annotated, Optional

import typer

from . import workflow as wf
from .config import load_settings
from .models import AudioMode, Character
from .preview import build_preview
from .publish import PublishError, publish_site
from .providers import build_providers
from .providers.base import ProviderError
from .rendering import RenderNotReady, find_ffmpeg, plan_render, render
from .storage import ProjectStore, StorageError

app = typer.Typer(help="有聲故事書影片製作 CLI", no_args_is_help=True, add_completion=False)

ProjectArg = Annotated[str, typer.Argument(help="專案 ID（projects/ 下的資料夾名）")]
ScenesOpt = Annotated[
    Optional[list[str]], typer.Option("--scene", "-s", help="只處理指定幕，可重複，例如 -s s01 -s s03")
]
ForceOpt = Annotated[bool, typer.Option("--force", help="強制重做（已有當前版本成品也重新生成）")]
DryRunOpt = Annotated[bool, typer.Option("--dry-run", help="只顯示將處理哪些幕與設定，不送出請求")]

_state: dict = {}


@app.callback()
def main(
    projects_dir: Annotated[
        Optional[Path], typer.Option("--projects-dir", help="專案存放位置（預設 $STORYBOOK_PROJECTS_DIR 或 ./projects）")
    ] = None,
) -> None:
    settings = load_settings()
    _state["settings"] = settings
    _state["store"] = ProjectStore(projects_dir or settings.projects_dir)


def _store() -> ProjectStore:
    return _state["store"]


def handle_errors(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (wf.WorkflowError, StorageError, ProviderError, KeyError, ValueError) as e:
            msg = e.args[0] if isinstance(e, KeyError) and e.args else str(e)
            typer.secho(f"錯誤：{msg}", fg=typer.colors.RED, err=True)
            raise typer.Exit(1)

    return wrapper


def _print_outcomes(outcomes: list[wf.Outcome], verbose: bool = False) -> None:
    colors = {
        "generated": "green", "submitted": "green", "approved": "green", "completed": "green",
        "skipped": "yellow", "pending": "yellow", "would_generate": "cyan", "would_submit": "cyan",
        "would_poll": "cyan", "updated": "yellow", "error": "red", "failed": "red", "uncertain": "red",
    }
    if not outcomes:
        typer.echo("（沒有需要處理的幕）")
    for o in outcomes:
        typer.secho(f"  {o.scene_id:<5} {o.action:<15} {o.message}", fg=colors.get(o.action))
        if verbose:
            for k, v in o.details.items():
                text = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
                typer.echo("        " + f"{k}: {text}".replace("\n", "\n        "))


def _load(project_id: str):
    store = _store()
    project = store.load(project_id)
    return store, project, build_providers(project, _state["settings"])


@app.command()
@handle_errors
def init(
    story_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="故事文字檔（UTF-8）")],
    project_id: Annotated[str, typer.Option("--id", help="專案 ID（英數、-、_）")],
    style: Annotated[str, typer.Option(help="共用畫風")] = "",
    aspect_ratio: Annotated[str, typer.Option("--aspect-ratio", help="畫面比例，例如 16:9、9:16")] = "16:9",
    provider: Annotated[str, typer.Option(help="mock 或 xai；整個專案沿用")] = "mock",
    characters: Annotated[
        Optional[Path], typer.Option(exists=True, dir_okay=False, help="角色設定 JSON：[{name, kind, description, reference_prompt, reference_paths}]")
    ] = None,
    brief: Annotated[
        Optional[Path], typer.Option(exists=True, dir_okay=False, help="創作說明（情緒、視覺設定、分鏡骨架），分鏡時交給 LLM")
    ] = None,
) -> None:
    """從故事檔建立製作專案。"""
    if provider not in ("mock", "xai"):
        raise wf.WorkflowError("provider 只能是 mock 或 xai")
    chars = _load_characters(characters) if characters else []
    store = _store()
    if store.exists(project_id):
        raise StorageError(f"專案已存在：{store.project_dir(project_id)}")
    # 角色參考圖複製進專案，之後一律用相對路徑
    pdir = store.project_dir(project_id)
    for c in chars:
        copied = []
        for ref in c.reference_paths:
            src = (characters.parent / ref) if not Path(ref).is_absolute() else Path(ref)
            if not src.exists():
                raise wf.WorkflowError(f"找不到角色參考圖：{src}")
            rel = Path("assets/characters") / src.name
            (pdir / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, pdir / rel)
            copied.append(rel.as_posix())
        c.reference_paths = copied
    project = wf.init_project(
        store, project_id, story_file.read_text(encoding="utf-8"),
        style=style, aspect_ratio=aspect_ratio, provider=provider, characters=chars,
        brief=brief.read_text(encoding="utf-8") if brief else "",
    )
    typer.secho(f"已建立專案 {project.project_id}（provider={provider}）：{pdir}", fg="green")
    typer.echo(f"下一步：storybook plan {project_id}")


def _load_characters(path: Path) -> list[Character]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [Character.model_validate(c) for c in data]
    except (json.JSONDecodeError, ValueError) as e:
        raise wf.WorkflowError(f"角色設定檔格式錯誤：{path}\n{e}") from e


@app.command()
@handle_errors
def plan(project_id: ProjectArg, force: ForceOpt = False, dry_run: DryRunOpt = False) -> None:
    """用 LLM（或 mock）把故事拆成分鏡與講稿。"""
    store, project, providers = _load(project_id)
    if dry_run:
        ps = _state["settings"].effective_project_settings(project.settings)
        model = ps.text_model if providers.planner.name != "mock" else "（mock 不使用模型）"
        typer.echo(f"[dry-run] provider={providers.planner.name} text_model={model or '未設定'}")
        typer.echo(f"  故事長度 {len(project.source_story)} 字，現有分鏡 {len(project.scenes)} 幕"
                   + ("，將被取代（--force）" if project.scenes and force else ""))
        return
    scenes = wf.plan_story(store, project, providers.planner, force=force)
    label = "（MOCK：依段落切分，非 LLM 結果）" if providers.planner.name == "mock" else ""
    typer.secho(f"已產生 {len(scenes)} 幕分鏡{label}", fg="green")
    for s in scenes:
        typer.echo(f"  {s.scene_id} [{s.audio_mode}] {s.display_text[:40]}")
    typer.echo(f"下一步：storybook images {project_id}")


@app.command()
@handle_errors
def refs(
    project_id: ProjectArg,
    name: Annotated[Optional[list[str]], typer.Option("--name", "-n", help="只處理指定角色／物件，可重複")] = None,
    force: ForceOpt = False,
    dry_run: DryRunOpt = False,
) -> None:
    """產生角色、物件與畫風參考圖（畫風先做，其餘以畫風參考圖為輸入）。"""
    store, project, providers = _load(project_id)
    outcomes = wf.generate_references(store, project, providers.images, names=name, force=force, dry_run=dry_run)
    _print_outcomes(outcomes, verbose=dry_run)


@app.command()
@handle_errors
def images(
    project_id: ProjectArg, scene: ScenesOpt = None, force: ForceOpt = False, dry_run: DryRunOpt = False
) -> None:
    """生成全部或指定幕的圖片（每次新增版本，不覆蓋舊圖；新圖會使該幕確認失效）。"""
    store, project, providers = _load(project_id)
    outcomes = wf.generate_images(store, project, providers.images, scene_ids=scene, force=force, dry_run=dry_run)
    _print_outcomes(outcomes, verbose=dry_run)


@app.command()
@handle_errors
def edit(
    project_id: ProjectArg,
    scene: Annotated[str, typer.Option("--scene", "-s", help="要修改的幕")],
    display_text: Annotated[Optional[str], typer.Option(help="頁面文字")] = None,
    narration: Annotated[Optional[str], typer.Option(help="旁白或台詞")] = None,
    audio_mode: Annotated[Optional[AudioMode], typer.Option(help="voiceover / dialogue / ambient / none")] = None,
    sound_prompt: Annotated[Optional[str], typer.Option(help="聲音設計（海浪、風、腳步、雨…）")] = None,
    image_prompt: Annotated[Optional[str], typer.Option(help="圖片提示詞")] = None,
    motion_prompt: Annotated[Optional[str], typer.Option(help="動作與鏡頭")] = None,
    duration: Annotated[Optional[float], typer.Option(help="時長（秒）")] = None,
    character_ref: Annotated[
        Optional[list[str]], typer.Option("--character-ref", help="角色參考圖（專案內相對路徑，可重複；會取代原設定）")
    ] = None,
) -> None:
    """修改某一幕。內容有變就升 revision，該幕確認失效，其他幕不受影響。"""
    store, project, _ = _load(project_id)
    changed = wf.edit_scene(
        store, project, scene,
        display_text=display_text, narration_or_dialogue=narration, audio_mode=audio_mode,
        image_prompt=image_prompt, motion_prompt=motion_prompt, duration_seconds=duration,
        sound_prompt=sound_prompt, character_reference_paths=character_ref,
    )
    s = project.get_scene(scene)
    if changed:
        typer.secho(f"{scene} 已更新為 revision {s.revision}，需重新確認", fg="yellow")
        if image_prompt is not None:
            typer.echo(f"圖片提示已改；如需新圖：storybook images {project_id} -s {scene} --force")
    else:
        typer.echo("內容沒有變化")


@app.command()
@handle_errors
def trim(
    project_id: ProjectArg,
    scene: Annotated[list[str], typer.Option("--scene", "-s", help="要設定的幕，可重複")],
    seconds: Annotated[Optional[float], typer.Option("--seconds", help="合成時只用前 N 秒")] = None,
    clear: Annotated[bool, typer.Option("--clear", help="取消剪短，整段使用")] = False,
) -> None:
    """設定合成時每幕只用前幾秒（剪輯設定：不重新生成、不影響確認）。"""
    if (seconds is None) == (not clear):
        raise wf.WorkflowError("請指定 --seconds N 或 --clear 其中一個")
    if seconds is not None and seconds <= 0:
        raise wf.WorkflowError("--seconds 需大於 0")
    store, project, _ = _load(project_id)
    for s in project.select_scenes(scene):
        s.render_seconds = None if clear else seconds
        typer.echo(f"  {s.scene_id}  合成長度：{'整段' if clear else f'前 {seconds:g} 秒'}")
    store.save(project)
    store.log(project_id, "trim", scenes=scene, seconds=None if clear else seconds)
    typer.echo(f"重新合成後生效：storybook render {project_id} ...")


@app.command()
@handle_errors
def review(project_id: ProjectArg) -> None:
    """產生本機靜態分鏡預覽 HTML。"""
    store, project, _ = _load(project_id)
    out = build_preview(store, project)
    typer.secho(f"預覽頁：{out.resolve()}", fg="green")
    typer.echo("用瀏覽器開啟查看；確認請用：storybook approve <專案> --scene <幕> 或 --all")


@app.command()
@handle_errors
def approve(
    project_id: ProjectArg,
    scene: ScenesOpt = None,
    all_: Annotated[bool, typer.Option("--all", help="明確確認全部幕的當前 revision")] = False,
    revoke: Annotated[bool, typer.Option("--revoke", help="撤銷指定幕的確認")] = False,
) -> None:
    """確認指定幕的當前 revision（圖片與講稿）。"""
    store, project, _ = _load(project_id)
    if revoke:
        if not scene:
            raise wf.WorkflowError("--revoke 需要搭配 --scene")
        wf.revoke_approval(store, project, scene)
        typer.secho(f"已撤銷確認：{', '.join(scene)}", fg="yellow")
        return
    _print_outcomes(wf.approve_scenes(store, project, scene_ids=scene, approve_all=all_))


@app.command()
@handle_errors
def videos(
    project_id: ProjectArg, scene: ScenesOpt = None, force: ForceOpt = False, dry_run: DryRunOpt = False
) -> None:
    """為已確認的幕提交影片生成（已有 pending 任務只查詢，不重複提交）。"""
    store, project, providers = _load(project_id)
    outcomes = wf.submit_videos(store, project, providers.videos, scene_ids=scene, force=force, dry_run=dry_run)
    _print_outcomes(outcomes, verbose=dry_run)
    if providers.videos.name == "mock" and not dry_run:
        typer.secho("提醒：mock 只模擬任務狀態，不會產生真實影片或中文旁白。", fg="yellow")


@app.command()
@handle_errors
def status(
    project_id: ProjectArg,
    poll: Annotated[bool, typer.Option("--poll", help="向供應商查詢 pending 任務並下載完成的結果")] = False,
    scene: ScenesOpt = None,
) -> None:
    """顯示各幕狀態；--poll 會查詢已提交的任務。"""
    store, project, providers = _load(project_id)
    if poll:
        typer.echo("查詢任務：")
        _print_outcomes(wf.poll_videos(store, project, providers.videos, scene_ids=scene))
        typer.echo()
    typer.echo(f"專案 {project.project_id} · provider={project.settings.provider} · {len(project.scenes)} 幕")
    cols = ["scene", "order", "rev", "image", "approval", "job", "video"]
    rows = [wf.scene_status(store, project, s) for s in project.select_scenes(scene)]
    widths = {c: max([_w(c)] + [_w(r[c]) for r in rows]) for c in cols[:-1]}
    typer.echo("  ".join(_pad(c, widths.get(c)) for c in cols))
    for r in rows:
        typer.echo("  ".join(_pad(r[c], widths.get(c)) for c in cols))


def _w(text: str) -> int:
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)


def _pad(text: str, width: int | None) -> str:
    return text if width is None else text + " " * (width - _w(text))


@app.command("render")
@handle_errors
def render_cmd(
    project_id: ProjectArg,
    bgm: Annotated[
        Optional[Path], typer.Option("--bgm", help="背景音樂檔（mp3/wav 等），循環鋪滿全片，有人聲時自動壓低")
    ] = None,
    bgm_volume: Annotated[float, typer.Option("--bgm-volume", help="背景音樂音量（0–1）")] = 0.18,
    ambience: Annotated[
        Optional[Path], typer.Option("--ambience", help="環境音底（海浪、風、雨…），循環鋪滿全片，有人聲時輕微壓低")
    ] = None,
    ambience_volume: Annotated[float, typer.Option("--ambience-volume", help="環境音音量（0–1）")] = 0.5,
    voice_volume: Annotated[float, typer.Option("--voice-volume", help="各幕原聲（旁白、台詞）音量，1 為原音量")] = 1.0,
    dry_run: DryRunOpt = False,
) -> None:
    """按順序合成當前版本影片為完整故事影片（需要 FFmpeg 與真實影片）。"""
    store, project, _ = _load(project_id)
    try:
        rp = plan_render(store, project, bgm=bgm, bgm_volume=bgm_volume,
                         ambience=ambience, ambience_volume=ambience_volume, voice_volume=voice_volume)
    except RenderNotReady as e:
        typer.secho(str(e), fg="red", err=True)
        typer.echo(f"ffmpeg：{find_ffmpeg() or '未安裝'}", err=True)
        raise typer.Exit(1)
    music = f"，背景音樂 {rp.bgm}（音量 {rp.bgm_volume}）" if rp.bgm else "，無背景音樂"
    if rp.ambience:
        music += f"，環境音 {rp.ambience}（音量 {rp.ambience_volume}）"
    if rp.voice_volume != 1.0:
        music += f"，原聲音量 {rp.voice_volume}"
    typer.echo(f"將依序合成 {len(rp.inputs)} 段{music} → {rp.output_rel}")
    if dry_run:
        for p in rp.inputs:
            typer.echo(f"  {p}")
        return
    try:
        out = render(store, project, rp)
    except RuntimeError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(1)
    typer.secho(f"完成：{out}", fg="green")


@app.command()
@handle_errors
def publish(
    project_ids: Annotated[list[str], typer.Argument(help="要公開的專案 ID，可多個")],
    out: Annotated[Path, typer.Option("--out", help="網站輸出資料夾")] = Path("site"),
) -> None:
    """輸出可公開的展示網站（首頁＋各故事頁），只含成品，不含提示詞與紀錄。"""
    try:
        stories = publish_site(_store(), project_ids, out)
    except PublishError as e:
        typer.secho(f"錯誤：{e}", fg="red", err=True)
        raise typer.Exit(1)
    for st in stories:
        size = (out / "stories" / st.project_id / "story.mp4").stat().st_size / 1e6
        typer.echo(f"  {st.project_id:<16} {st.title}（{st.scene_count} 頁，影片 {size:.1f} MB）")
    total = sum(f.stat().st_size for f in out.rglob("*") if f.is_file()) / 1e6
    typer.secho(f"網站已輸出到 {out.resolve()}（共 {total:.1f} MB）", fg="green")


if __name__ == "__main__":
    app()
