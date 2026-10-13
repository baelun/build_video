"""xAI (Grok) adapter，依 docs.x.ai REST API reference 實作（2026-10 查閱）。

- 分鏡：POST /chat/completions，response_format=json_schema（strict）→ parse_story_plan() 驗證
- 圖片：POST /images/generations（無參考圖）或 /images/edits（images=[參考圖...]），response_format=b64_json
- 影片：POST /videos/generations（image 為 base64 data URL）→ request_id
        GET  /videos/{request_id} → status: pending / done / failed（另處理 expired）
        done 時下載 video.url（暫時網址，需盡快下載）

費用相關的錯誤分類（影片提交）：
- 連線根本沒建立（ConnectError/ConnectTimeout）或 HTTP 4xx → 確定沒有建立任務 → ProviderError
- 已送出但讀取逾時、連線中斷、HTTP 5xx → 任務可能已建立 → SubmissionUncertainError（不自動重送）
"""

from __future__ import annotations

import base64
import copy
import mimetypes
from pathlib import Path
from typing import Any

import httpx

from ..models import AudioMode, JobStatus, ProjectSettings, StoryPlan
from .base import (
    ImageGenerator,
    ImageRequest,
    ImageResult,
    PlanRequest,
    ProviderError,
    StoryPlanner,
    SubmissionUncertainError,
    VideoGenerator,
    VideoJobState,
    VideoRequest,
    VideoResult,
    VideoSubmission,
    parse_story_plan,
)

VIDEO_MAX_DURATION_SECONDS = 15
VIDEO_ASPECT_RATIOS = {"1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "21:9", "5:2"}
VIDEO_RESOLUTIONS = {"480p", "720p", "1080p"}
IMAGE_ASPECT_RATIOS = VIDEO_ASPECT_RATIOS | {"9:19.5", "19.5:9", "9:20", "20:9", "1:2", "2:1", "auto"}
INPUT_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
USD_TICKS = 10_000_000_000  # 1 USD = 1e10 ticks


class _XAIBase:
    name = "xai"

    def __init__(
        self,
        api_key: str | None,
        settings: ProjectSettings,
        base_url: str,
        transport: httpx.BaseTransport | None = None,
    ):
        if not api_key:
            raise ProviderError("缺少 XAI_API_KEY 環境變數（可寫在 .env）。mock 模式不需要 key。")
        self.settings = settings
        self._transport = transport
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(60.0, connect=15.0),
            transport=transport,
        )

    def _post(self, path: str, body: dict, *, timeout: float = 60.0) -> dict:
        try:
            r = self._client.post(path, json=body, timeout=timeout)
        except httpx.HTTPError as e:
            raise ProviderError(f"xAI 連線錯誤（{type(e).__name__}）：{e}") from e
        return _json_or_raise(r)

    def _get(self, path: str) -> httpx.Response:
        try:
            return self._client.get(path)
        except httpx.HTTPError as e:
            raise ProviderError(f"xAI 連線錯誤（{type(e).__name__}）：{e}") from e


def _error_text(r: httpx.Response) -> str:
    try:
        data = r.json()
        msg = data.get("error") or data.get("message") or data
        if isinstance(msg, dict):
            msg = msg.get("message") or msg
        return f"HTTP {r.status_code}：{msg}"
    except ValueError:
        return f"HTTP {r.status_code}：{r.text[:300]}"


def _json_or_raise(r: httpx.Response) -> dict:
    if r.status_code >= 400:
        raise ProviderError(_error_text(r))
    try:
        return r.json()
    except ValueError as e:
        raise ProviderError(f"xAI 回應不是 JSON：{r.text[:200]}") from e


def _data_url(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0]
    if mime not in INPUT_IMAGE_TYPES:
        raise ProviderError(
            f"xAI 只接受 JPEG/PNG/WebP 圖片，{path.name} 是 {mime or '未知格式'}"
            "（mock 的 SVG 占位圖不能拿去生成真實影片，請先用 xai 生成圖片）"
        )
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def _cost_usd(data: dict) -> float | None:
    ticks = (data.get("usage") or {}).get("cost_in_usd_ticks")
    return ticks / USD_TICKS if isinstance(ticks, int) else None


# ---- 分鏡 ----

