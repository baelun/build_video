"""本機靜態 HTML 分鏡預覽頁（唯讀；確認操作由 CLI 完成）。"""

from __future__ import annotations

import os
from html import escape
from pathlib import Path

from .models import Project
from .storage import ProjectStore, atomic_write_text
from .workflow import build_video_prompt, scene_status

_CSS = """
:root{--bg:#faf8f4;--card:#fff;--fg:#222;--muted:#666;--line:#e3ded5;--ok:#1d7a3a;--warn:#a35a00;--bad:#b42318}
@media (prefers-color-scheme:dark){:root{--bg:#1b1a18;--card:#262421;--fg:#eee;--muted:#aaa;--line:#3a3733;--ok:#5bc27c;--warn:#f0a44b;--bad:#ff7b6e}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 system-ui,"Noto Sans TC",sans-serif;padding:24px 16px}
main{max-width:1100px;margin:0 auto}
h1{margin:0 0 4px}.meta{color:var(--muted);margin-bottom:20px}
.mock{background:#fff3cd;color:#664d03;border:1px solid #e0c36a;padding:8px 12px;border-radius:6px;margin-bottom:16px}
.scene{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:16px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:16px}
@media (max-width:720px){.scene{grid-template-columns:1fr}}
.scene img{width:100%;border-radius:6px;border:1px solid var(--line)}
.noimg{aspect-ratio:16/9;display:grid;place-items:center;border:1px dashed var(--line);border-radius:6px;color:var(--muted)}
h2{margin:0 0 8px;font-size:18px}
dl{margin:0;display:grid;grid-template-columns:7em 1fr;gap:4px 8px}dt{color:var(--muted)}dd{margin:0;white-space:pre-wrap}
.badge{display:inline-block;padding:1px 8px;border-radius:99px;font-size:13px;border:1px solid currentColor}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
code{background:var(--bg);padding:1px 4px;border-radius:4px}
.final{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:16px}
.final video{width:100%;max-width:960px;border-radius:6px;display:block}
.refs{margin-bottom:16px}.refgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}
.refgrid figure{margin:0;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px}
.refgrid img{width:100%;border-radius:6px;display:block}.refgrid figcaption{font-size:13px;color:var(--muted);margin-top:6px}
.refgrid b{color:var(--fg)}.kind{font-size:12px;border:1px solid var(--line);border-radius:99px;padding:0 6px}
img.thumb{width:72px;height:auto;border-radius:4px;border:1px solid var(--line);vertical-align:middle;margin:0 4px 4px 0}
"""

AUDIO_LABELS = {"voiceover": "旁白", "dialogue": "台詞（對嘴）", "ambient": "只有環境音", "none": "無聲"}
KIND_LABELS = {"character": "角色", "object": "物件", "style": "畫風"}


def _rel_from_preview(project_dir: Path, rel_asset: str) -> str:
    return Path(os.path.relpath(project_dir / rel_asset, project_dir / "preview")).as_posix()


