import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from storybook_video import workflow as wf
from storybook_video.cli import app
from storybook_video.config import Settings
from storybook_video.models import JobStatus, ProjectSettings
from storybook_video.providers import build_providers
from storybook_video.providers.base import (
    PlanFormatError,
    SubmissionUncertainError,
    VideoGenerator,
    VideoJobState,
    VideoResult,
    VideoSubmission,
    parse_story_plan,
)
from storybook_video.providers.mock import MockImageGenerator, MockStoryPlanner, MockVideoGenerator
from storybook_video.rendering import RenderNotReady, collect_render_inputs
from storybook_video.storage import ProjectStore

STORY = """# 測試故事

第一段，小兔出門。

第二段，小兔說：「你好！」

第三段，月亮出來了。
"""


class FakeVideo(VideoGenerator):
    """可控制的影片供應商：記錄提交次數，poll 結果可指定。"""

    name = "fake"

    def __init__(self, poll_status=JobStatus.pending, submit_error=None):
        self.submits = []
        self.polls = []
        self.poll_status = poll_status
        self.submit_error = submit_error

    def submit(self, request):
        if self.submit_error:
            raise self.submit_error
        self.submits.append(request.scene_id)
        return VideoSubmission(job_id=f"job-{request.scene_id}-{len(self.submits)}", provider=self.name)

    def poll(self, job_id):
        self.polls.append(job_id)
        return VideoJobState(status=self.poll_status)

    def fetch_result(self, job_id):
        return VideoResult(data=b"\x00\x00\x00\x18ftypmp42", suffix=".mp4")


@pytest.fixture
def store(tmp_path):
    return ProjectStore(tmp_path / "projects")


@pytest.fixture
def project(store):
    p = wf.init_project(store, "demo", STORY, style="水彩")
    wf.plan_story(store, p, MockStoryPlanner())
    wf.generate_images(store, p, MockImageGenerator())
    return store.load("demo")


def approve_all(store, project):
    wf.approve_scenes(store, project, approve_all=True)


# ---- 必要測試 ----


def test_unapproved_scene_not_submitted(store, project):
    video = FakeVideo()
    wf.approve_scenes(store, project, scene_ids=["s01"])
    outcomes = wf.submit_videos(store, project, video)
    assert video.submits == ["s01"]
    skipped = {o.scene_id for o in outcomes if o.action == "skipped"}
    assert skipped == {"s02", "s03"}
    # 明確指定未確認的幕也不會提交
    wf.submit_videos(store, project, video, scene_ids=["s02"], force=True)
    assert video.submits == ["s01"]


def test_edit_invalidates_only_that_scene(store, project):
    approve_all(store, project)
    before = {s.scene_id: s.revision for s in project.scenes}
    assert wf.edit_scene(store, project, "s02", narration_or_dialogue="新的台詞")
    reloaded = store.load("demo")
    s1, s2, s3 = (reloaded.get_scene(i) for i in ("s01", "s02", "s03"))
    assert s1.is_approved and s3.is_approved
    assert not s2.is_approved
    assert s2.revision == before["s02"] + 1
    assert s1.revision == before["s01"] and s3.revision == before["s03"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("image_prompt", "新的提示"),
        ("motion_prompt", "鏡頭左移"),
        ("duration_seconds", 9),
        ("character_reference_paths", ["assets/characters/x.png"]),
        ("image_path", "assets/images/s01/v999.png"),
    ],
)
def test_each_content_field_invalidates_approval(store, project, field, value):
    approve_all(store, project)
    wf.edit_scene(store, project, "s01", **{field: value})
    assert not project.get_scene("s01").is_approved
    assert project.get_scene("s02").is_approved


def test_pending_job_is_polled_not_resubmitted(store, project):
    approve_all(store, project)
    video = FakeVideo(poll_status=JobStatus.pending)
    wf.submit_videos(store, project, video, scene_ids=["s01"])
    assert video.submits == ["s01"]
    project = store.load("demo")
    outcomes = wf.submit_videos(store, project, video, scene_ids=["s01"], force=True)
    assert video.submits == ["s01"], "pending 時不可重複提交，--force 也一樣"
    assert len(video.polls) == 1
    assert outcomes[0].action == "pending"


def test_old_video_not_used_as_current(store, project):
    approve_all(store, project)
    video = FakeVideo(poll_status=JobStatus.succeeded)
    wf.submit_videos(store, project, video)
    wf.poll_videos(store, project, video)
    assert all(s.has_current_video for s in project.scenes)
    assert len(collect_render_inputs(store, project)) == 3

    wf.edit_scene(store, project, "s02", motion_prompt="改成拉遠")
    wf.approve_scenes(store, project, scene_ids=["s02"])
    s2 = project.get_scene("s02")
    assert s2.video_path is not None  # 舊影片保留
    assert store.resolve("demo", s2.video_path).exists()
    assert not s2.has_current_video
    with pytest.raises(RenderNotReady) as exc:
        collect_render_inputs(store, project)
    assert any("s02" in r and "舊版" in r for r in exc.value.reasons)

    # 重跑：只有 s02 會重新提交，新影片存成新版本
    wf.submit_videos(store, project, video)
    assert video.submits.count("s02") == 2 and video.submits.count("s01") == 1
    wf.poll_videos(store, project, video)
    assert s2.has_current_video and s2.video_path.endswith("v002.mp4")
    assert [v.path for v in s2.video_history] == ["assets/videos/s02/v001.mp4", "assets/videos/s02/v002.mp4"]


# ---- 其他規則 ----