PLAN_SYSTEM_PROMPT = """你是有聲故事書的分鏡師。把使用者的故事拆成數幕，輸出符合 JSON Schema 的物件。
規則：
- 若有「創作說明」，以它為最高優先：情緒方向、視覺設定、分鏡骨架（幕數與順序）都必須遵守；沒有骨架時依故事自然段落分幕，不增刪情節。
- display_text：該頁顯示的{language}文字；創作說明要求短句時就寫短句。
- narration_or_dialogue：影片中要說出的{language}旁白或台詞。旁白用 voiceover；角色開口說話用 dialogue（寫成「角色名：台詞」）；只有環境音、沒有人聲用 ambient（此欄留空）；完全無聲用 none。
- image_prompt：描述單一靜態畫面，含畫風、場景、構圖、光線、角色外觀與動作；畫面中不要出現任何文字。
- motion_prompt：描述畫面中的動作與鏡頭運動。
- sound_prompt：聲音設計，例如海浪、風、腳步、雨、環境聲，以及必要的短句台詞。
- duration_seconds：整數，介於 4 到 {max_duration}；有旁白或台詞時要足夠說完。
- characters：本幕出場的角色與固定物件名稱，只能使用「角色與物件」清單中的名稱。"""


def _strict_schema(schema: dict) -> dict:
    """移除 xAI 文件未列支援的關鍵字（default、title），其餘保留。"""
    s = copy.deepcopy(schema)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            node.pop("default", None)
            node.pop("title", None)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(s)
    return s


class XAIStoryPlanner(_XAIBase, StoryPlanner):
    def build_messages(self, request: PlanRequest) -> list[dict]:
        system = PLAN_SYSTEM_PROMPT.format(
            language=request.language, max_duration=VIDEO_MAX_DURATION_SECONDS
        )
        kinds = {"character": "角色", "object": "物件"}
        chars = "\n".join(
            f"- {c.name}（{kinds[c.kind]}）：{c.description}" for c in request.characters if c.kind != "style"
        ) or "（未指定，請自行整理）"
        user = (
            f"畫風：{request.style or '（未指定，請建議一種適合兒童繪本的畫風）'}\n"
            f"畫面比例：{request.aspect_ratio}\n角色與物件：\n{chars}\n\n"
        )
        if request.brief:
            user += f"創作說明：\n{request.brief}\n\n"
        user += f"故事：\n{request.story}"
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    def response_schema(self) -> dict:
        return _strict_schema(StoryPlan.model_json_schema())

    def plan(self, request: PlanRequest) -> StoryPlan:
        if not self.settings.text_model:
            raise ProviderError("未設定分鏡用文字模型：請設定 XAI_TEXT_MODEL（名稱請查 docs.x.ai）。")
        body = {
            "model": self.settings.text_model,
            "messages": self.build_messages(request),
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "story_plan", "schema": self.response_schema(), "strict": True},
            },
        }
        data = self._post("/chat/completions", body, timeout=300.0)
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise ProviderError(f"xAI 分鏡回應缺少 choices[0].message.content：{str(data)[:300]}") from e
        plan = parse_story_plan(content)
        for s in plan.scenes:
            if s.duration_seconds > VIDEO_MAX_DURATION_SECONDS:
                s.duration_seconds = VIDEO_MAX_DURATION_SECONDS
        return plan


# ---- 圖片 ----


class XAIImageGenerator(_XAIBase, ImageGenerator):
    def generate(self, request: ImageRequest) -> ImageResult:
        if request.aspect_ratio not in IMAGE_ASPECT_RATIOS:
            raise ProviderError(f"xAI 圖片不支援比例 {request.aspect_ratio}；可用：{sorted(IMAGE_ASPECT_RATIOS)}")
        body: dict[str, Any] = {
            "model": self.settings.image_model,
            "prompt": request.prompt,
            "aspect_ratio": request.aspect_ratio,
            "n": 1,
            "response_format": "b64_json",
        }
        path = "/images/generations"
        if request.reference_paths:
            # 角色參考圖：多圖編輯，prompt 中以 <IMAGE_i> 指稱
            refs = request.reference_paths[:5]
            body["images"] = [{"url": _data_url(p)} for p in refs]
            labels = request.reference_labels or ["角色外觀參考"] * len(refs)
            legend = "\n".join(f"<IMAGE_{i}>：{labels[i] if i < len(labels) else '參考圖'}" for i in range(len(refs)))
            body["prompt"] = f"參考圖：\n{legend}\n\n依參考圖畫出新的場景：\n{request.prompt}"
            path = "/images/edits"
        data = self._post(path, body, timeout=180.0)
        try:
            item = data["data"][0]
            raw = base64.b64decode(item["b64_json"])
        except (KeyError, IndexError, TypeError, ValueError) as e:
            raise ProviderError(f"xAI 圖片回應格式不符：{str(data)[:300]}") from e
        mime = item.get("mime_type") or "image/jpeg"
        suffix = mimetypes.guess_extension(mime) or ".jpg"
        if suffix == ".jpe":
            suffix = ".jpg"
        return ImageResult(data=raw, suffix=suffix, provider=self.name, model=self.settings.image_model)