def build_preview(store: ProjectStore, project: Project) -> Path:
    pdir = store.project_dir(project.project_id)
    is_mock = project.settings.provider == "mock"
    def refs_html(s) -> str:
        if not s.character_reference_paths:
            return "—"
        return " ".join(
            f'<img class="thumb" src="{escape(_rel_from_preview(pdir, p))}" alt="" title="{escape(p)}">'
            for p in s.character_reference_paths
        )

    cards = []
    for s in project.ordered_scenes():
        st = scene_status(store, project, s)
        if s.image_path:
            src = escape(_rel_from_preview(pdir, s.image_path))
            img = f'<img src="{src}" alt="{escape(s.scene_id)} 圖片">'
        else:
            img = '<div class="noimg">尚無圖片</div>'
        cls = "ok" if s.is_approved else ("bad" if s.approval.approved else "warn")
        video = escape(st["video"])
        if s.has_current_video and not s.current_video_is_mock:
            video = f'<a href="{escape(_rel_from_preview(pdir, s.video_path))}">{video}</a>'
        cards.append(f"""
<section class="scene" id="{escape(s.scene_id)}">
  <div>{img}</div>
  <div>
    <h2>#{s.order} · {escape(s.scene_id)} <span class="badge {cls}">{escape(st['approval'])}</span></h2>
    <dl>
      <dt>revision</dt><dd>{s.revision}</dd>
      <dt>頁面文字</dt><dd>{escape(s.display_text)}</dd>
      <dt>聲音模式</dt><dd>{escape(AUDIO_LABELS.get(s.audio_mode.value, s.audio_mode.value))}</dd>
      <dt>旁白／台詞</dt><dd>{escape(s.narration_or_dialogue) or '（無人聲）'}</dd>
      <dt>聲音設計</dt><dd>{escape(s.sound_prompt) or '—'}</dd>
      <dt>動作／鏡頭</dt><dd>{escape(s.motion_prompt) or '—'}</dd>
      <dt>預計時長</dt><dd>{s.duration_seconds:g} 秒</dd>
      <dt>角色／物件</dt><dd>{escape('、'.join(s.characters)) or '—'}</dd>
      <dt>參考圖</dt><dd>{refs_html(s)}</dd>
      <dt>圖片提示</dt><dd>{escape(s.image_prompt)}</dd>
      <dt>影片任務</dt><dd>{escape(st['job'])}</dd>
      <dt>影片</dt><dd>{video}</dd>
      <dt>送出的影片提示</dt><dd>{escape(build_video_prompt(project, s))}</dd>
    </dl>
    <p>確認：<code>storybook approve {escape(project.project_id)} --scene {escape(s.scene_id)}</code></p>
  </div>
</section>""")
    banner = (
        '<div class="mock">MOCK 模式：圖片為占位圖，影片任務為模擬，沒有生成任何真實影片或中文旁白。</div>'
        if is_mock
        else ""
    )
    outputs = sorted((pdir / "output").glob("story_v*.mp4"))
    output_html = ""
    if outputs:
        links = " · ".join(
            f'<a href="{escape(_rel_from_preview(pdir, o.relative_to(pdir).as_posix()))}">{escape(o.name)}</a>'
            for o in outputs
        )
        latest = escape(_rel_from_preview(pdir, outputs[-1].relative_to(pdir).as_posix()))
        output_html = (
            f'<section class="final"><h2>完整影片</h2>'
            f'<video controls preload="metadata" src="{latest}"></video><p>所有版本：{links}</p></section>'
        )
    ref_cards = "".join(
        f'<figure><img src="{escape(_rel_from_preview(pdir, c.reference_paths[0]))}" alt="{escape(c.name)}">'
        f'<figcaption><b>{escape(c.name)}</b> <span class="kind">{KIND_LABELS[c.kind]}</span><br>{escape(c.description)}</figcaption></figure>'
        if c.reference_paths else
        f'<figure><div class="noimg">尚無參考圖</div><figcaption><b>{escape(c.name)}</b> '
        f'<span class="kind">{KIND_LABELS[c.kind]}</span><br>{escape(c.description)}</figcaption></figure>'
        for c in project.characters
    )
    refs_section = f'<section class="refs"><h2>角色、物件與畫風參考</h2><div class="refgrid">{ref_cards}</div></section>' if ref_cards else ""
    total = sum(s.duration_seconds for s in project.scenes)
    pending = sum(1 for s in project.scenes if not s.is_approved)
    summary = f'<p class="meta">{len(project.scenes)} 幕 · 預計 {total:g} 秒 · 待確認 {pending} 幕</p>' if project.scenes else ""
    title = escape(project.title or project.project_id)
    html = f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · 分鏡預覽</title><style>{_CSS}</style></head>
<body><main>
<h1>{title}</h1>
<div class="meta">專案 {escape(project.project_id)} · provider {escape(project.settings.provider)} · 比例 {escape(project.aspect_ratio)} · 畫風 {escape(project.style or '—')} · 產生於 {escape(project.updated_at.strftime('%Y-%m-%d %H:%M UTC'))}</div>
{banner}
{summary}
{output_html}
{refs_section}
{''.join(cards) or '<p>尚無分鏡，請先執行 plan。</p>'}
</main></body></html>
"""
    out = pdir / "preview" / "index.html"
    atomic_write_text(out, html)
    return out
