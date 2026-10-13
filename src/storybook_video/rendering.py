"""完整故事影片合成（FFmpeg，透過 subprocess）。

只接受「當前 revision 已確認、且有當前版本真實影片」的幕；條件不足時回報原因，不產生假 MP4。

合成方式：
- 各幕先統一解析度、fps、像素格式與音訊格式，再用 concat filter 依序接起來（重新編碼最穩妥）。
- 沒有音軌的幕（audio_mode=none 生成的無聲影片）補靜音。
- 整支影片頭尾淡入淡出。
- 可選背景音樂：循環鋪滿全片、淡入淡出，並以 sidechain 壓縮在有人聲時自動壓低。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .models import Project
from .storage import ProjectStore

FPS = 24
AUDIO_RATE = 48000
FADE_IN = 0.5
FADE_OUT = 1.0
BGM_FADE_IN = 2.0
BGM_FADE_OUT = 3.0


class RenderNotReady(Exception):
    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        super().__init__("無法合成：\n" + "\n".join(f"  - {r}" for r in reasons))


@dataclass
class ClipInfo:
    path: Path
    duration: float
    width: int
    height: int
    has_audio: bool


@dataclass
class RenderPlan:
    inputs: list[Path]
    output_rel: str
    ffmpeg: str
    bgm: Path | None = None
    bgm_volume: float = 0.18


def find_ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def collect_render_inputs(store: ProjectStore, project: Project) -> list[Path]:
    """回傳依 order 排列的當前版本影片路徑；有任何一幕不符就丟 RenderNotReady。"""
    reasons: list[str] = []
    inputs: list[Path] = []
    scenes = project.ordered_scenes()
    if not scenes:
        raise RenderNotReady(["尚無分鏡"])
    for s in scenes:
        if not s.is_approved:
            reasons.append(f"{s.scene_id}：當前 revision {s.revision} 未確認")
            continue
        if not s.has_current_video:
            if s.video_path:
                reasons.append(f"{s.scene_id}：只有舊版 r{s.video_revision} 的影片，當前是 r{s.revision}")
            else:
                reasons.append(f"{s.scene_id}：尚無影片")
            continue
        if s.current_video_is_mock:
            reasons.append(f"{s.scene_id}：影片是 mock 模擬結果，不是真實影片")
            continue
        path = store.resolve(project.project_id, s.video_path)
        if not path.exists():
            reasons.append(f"{s.scene_id}：影片檔不存在 {s.video_path}")
            continue
        inputs.append(path)
    if reasons:
        raise RenderNotReady(reasons)
    return inputs


def plan_render(
    store: ProjectStore, project: Project, *, bgm: Path | None = None, bgm_volume: float = 0.18
) -> RenderPlan:
    reasons = []
    try:
        inputs = collect_render_inputs(store, project)
    except RenderNotReady as e:
        reasons.extend(e.reasons)
        inputs = []
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        reasons.append("找不到 ffmpeg，請先安裝並確認在 PATH 中（例如 apt install ffmpeg / brew install ffmpeg）")
    if bgm is not None and not bgm.is_file():
        reasons.append(f"找不到背景音樂檔：{bgm}")
    if not 0 <= bgm_volume <= 1:
        reasons.append(f"背景音樂音量需在 0–1 之間，收到 {bgm_volume}")
    if reasons:
        raise RenderNotReady(reasons)
    return RenderPlan(
        inputs=inputs, output_rel=store.next_output_path(project.project_id), ffmpeg=ffmpeg,
        bgm=bgm, bgm_volume=bgm_volume,
    )


_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO_RE = re.compile(r"Stream #\d+:\d+.*?: Video: (?!mjpeg|png).*?(\d{2,5})x(\d{2,5})")
_AUDIO_RE = re.compile(r"Stream #\d+:\d+.*?: Audio:")


def probe(ffmpeg: str, path: Path) -> ClipInfo:
    """用 `ffmpeg -i` 的輸出取得時長、尺寸與是否有音軌（不依賴 ffprobe）。"""
    proc = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)], capture_output=True, text=True)
    text = proc.stderr
    m = _DURATION_RE.search(text)
    v = _VIDEO_RE.search(text)
    if not m or not v:
        raise RuntimeError(f"無法讀取影片資訊：{path}\n{text[-500:]}")
    h, mi, s = m.groups()
    return ClipInfo(
        path=path,
        duration=int(h) * 3600 + int(mi) * 60 + float(s),
        width=int(v.group(1)),
        height=int(v.group(2)),
        has_audio=bool(_AUDIO_RE.search(text)),
    )


def build_filter(clips: list[ClipInfo], *, bgm_input: int | None = None, bgm_volume: float = 0.18) -> str:
    """產生 filter_complex。輸出標籤為 [vout] 與 [aout]。"""
    w, h = clips[0].width, clips[0].height  # 以第一幕為準
    total = sum(c.duration for c in clips)
    parts = []
    for i, c in enumerate(clips):
        parts.append(
            f"[{i}:v:0]scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={FPS},format=yuv420p[v{i}]"
        )
        if c.has_audio:
            parts.append(f"[{i}:a:0]aresample={AUDIO_RATE},aformat=channel_layouts=stereo[a{i}]")
        else:
            parts.append(
                f"anullsrc=r={AUDIO_RATE}:cl=stereo,atrim=duration={c.duration:.3f}[a{i}]"
            )
    streams = "".join(f"[v{i}][a{i}]" for i in range(len(clips)))
    fade_out_at = max(total - FADE_OUT, 0)
    parts.append(f"{streams}concat=n={len(clips)}:v=1:a=1[vcat][acat]")
    parts.append(f"[vcat]fade=t=in:d={FADE_IN},fade=t=out:st={fade_out_at:.3f}:d={FADE_OUT}[vout]")
    voice_chain = f"afade=t=in:d={FADE_IN},afade=t=out:st={fade_out_at:.3f}:d={FADE_OUT}"
    if bgm_input is None:
        parts.append(f"[acat]{voice_chain}[aout]")
    else:
        bgm_out_at = max(total - BGM_FADE_OUT, 0)
        parts.append(f"[acat]{voice_chain},asplit=2[voice][sc]")
        parts.append(
            f"[{bgm_input}:a:0]aresample={AUDIO_RATE},aformat=channel_layouts=stereo,"
            f"atrim=duration={total:.3f},volume={bgm_volume},"
            f"afade=t=in:d={BGM_FADE_IN},afade=t=out:st={bgm_out_at:.3f}:d={BGM_FADE_OUT}[bg]"
        )
        # 有人聲時壓低背景音樂
        parts.append("[bg][sc]sidechaincompress=threshold=0.02:ratio=6:attack=30:release=500[bgduck]")
        parts.append("[voice][bgduck]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[aout]")
    return ";".join(parts)


def build_command(plan: RenderPlan, clips: list[ClipInfo], out: Path) -> list[str]:
    cmd = [plan.ffmpeg, "-hide_banner", "-loglevel", "error", "-n"]
    for c in clips:
        cmd += ["-i", str(c.path)]
    bgm_input = None
    if plan.bgm:
        bgm_input = len(clips)
        cmd += ["-stream_loop", "-1", "-i", str(plan.bgm)]
    cmd += [
        "-filter_complex", build_filter(clips, bgm_input=bgm_input, bgm_volume=plan.bgm_volume),
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(out),
    ]
    return cmd


def render(store: ProjectStore, project: Project, plan: RenderPlan) -> Path:
    base = store.project_dir(project.project_id).resolve()
    out = base / plan.output_rel
    clips = [probe(plan.ffmpeg, p) for p in plan.inputs]
    cmd = build_command(plan, clips, out)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    store.log(
        project.project_id, "render", output=plan.output_rel, returncode=proc.returncode,
        inputs=[str(p.relative_to(base)) for p in plan.inputs],
        bgm=str(plan.bgm) if plan.bgm else None, bgm_volume=plan.bgm_volume if plan.bgm else None,
        duration=round(sum(c.duration for c in clips), 2),
    )
    if proc.returncode != 0:
        out.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg 失敗（{proc.returncode}）：\n{proc.stderr[-2000:]}")
    return out
