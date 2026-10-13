"""參考圖、創作說明、環境音模式與舊專案相容性。"""

import hashlib
import json

import pytest

from storybook_video import workflow as wf
from storybook_video.models import CONTENT_FIELDS, OPTIONAL_CONTENT_FIELDS, AudioMode, Character, PlannedScene, Scene, StoryPlan
from storybook_video.providers.base import ImageGenerator, ImageResult, PlanRequest, StoryPlanner
from storybook_video.providers.xai import XAIStoryPlanner
from storybook_video.models import ProjectSettings
from storybook_video.storage import ProjectStore

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8


class RecordingImages(ImageGenerator):
    name = "rec"

    def __init__(self):
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        return ImageResult(data=PNG, suffix=".png", provider=self.name)


class FixedPlanner(StoryPlanner):
    name = "fixed"

    def __init__(self):
        self.request = None

    def plan(self, request):
        self.request = request
        return StoryPlan(scenes=[
            PlannedScene(display_text="海", image_prompt="海邊", audio_mode=AudioMode.ambient,
                         sound_prompt="靜止的海、微風", characters=["少女", "玻璃瓶"]),
            PlannedScene(display_text="燈", image_prompt="街角", characters=["修燈人"]),
        ])


CHARS = [
    Character(name="少女", kind="character", description="黑色齊肩髮"),
    Character(name="修燈人", kind="character", description="灰色工作外套"),
    Character(name="玻璃瓶", kind="object", description="透明圓柱瓶"),
    Character(name="畫風", kind="style", reference_prompt="深藍水彩夜景"),
]


@pytest.fixture
def setup(tmp_path):
    store = ProjectStore(tmp_path / "projects")
    p = wf.init_project(store, "g", "# 玻璃海\n\n故事", provider="mock",
                        characters=[c.model_copy(deep=True) for c in CHARS], brief="七幕骨架")
    return store, p


def test_old_fingerprint_unchanged_when_new_fields_empty():
    s = Scene(scene_id="s01", order=1, display_text="a", image_prompt="b")
    old_fields = [f for f in CONTENT_FIELDS if f not in OPTIONAL_CONTENT_FIELDS]
    raw = json.dumps(s.model_dump(mode="json", include=set(old_fields)), sort_keys=True, ensure_ascii=False)
    assert s.compute_fingerprint() == hashlib.sha256(raw.encode()).hexdigest()[:16]
    s2 = s.model_copy(update={"sound_prompt": "雨"})
    assert s2.compute_fingerprint() != s.compute_fingerprint()


def test_references_style_first_and_used_as_input(setup):
    store, p = setup
    gen = RecordingImages()
    out = wf.generate_references(store, p, gen)
    assert [r.caption for r in gen.requests][0] == "畫風"
    assert gen.requests[0].reference_paths == []
    style_path = p.characters[3].reference_paths[0]
    for req in gen.requests[1:]:
        assert [str(x) for x in req.reference_paths] == [str(store.resolve("g", style_path))]
        assert "畫風" in req.reference_labels[0]
    assert all(c.reference_paths for c in p.characters)
    assert {o.action for o in out} == {"generated"}
    # 再跑一次會跳過
    assert {o.action for o in wf.generate_references(store, p, gen)} == {"skipped"}


def test_plan_uses_brief_and_assigns_refs(setup):
    store, p = setup
    wf.generate_references(store, p, RecordingImages())
    planner = FixedPlanner()
    scenes = wf.plan_story(store, p, planner)
    assert planner.request.brief == "七幕骨架"
    s1, s2 = scenes
    assert s1.audio_mode == AudioMode.ambient and s1.sound_prompt == "靜止的海、微風"
    by = {c.name: c.reference_paths[0] for c in p.characters}
    assert s1.character_reference_paths == [by["畫風"], by["少女"], by["玻璃瓶"]]  # 畫風在前
    assert s2.character_reference_paths == [by["畫風"], by["修燈人"]]
    labels = wf.reference_labels(p, s1.character_reference_paths)
    assert "畫風" in labels[0] and "少女" in labels[1] and "玻璃瓶" in labels[2]
    assert not any(s.is_approved for s in scenes)


def test_regenerating_reference_invalidates_only_affected_scenes(setup):
    store, p = setup
    gen = RecordingImages()
    wf.generate_references(store, p, gen)
    wf.plan_story(store, p, FixedPlanner())
    wf.generate_images(store, p, gen)
    wf.approve_scenes(store, p, approve_all=True)
    out = wf.generate_references(store, p, gen, names=["修燈人"], force=True)
    assert [o.scene_id for o in out if o.action == "updated"] == ["s02"]
    assert p.get_scene("s01").is_approved and not p.get_scene("s02").is_approved


def test_ambient_video_prompt_and_sound(setup):
    store, p = setup
    wf.plan_story(store, p, FixedPlanner())
    prompt = wf.build_video_prompt(p, p.get_scene("s01"))
    assert "沒有旁白、沒有人聲" in prompt and "聲音設計：靜止的海、微風" in prompt


def test_xai_planner_includes_brief_and_kinds():
    planner = XAIStoryPlanner("k", ProjectSettings(provider="xai", text_model="m"), "https://api.x.ai/v1")
    msgs = planner.build_messages(PlanRequest(story="故事", style="水彩", aspect_ratio="16:9",
                                              characters=CHARS, brief="分鏡骨架：七幕"))
    user = msgs[1]["content"]
    assert "創作說明：\n分鏡骨架：七幕" in user
    assert "玻璃瓶（物件）" in user and "少女（角色）" in user
    assert "畫風（" not in user  # 畫風參考不列為角色
    assert "ambient" in msgs[0]["content"]


def test_reference_inputs_borrow_drawing_style(setup):
    store, p = setup
    gen = RecordingImages()
    wf.generate_references(store, p, gen, names=["畫風", "少女"])
    man = next(c for c in p.characters if c.name == "修燈人")
    man.reference_inputs = ["少女"]
    gen.requests.clear()
    wf.generate_references(store, p, gen, names=["修燈人"])
    req = gen.requests[0]
    assert len(req.reference_paths) == 2
    assert "畫法參考" in req.reference_labels[1] and "不是同一個人" in req.reference_labels[1]


def test_voiceover_prompt_keeps_mouths_closed(setup):
    store, p = setup
    scene = Scene(scene_id="s09", order=9, display_text="x", image_prompt="y",
                  audio_mode=AudioMode.voiceover, narration_or_dialogue="旁白")
    prompt = wf.build_video_prompt(p, scene)
    assert "畫面中的人物不開口、不說話" in prompt and "聲音只來自畫外旁白" in prompt
