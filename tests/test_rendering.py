"""合成測試：濾鏡字串為純函式測試；實際合成需要系統有 ffmpeg，否則略過。"""

import shutil
import subprocess
from pathlib import Path

import pytest

from storybook_video import workflow as wf
from storybook_video.models import AssetVersion
from storybook_video.providers.mock import MockImageGenerator, MockStoryPlanner
from storybook_video.rendering import ClipInfo, RenderNotReady, build_filter, plan_render, probe, render
from storybook_video.storage import ProjectStore

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="需要 ffmpeg")


def clip(i, duration=2.0, audio=True, size=(1280, 720)):
    return ClipInfo(path=Path(f"c{i}.mp4"), duration=duration, width=size[0], height=size[1], has_audio=audio)


def test_filter_without_bgm():
    f = build_filter([clip(0), clip(1, audio=False)])
    assert "[0:v:0]" in f and "[0:a:0]" in f
    assert "[1:a:0]" not in f and "anullsrc" in f  # 無聲的幕補靜音
    assert "concat=n=2:v=1:a=1" in f and f.endswith("[aout]")
    assert "sidechaincompress" not in f


def test_filter_with_bgm_ducks_under_voice():
    f = build_filter([clip(0, 5), clip(1, 7)], bgm_input=2, bgm_volume=0.2)
    assert "[2:a:0]" in f and "volume=0.2" in f and "atrim=duration=12.000" in f
    assert "sidechaincompress" in f and "amix=inputs=2:duration=first" in f


def test_plan_render_rejects_missing_bgm(tmp_path):
    store = ProjectStore(tmp_path)
    p = wf.init_project(store, "x", "一\n\n二")
    with pytest.raises(RenderNotReady) as e:
        plan_render(store, p, bgm=tmp_path / "nope.mp3")
    assert any("背景音樂" in r for r in e.value.reasons)


def _make_clip(path: Path, size: str, seconds: float, audio: bool) -> None:
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
           f"testsrc=size={size}:rate=30:duration={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:a", "aac"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-shortest", str(path)]
    subprocess.run(cmd, check=True)


@needs_ffmpeg
def test_render_real_clips_with_bgm(tmp_path):
    store = ProjectStore(tmp_path / "projects")
    p = wf.init_project(store, "r", "一\n\n二\n\n三")
    wf.plan_story(store, p, MockStoryPlanner())
    wf.generate_images(store, p, MockImageGenerator())
    wf.approve_scenes(store, p, approve_all=True)
    specs = [("640x360", 1.5, True), ("640x360", 1.0, False), ("480x360", 1.2, True)]  # 無聲、尺寸不同
    for scene, (size, sec, audio) in zip(p.ordered_scenes(), specs):
        rel = f"assets/videos/{scene.scene_id}/v001.mp4"
        dest = store.resolve("r", rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        _make_clip(dest, size, sec, audio)
        scene.video_path, scene.video_revision = rel, scene.revision
        scene.video_history.append(AssetVersion(path=rel, scene_revision=scene.revision, provider="test"))
    store.save(p)

    bgm = tmp_path / "bgm.wav"
    subprocess.run([FFMPEG, "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=220:duration=1", str(bgm)], check=True)

    p.get_scene("s01").render_seconds = 1.0  # 1.5 秒的片段只用前 1 秒
    store.save(p)
    out = render(store, p, plan_render(store, p, bgm=bgm, bgm_volume=0.2, ambience=bgm, ambience_volume=0.3))
    info = probe(FFMPEG, out)
    assert info.has_audio and (info.width, info.height) == (640, 360)
    assert info.duration == pytest.approx(3.2, abs=0.2)
    # 再合成一次會產生新版本，不覆蓋
    out2 = render(store, p, plan_render(store, p))
    assert out2.name == "story_v002.mp4" and out.exists()


def _rendered_project(tmp_path):
    store = ProjectStore(tmp_path / "projects")
    p = wf.init_project(store, "pub", "# 測試標題\n\n一\n\n二")
    wf.plan_story(store, p, MockStoryPlanner())
    wf.generate_images(store, p, MockImageGenerator())
    wf.approve_scenes(store, p, approve_all=True)
    for scene in p.ordered_scenes():
        rel = f"assets/videos/{scene.scene_id}/v001.mp4"
        dest = store.resolve("pub", rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        _make_clip(dest, "320x180", 1.0, True)
        scene.video_path, scene.video_revision = rel, scene.revision
        scene.video_history.append(AssetVersion(path=rel, scene_revision=scene.revision, provider="test"))
    store.save(p)
    return store, p


@needs_ffmpeg
def test_publish_site(tmp_path):
    from storybook_video.publish import PublishError, publish_site

    store, p = _rendered_project(tmp_path)
    site = tmp_path / "site"
    with pytest.raises(PublishError, match="還沒有完整影片"):
        publish_site(store, ["pub"], site)

    render(store, p, plan_render(store, p))
    stories = publish_site(store, ["pub"], site)
    assert stories[0].title == "測試標題" and stories[0].scene_count == 2
    page = (site / "stories/pub/index.html").read_text(encoding="utf-8")
    assert "story.mp4" in page and "pages/s01.svg" in page
    assert "[MOCK] 第 1 幕插畫" not in page  # 不公開提示詞
    assert (site / ".nojekyll").exists() and (site / "stories/pub/story.mp4").exists()
    assert not list(site.rglob("*.source"))

    # 修改一幕後，舊成品不能再發佈
    wf.edit_scene(store, p, "s01", display_text="改過")
    with pytest.raises(PublishError, match="重新 render"):
        publish_site(store, ["pub"], site)


def test_filter_with_bgm_and_ambience():
    f = build_filter([clip(0, 5), clip(1, 7)], bgm_input=2, bgm_volume=0.2, ambience_input=3, ambience_volume=0.4)
    assert "asplit=3[voice][sc_bg][sc_amb]" in f
    assert "[3:a:0]" in f and "volume=0.4" in f
    assert "[bg][sc_bg]sidechaincompress=threshold=0.02:ratio=6" in f
    assert "[amb][sc_amb]sidechaincompress=threshold=0.02:ratio=2" in f
    assert "[voice][bgduck][ambduck]amix=inputs=3" in f


def test_filter_ambience_only():
    f = build_filter([clip(0)], ambience_input=1)
    assert "asplit=2[voice][sc_amb]" in f and "amix=inputs=2" in f and "[bg]" not in f


def test_filter_trims_clip():
    c = clip(0, 4.0)
    c.trimmed = True
    f = build_filter([c, clip(1, 3.0, audio=False)])
    assert "[0:v:0]trim=duration=4.000,setpts=PTS-STARTPTS,scale=" in f
    assert "[0:a:0]atrim=duration=4.000,asetpts=PTS-STARTPTS," in f
    assert "[1:v:0]scale=" in f  # 沒剪的幕不加 trim


def test_trim_does_not_change_revision(tmp_path):
    store = ProjectStore(tmp_path)
    p = wf.init_project(store, "t", "一\n\n二")
    wf.plan_story(store, p, MockStoryPlanner())
    wf.generate_images(store, p, MockImageGenerator())
    wf.approve_scenes(store, p, approve_all=True)
    p.get_scene("s01").render_seconds = 3
    store.save(p)
    p = store.load("t")
    assert p.get_scene("s01").is_approved and p.get_scene("s01").render_seconds == 3


def test_filter_voice_volume():
    f = build_filter([clip(0)], bgm_input=1, voice_volume=0.7)
    assert "[acat]volume=0.7,afade=t=in" in f
    assert "volume=1.0" not in build_filter([clip(0)])