def test_skip_existing_unless_force(store, project):
    approve_all(store, project)
    video = FakeVideo(poll_status=JobStatus.succeeded)
    wf.submit_videos(store, project, video)
    wf.poll_videos(store, project, video)
    wf.submit_videos(store, project, video)
    assert len(video.submits) == 3
    wf.submit_videos(store, project, video, scene_ids=["s03"], force=True)
    assert video.submits[-1] == "s03" and len(video.submits) == 4


def test_uncertain_submission_not_auto_resubmitted(store, project):
    approve_all(store, project)
    video = FakeVideo(submit_error=SubmissionUncertainError("timeout"))
    out = wf.submit_videos(store, project, video, scene_ids=["s01"])
    assert out[0].action == "uncertain"
    assert store.load("demo").get_scene("s01").generation_job.status == JobStatus.unknown

    video.submit_error = None
    project = store.load("demo")
    out = wf.submit_videos(store, project, video, scene_ids=["s01"])
    assert video.submits == [] and out[0].action == "skipped"
    wf.submit_videos(store, project, video, scene_ids=["s01"], force=True)
    assert video.submits == ["s01"]


def test_interrupted_submit_becomes_unknown(store, project):
    approve_all(store, project)

    class Crash(FakeVideo):
        def submit(self, request):
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        wf.submit_videos(store, project, Crash(), scene_ids=["s01"])
    project = store.load("demo")
    assert project.get_scene("s01").generation_job.status == JobStatus.submitting
    video = FakeVideo()
    wf.submit_videos(store, project, video, scene_ids=["s01"])
    assert video.submits == []
    assert project.get_scene("s01").generation_job.status == JobStatus.unknown


def test_manual_json_edit_detected(store, project):
    approve_all(store, project)
    path = store.project_dir("demo") / "project.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["scenes"][2]["narration_or_dialogue"] = "手動改過"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    reloaded = store.load("demo")
    assert not reloaded.get_scene("s03").is_approved
    assert reloaded.get_scene("s01").is_approved


def test_images_are_versioned_and_reset_approval(store, project):
    approve_all(store, project)
    wf.generate_images(store, project, MockImageGenerator(), scene_ids=["s01"], force=True)
    s1 = project.get_scene("s01")
    assert s1.image_path == "assets/images/s01/v002.svg"
    assert store.resolve("demo", "assets/images/s01/v001.svg").exists()
    assert not s1.is_approved and project.get_scene("s02").is_approved


def test_mock_video_is_not_renderable(store, project):
    approve_all(store, project)
    video = MockVideoGenerator()
    wf.submit_videos(store, project, video)
    wf.poll_videos(store, project, video)
    s1 = project.get_scene("s01")
    assert s1.video_path.endswith(".mock.json") and s1.current_video_is_mock
    with pytest.raises(RenderNotReady) as exc:
        collect_render_inputs(store, project)
    assert all("mock" in r for r in exc.value.reasons)


def test_dry_run_submits_nothing(store, project):
    approve_all(store, project)
    video = FakeVideo()
    out = wf.submit_videos(store, project, video, dry_run=True)
    assert video.submits == [] and {o.action for o in out} == {"would_submit"}
    assert store.load("demo").get_scene("s01").generation_job is None


def test_plan_requires_force_to_replace(store, project):
    with pytest.raises(wf.WorkflowError):
        wf.plan_story(store, project, MockStoryPlanner())
    scenes = wf.plan_story(store, project, MockStoryPlanner(), force=True)
    assert [s.scene_id for s in scenes] == ["s04", "s05", "s06"]
    assert list((store.project_dir("demo") / "history").glob("project_*.json"))


def test_parse_story_plan_readable_errors():
    with pytest.raises(PlanFormatError, match="不是合法 JSON"):
        parse_story_plan("{oops")
    with pytest.raises(PlanFormatError, match="scenes.0.image_prompt"):
        parse_story_plan({"scenes": [{"display_text": "x", "audio_mode": "voiceover"}]})
    with pytest.raises(PlanFormatError, match="audio_mode"):
        parse_story_plan({"scenes": [{"display_text": "x", "image_prompt": "y", "audio_mode": "song"}]})


def test_xai_rejects_mock_svg_without_sending(store, project):
    """專案切到 xai 時不退回 mock；SVG 占位圖在送出前就被擋下。"""
    project.settings = ProjectSettings(provider="xai")
    providers = build_providers(project, Settings(xai_api_key="test-key"))
    assert providers.videos.name == "xai"
    approve_all(store, project)
    out = wf.submit_videos(store, project, providers.videos, scene_ids=["s01"])
    assert out[0].action == "error" and "SVG" in out[0].message


def test_cli_mock_flow(tmp_path):
    runner = CliRunner()
    story = tmp_path / "story.txt"
    story.write_text(STORY, encoding="utf-8")
    base = ["--projects-dir", str(tmp_path / "p")]

    def run(*args):
        result = runner.invoke(app, base + list(args))
        assert result.exit_code == 0, result.output
        return result.output

    run("init", str(story), "--id", "cli")
    run("plan", "cli")
    run("images", "cli")
    run("review", "cli")
    assert (tmp_path / "p/cli/preview/index.html").read_text(encoding="utf-8").count("MOCK") >= 1
    assert "尚未確認" in run("videos", "cli")
    run("approve", "cli", "--all")
    assert "submitted" in run("videos", "cli")
    assert "completed" in run("status", "cli", "--poll")
    result = runner.invoke(app, base + ["render", "cli"])
    assert result.exit_code == 1
    assert not list(Path(tmp_path / "p/cli/output").glob("*.mp4"))
