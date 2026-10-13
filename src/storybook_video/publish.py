"""輸出可公開的靜態展示網站（GitHub Pages 等）。

只放成品：完整故事影片（重新壓縮）、每幕當前圖片、頁面文字。
不放提示詞、任務 ID、費用、生成紀錄、舊版本素材。

    site/
      index.html                 專案介紹＋故事列表
      .nojekyll
      stories/<project_id>/
        index.html               故事頁
        story.mp4                壓縮後的完整影片
        pages/s01.jpg ...        每幕圖片
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from pathlib import Path

from .models import Project
from .rendering import find_ffmpeg, probe
from .storage import ProjectStore, atomic_write_text

REPO_URL = "https://github.com/baelun/build_video"


class PublishError(Exception):
    pass


@dataclass
class PublishedStory:
    project_id: str
    title: str
    duration: float
    scene_count: int
    cover: str  # 相對 site 根目錄
    page: str


def latest_render(store: ProjectStore, project: Project) -> Path:
    """取得最新成品，並確認它是用「目前各幕的當前影片」合成的。"""
    pid = project.project_id
    base = store.project_dir(pid)
    outputs = sorted((base / "output").glob("story_v*.mp4"))
    if not outputs:
        raise PublishError(f"{pid}：還沒有完整影片，請先執行 storybook render {pid}")
    latest = outputs[-1]
    rel = latest.relative_to(base).as_posix()
    inputs = None
    log = base / "logs" / "generation.jsonl"
    if log.exists():
        for line in log.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            if rec.get("event") == "render" and rec.get("output") == rel and rec.get("returncode") == 0:
                inputs = rec.get("inputs")
    current = [s.video_path for s in project.ordered_scenes()]
    if inputs is None:
        raise PublishError(f"{pid}：找不到 {rel} 的合成紀錄，無法確認是否為最新內容，請重新 render")
    if inputs != current or not all(s.is_approved and s.has_current_video for s in project.scenes):
        raise PublishError(f"{pid}：{rel} 不是用目前確認過的各幕影片合成的，請重新 render")
    return latest


def _compress(ffmpeg: str, src: Path, dest: Path, stamp: Path) -> None:
    """壓縮成適合網頁播放的 MP4；來源沒變就沿用上次的結果（stamp 放在專案內，不公開）。"""
    key = f"{src.resolve()}|{src.stat().st_size}|{src.stat().st_mtime_ns}"
    if dest.exists() and stamp.exists() and stamp.read_text() == key:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp.mp4")
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
        "-map", "0:v:0", "-map", "0:a:0",
        "-vf", "scale=-2:'min(720,ih)'", "-c:v", "libx264", "-preset", "slow", "-crf", "27",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(tmp),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise PublishError(f"影片壓縮失敗：{proc.stderr[-800:]}")
    tmp.replace(dest)
    stamp.write_text(key)


def publish_site(store: ProjectStore, project_ids: list[str], out_dir: Path) -> list[PublishedStory]:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise PublishError("找不到 ffmpeg，請先安裝（apt install ffmpeg / brew install ffmpeg）")
    if not project_ids:
        raise PublishError("請指定至少一個要發佈的專案")
    stories = []
    out_dir.mkdir(parents=True, exist_ok=True)
    keep = set()
    for pid in project_ids:
        project = store.load(pid)
        src = latest_render(store, project)
        story_dir = out_dir / "stories" / pid
        keep.add(pid)
        pages_dir = story_dir / "pages"
        if pages_dir.exists():
            shutil.rmtree(pages_dir)
        pages_dir.mkdir(parents=True)
        _compress(ffmpeg, src, story_dir / "story.mp4", store.project_dir(pid) / "logs" / "publish_source.txt")
        pages = []
        for s in project.ordered_scenes():
            img = store.resolve(pid, s.image_path)
            name = f"{s.scene_id}{img.suffix}"
            shutil.copy2(img, pages_dir / name)
            pages.append((s, f"pages/{name}"))
        duration = probe(ffmpeg, story_dir / "story.mp4").duration
        title = project.title or pid
        atomic_write_text(story_dir / "index.html", _story_page(project, title, pages, duration))
        stories.append(
            PublishedStory(
                project_id=pid, title=title, duration=duration, scene_count=len(pages),
                cover=f"stories/{pid}/{pages[0][1]}", page=f"stories/{pid}/",
            )
        )
    # 移除這次沒有發佈的舊故事資料夾，網站內容與指定清單一致
    stories_root = out_dir / "stories"
    for d in stories_root.iterdir():
        if d.is_dir() and d.name not in keep:
            shutil.rmtree(d)
    atomic_write_text(out_dir / "index.html", _index_page(stories))
    (out_dir / ".nojekyll").write_text("")
    return stories


# ---- HTML ----

_CSS = """
:root{--bg:#fbf7ef;--paper:#fffdf8;--ink:#2b2622;--muted:#6f655c;--line:#e8dfd0;--accent:#c4472f;--accent-soft:#f6e3d9;--shadow:0 1px 2px rgba(60,40,20,.06),0 8px 24px rgba(60,40,20,.08)}
@media (prefers-color-scheme:dark){:root{--bg:#191614;--paper:#221e1b;--ink:#efe8df;--muted:#a99e92;--line:#3a332d;--accent:#ec7b5f;--accent-soft:#3a2620;--shadow:0 1px 2px rgba(0,0,0,.3),0 8px 24px rgba(0,0,0,.3)}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.75 "Noto Sans TC","PingFang TC","Microsoft JhengHei",system-ui,sans-serif}
a{color:var(--accent)}
.wrap{max-width:1040px;margin:0 auto;padding:0 20px}
header.hero{padding:72px 0 40px}
.eyebrow{color:var(--accent);font-weight:600;letter-spacing:.08em;font-size:14px;margin:0 0 8px}
h1{font-family:"Noto Serif TC","Songti TC",serif;font-size:clamp(30px,5vw,46px);line-height:1.25;margin:0 0 16px}
h2{font-family:"Noto Serif TC","Songti TC",serif;font-size:26px;margin:56px 0 16px}
.lead{font-size:18px;color:var(--muted);max-width:44em;margin:0}
.flow{list-style:none;padding:0;margin:0;display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;counter-reset:step}
.flow li{background:var(--paper);border:1px solid var(--line);border-radius:12px;padding:14px 16px;counter-increment:step}
.flow li::before{content:counter(step);display:inline-grid;place-items:center;width:24px;height:24px;border-radius:50%;background:var(--accent-soft);color:var(--accent);font-size:13px;font-weight:700;margin-right:8px}
.flow b{font-weight:600}.flow span{display:block;color:var(--muted);font-size:14px;margin-top:4px}
.features{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px}
.feature{background:var(--paper);border:1px solid var(--line);border-radius:12px;padding:18px 20px}
.feature h3{margin:0 0 6px;font-size:17px}.feature p{margin:0;color:var(--muted);font-size:15px}
.gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:20px}
.card{display:block;background:var(--paper);border:1px solid var(--line);border-radius:14px;overflow:hidden;text-decoration:none;color:inherit;box-shadow:var(--shadow);transition:transform .15s}
.card:hover{transform:translateY(-3px)}
.card img{width:100%;aspect-ratio:16/9;object-fit:cover;display:block}
.card div{padding:14px 18px}.card h3{margin:0 0 4px;font-size:19px}.card p{margin:0;color:var(--muted);font-size:14px}
footer{margin:72px 0 0;padding:28px 0 48px;border-top:1px solid var(--line);color:var(--muted);font-size:14px}
.back{display:inline-block;margin:28px 0 0;text-decoration:none}
.player{margin:8px 0 0}
.player video{width:100%;border-radius:14px;box-shadow:var(--shadow);background:#000;display:block}
.meta{color:var(--muted);margin:8px 0 0;font-size:15px}
.pages{display:grid;gap:28px;margin-top:8px}
.page{display:grid;grid-template-columns:minmax(0,1.1fr) minmax(0,1fr);gap:24px;align-items:center;background:var(--paper);border:1px solid var(--line);border-radius:14px;padding:16px}
.page img{width:100%;border-radius:10px;display:block}
.page p{margin:0;font-family:"Noto Serif TC","Songti TC",serif;font-size:19px;line-height:1.9}
.page .no{color:var(--accent);font:600 13px/1 system-ui,sans-serif;letter-spacing:.08em;margin-bottom:10px;display:block}
@media (max-width:720px){.page{grid-template-columns:1fr}header.hero{padding:48px 0 28px}}
"""


def _doc(title: str, body: str, root: str) -> str:
    return f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)}</title><style>{_CSS}</style></head>
<body>{body}
<footer><div class="wrap">
圖片與影片（含旁白）由 xAI Grok Imagine 生成；背景音樂為本專案程式合成的原創旋律。
· <a href="{REPO_URL}">原始碼</a> · <a href="{root}">回首頁</a>
· 更新於 {datetime.now(timezone.utc):%Y-%m-%d}
</div></footer></body></html>
"""


def _fmt_duration(sec: float) -> str:
    m, s = divmod(round(sec), 60)
    return f"{m} 分 {s} 秒" if m else f"{s} 秒"


def _story_page(project: Project, title: str, pages: list, duration: float) -> str:
    items = "".join(
        f'<article class="page"><img src="{escape(path)}" alt="第 {i} 頁插圖" loading="lazy">'
        f'<div><span class="no">第 {i} 頁</span><p>{escape(s.display_text)}</p></div></article>'
        for i, (s, path) in enumerate(pages, start=1)
    )
    poster = escape(pages[0][1])
    body = f"""
<main class="wrap">
  <a class="back" href="../../">← 所有故事</a>
  <header class="hero" style="padding-top:20px">
    <p class="eyebrow">有聲故事書</p>
    <h1>{escape(title)}</h1>
    <p class="meta">{len(pages)} 頁 · {_fmt_duration(duration)}{' · ' + escape(project.style) if project.style else ''}</p>
  </header>
  <section class="player"><video controls preload="metadata" playsinline poster="{poster}" src="story.mp4"></video></section>
  <h2>故事內容</h2>
  <section class="pages">{items}</section>
</main>"""
    return _doc(f"{title}｜有聲故事書", body, "../../")


def _index_page(stories: list[PublishedStory]) -> str:
    cards = "".join(
        f'<a class="card" href="{escape(st.page)}"><img src="{escape(st.cover)}" alt="" loading="lazy">'
        f'<div><h3>{escape(st.title)}</h3><p>{st.scene_count} 頁 · {_fmt_duration(st.duration)}</p></div></a>'
        for st in stories
    )
    steps = [
        ("故事文字", "準備一段故事"),
        ("AI 分鏡", "拆成頁面、旁白與畫面描述"),
        ("生成插圖", "每頁一張，畫風一致"),
        ("人工確認", "預覽、修改、逐頁確認"),
        ("有聲影片", "插圖動起來，旁白與台詞同步生成"),
        ("合成成片", "依序接起並加上背景音樂"),
    ]
    flow = "".join(f"<li><b>{a}</b><span>{b}</span></li>" for a, b in steps)
    features = [
        ("確認後才生成影片", "影片只會用人工確認過的圖片與講稿生成；任何修改都會讓該頁需要重新確認，其他頁不受影響。"),
        ("避免重複計費", "已提交的任務先查詢、不重送；結果不明時記錄下來，由人決定是否重做。"),
        ("每個版本都保留", "圖片、影片、成品都另存新版本，不覆蓋舊檔，隨時可以回頭比較。"),
        ("內建聲音", "旁白與角色台詞由影片模型直接生成，角色開口時嘴型同步。"),
    ]
    feats = "".join(f'<div class="feature"><h3>{a}</h3><p>{b}</p></div>' for a, b in features)
    body = f"""
<main class="wrap">
  <header class="hero">
    <p class="eyebrow">STORYBOOK VIDEO</p>
    <h1>把一段故事，<br>做成會說話的繪本影片</h1>
    <p class="lead">一個用雲端 AI 模型製作有聲故事書影片的命令列工具。AI 負責分鏡、插圖與配音動畫，
    每一頁都經過人工查看與確認，最後合成完整影片。</p>
  </header>
  <h2>作品</h2>
  <section class="gallery">{cards}</section>
  <h2>製作流程</h2>
  <ol class="flow">{flow}</ol>
  <h2>設計重點</h2>
  <section class="features">{feats}</section>
  <h2>技術</h2>
  <p>Python 3.11+、Typer（CLI）、Pydantic（資料驗證）、httpx（xAI REST API）、FFmpeg（合成）、pytest。
  模型：Grok 4.7 分鏡、Grok Imagine Image 2.0 插圖、Grok Imagine Video 1.5 Lite 有聲影片。
  詳細說明見 <a href="{REPO_URL}#readme">README</a>。</p>
</main>"""
    return _doc("有聲故事書影片製作", body, "./")