# ---- 影片 ----


class XAIVideoGenerator(_XAIBase, VideoGenerator):
    def build_body(self, request: VideoRequest) -> dict:
        duration = round(request.duration_seconds)
        if not 1 <= duration <= VIDEO_MAX_DURATION_SECONDS:
            raise ProviderError(f"影片時長需在 1–{VIDEO_MAX_DURATION_SECONDS} 秒，收到 {request.duration_seconds}")
        resolution = request.resolution
        if resolution not in VIDEO_RESOLUTIONS:
            raise ProviderError(f"解析度需為 {sorted(VIDEO_RESOLUTIONS)}，收到 {resolution}")
        body: dict[str, Any] = {
            "model": request.model or self.settings.video_model,
            "prompt": request.prompt,
            # image-to-video：輸出比例跟隨輸入圖片，不另傳 aspect_ratio
            "image": {"url": _data_url(request.image_path)},
            "duration": duration,
            "resolution": resolution,
        }
        if request.audio_mode == AudioMode.none:
            body["generate_audio"] = False
        return body

    def submit(self, request: VideoRequest) -> VideoSubmission:
        body = self.build_body(request)  # 參數錯誤在送出前就擋下
        try:
            r = self._client.post("/videos/generations", json=body, timeout=120.0)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            raise ProviderError(f"無法連線到 xAI，請求未送出：{e}") from e
        except httpx.HTTPError as e:
            raise SubmissionUncertainError(f"送出後未收到回應（{type(e).__name__}），任務可能已建立") from e
        if r.status_code >= 500:
            raise SubmissionUncertainError(f"{_error_text(r)}；任務可能已建立")
        data = _json_or_raise(r)
        job_id = data.get("request_id")
        if not job_id:
            raise SubmissionUncertainError(f"回應缺少 request_id：{str(data)[:300]}")
        return VideoSubmission(job_id=job_id, provider=self.name, model=body["model"])

    def _status(self, job_id: str) -> dict:
        r = self._get(f"/videos/{job_id}")
        if r.status_code == 202:  # 部分 deferred 端點以 202 表示處理中
            return {"status": "pending"}
        return _json_or_raise(r)

    def poll(self, job_id: str) -> VideoJobState:
        data = self._status(job_id)
        status = data.get("status")
        cost = _cost_usd(data)
        if status == "done":
            video = data.get("video") or {}
            if not video.get("url"):
                if video.get("respect_moderation") is False:
                    return VideoJobState(JobStatus.failed, "影片未通過內容審核，xAI 未提供檔案", cost)
                return VideoJobState(JobStatus.failed, "狀態為 done 但沒有 video.url", cost)
            return VideoJobState(JobStatus.succeeded, None, cost)
        if status in ("failed", "expired"):
            err = data.get("error") or {}
            msg = f"{err.get('code', status)}：{err.get('message', '')}".strip("：")
            return VideoJobState(JobStatus.failed, msg, cost)
        return VideoJobState(JobStatus.pending, None, cost, progress=data.get("progress"))

    def fetch_result(self, job_id: str) -> VideoResult:
        data = self._status(job_id)
        url = (data.get("video") or {}).get("url")
        if data.get("status") != "done" or not url:
            raise ProviderError(f"任務 {job_id} 尚無可下載的影片（status={data.get('status')}）")
        try:
            with httpx.Client(
                timeout=httpx.Timeout(300.0, connect=15.0), follow_redirects=True, transport=self._transport
            ) as dl:
                r = dl.get(url)  # 暫時網址，不帶 API key
        except httpx.HTTPError as e:
            raise ProviderError(f"下載影片失敗（{type(e).__name__}）：{e}") from e
        if r.status_code != 200:
            raise ProviderError(f"下載影片失敗：HTTP {r.status_code}")
        if b"ftyp" not in r.content[:64]:
            raise ProviderError("下載內容不像 MP4 檔，已拒絕保存")
        return VideoResult(data=r.content, suffix=".mp4")
