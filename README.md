# build_video：有聲故事書影片製作

把一段故事做成有聲故事書影片：

```
故事文字 → LLM 拆分鏡與講稿 → 每幕生成圖片 → 人工查看／修改／確認
        → 用確認過的圖片生成有聲影片（Grok 影片內建聲音）→ 查看 → 依序合成完整影片
```

使用雲端模型 API（主要是 xAI Grok），不自架模型。**第一版完成 CLI、檔案管理與 mock 流程**；xAI 的實際串接位置已預留，但還沒實作。

## 安裝

需要 Python 3.11 以上。合成完整影片時另外需要 FFmpeg（mock 流程用不到）：`sudo apt-get install ffmpeg`（Ubuntu／Codespaces）或 `brew install ffmpeg`（macOS）。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env      # 真實 API 才需要填 XAI_API_KEY
storybook --help
```

## 用 mock 跑一次（離線、不需 API key）

範例故事：[examples/little_lantern/story.txt](examples/little_lantern/story.txt)，繁體中文，共三幕。

```bash
# 1. 建立專案（provider 寫入專案設定，後續指令不用再指定）
storybook init examples/little_lantern/story.txt --id lantern \
  --style "溫暖水彩繪本風，柔和光線" --aspect-ratio 16:9 \
  --characters examples/little_lantern/characters.json --provider mock

# 2. 分鏡
storybook plan lantern

# 3. 圖片（先用 --dry-run 看會處理哪些幕）
storybook images lantern --dry-run
storybook images lantern

# 4. 預覽：用瀏覽器開啟輸出的 projects/lantern/preview/index.html
storybook review lantern

# 5. 確認（逐幕確認，或明確使用 --all）
storybook approve lantern --scene s01 --scene s02 --scene s03
#   或 storybook approve lantern --all

# 6. 提交影片任務（mock 只模擬任務狀態）
storybook videos lantern --dry-run
storybook videos lantern

# 7. 查看狀態；--poll 會查詢任務，完成後保存結果
storybook status lantern
storybook status lantern --poll

# 8. 合成：mock 結果不是真實影片，這一步會列出原因並停止，不會產生假 MP4
storybook render lantern
```

## 指令

全域選項：`--projects-dir PATH`（預設是 `$STORYBOOK_PROJECTS_DIR`；沒設定時用 `./projects`），要寫在子指令前面。

| 指令 | 說明 | 常用選項 |
|---|---|---|
| `init STORY_FILE --id ID` | 用故事檔建立專案；角色參考圖會複製到專案內 | `--style` `--aspect-ratio` `--provider mock\|xai` `--characters FILE` `--brief FILE` |
| `refs ID` | 產生角色、物件與畫風參考圖；畫風先做，其餘以它為輸入 | `-n/--name`（可重複）`--force` `--dry-run` |
| `plan ID` | 產生分鏡與講稿；已有分鏡時要加 `--force`，舊分鏡會備份到 `history/` | `--force` `--dry-run` |
| `images ID` | 生成圖片；已有圖片的幕會跳過 | `-s/--scene`（可重複）`--force` `--dry-run` |
| `edit ID -s SCENE` | 修改一幕；內容有變就讓該幕 revision 加一，並使該幕確認失效 | `--display-text` `--narration` `--audio-mode` `--sound-prompt` `--image-prompt` `--motion-prompt` `--duration` `--character-ref` |
| `review ID` | 產生靜態 HTML 預覽頁，顯示圖片、台詞、動作提示、時長與確認狀態 | |
| `approve ID` | 確認目前 revision；必須指定 `--scene` 或 `--all` | `-s` `--all` `--revoke` |
| `videos ID` | 為已確認的幕提交影片任務 | `-s` `--force` `--dry-run` |
| `status ID` | 列出每幕的 revision、圖片、確認、任務與影片狀態 | `--poll` `-s` |
| `trim ID -s SCENE` | 設定合成時該幕只用前 N 秒（剪輯用；不重新生成、不影響確認） | `--seconds N` `--clear` |
| `render ID` | 檢查條件後，用 FFmpeg 依序合成當前版本影片，可加背景音樂與環境音 | `--bgm FILE` `--bgm-volume 0.18` `--ambience FILE` `--ambience-volume 0.5` `--voice-volume 1.0` `--dry-run` |
| `publish ID...` | 輸出公開展示網站（只含成品） | `--out site` |

也可以用 `python -m storybook_video` 代替 `storybook`。

## 執行規則

- **revision**：每幕都有內容版本。改到圖片、頁面文字、旁白／台詞、聲音模式、圖片或動作提示、時長、角色參考時，revision 會加一。直接手改 `project.json` 也會被偵測到（比對內容指紋），一樣讓 revision 加一。
- **確認**：只綁定某一個 revision。內容改了以後，只有那一幕的確認失效，其他幕維持原狀。
- **影片提交**：只送出當前 revision 已確認的幕，就算加 `--force` 也一樣。
- **重跑**：已有當前版本影片的幕會跳過；要重做必須加 `--force`。
- **pending 任務**：一律先查詢，不重複提交（`--force` 也不會重送）。
- **結果不明**：送出時逾時或斷線，或程式在送出途中中斷，任務會記為 `unknown`，不會自動重送，避免重複計費。請先到供應商後台確認，再用 `--force` 重送。
- **舊影片**：內容改了以後，舊影片仍然保留，但會標示為「僅舊版」，不能拿來合成。
- **合成前檢查**：每一幕都必須已確認、有當前版本影片、不是 mock、檔案存在，而且系統要有 FFmpeg。任一條件不符，`render` 就會列出原因並停止。

## 資料存放

每次製作的內容都放在 `projects/<project_id>/`。整個資料夾都已加入 `.gitignore`，預設不提交。

```
projects/lantern/
  project.json                 製作資料（原子寫入；素材一律記錄相對路徑，整個資料夾可以直接搬移）
  story.txt                    原始故事
  assets/images/s01/v001.svg   圖片版本，不覆蓋舊檔
  assets/videos/s01/v001.mp4   影片版本（mock 為 v001.mock.json 說明檔）
  assets/characters/           角色參考圖
  preview/index.html           分鏡預覽
  output/story_v001.mp4        完整合成影片（也做版本管理）
  history/                     重新分鏡前的 project.json 備份
  logs/generation.jsonl        每次生成、提交、查詢與修改的紀錄（只追加，不改寫）
```

每幕的主要欄位：`scene_id`（不會因修改或重新排序而改變）、`order`、`display_text`、`narration_or_dialogue`、`audio_mode`（voiceover/dialogue/none）、`image_prompt`、`motion_prompt`、`duration_seconds`、`character_reference_paths`、`image_path`、`video_path`／`video_revision`、`revision`、`approval`（是否確認、確認的 revision）、`generation_job`（任務 ID、狀態、錯誤），以及 `image_history`／`video_history`／`job_history`。

## mock 與真實 API 的差異

| | mock | xai |
|---|---|---|
| API key | 不需要 | 需要 `XAI_API_KEY`（console.x.ai 申請，`xai-` 開頭） |
| 分鏡 | 依段落機械切分；有「」台詞且出現角色名的段落設為 dialogue | `POST /chat/completions` 結構化輸出（`XAI_TEXT_MODEL`，例如 `grok-4.7`），用 `StoryPlan` schema 驗證 |
| 圖片 | SVG 占位圖，畫面上標有「MOCK 占位圖／非真實生成圖片」 | `POST /images/generations`；有角色參考圖時用 `/images/edits` |
| 影片 | 只模擬 submit → pending → succeeded 的任務流程，結果是 `.mock.json` 說明檔，沒有影像也沒有聲音 | `POST /videos/generations`（image-to-video，本機圖片以 base64 送出），內建聲音生成旁白／台詞 |
| 合成 | 會拒絕合成 | 拿到真實 MP4 並安裝 FFmpeg 後可以合成 |

專案設成 `xai` 時，任何錯誤都會直接回報，**不會偷偷改用 mock**。mock 的 SVG 占位圖不能拿去生成真實影片。

### xAI 實測紀錄（2026-10-13，範例故事）

- 分鏡：`grok-4.7`，約 75 秒；三幕時長 12–14 秒。
- 圖片：`grok-imagine-image-2.0`，1280×720 JPEG，每張 US$0.04，三幕畫風與角色一致。
- 影片：`grok-imagine-video-1.5-lite`、720p，每幕約 2 分鐘完成。xAI 回報費用：12 秒 **US$0.37**、14 秒 **US$0.43**（高於定價表的每秒 $0.02）。輸出為 H.264 + AAC 立體聲，有中文旁白，沒有背景音樂。
- 旁白聲音每幕由模型決定，**不同幕可能是不同聲音**。要固定同一個說書人，需要改用 `grok-imagine-video-1.5` 的 `reference_audios` 預設聲音（尚未實作）。

### 費用與重送

- 影片提交時，連線沒建立或 HTTP 4xx 判定為「確定沒送出」；逾時、斷線或 HTTP 5xx 判定為「結果不明」，記為 `unknown`，不會自動重送。
- 查詢任務時，會把 xAI 回報的費用（`cost_usd`）寫進 `logs/generation.jsonl`。
- 圖片和分鏡是同步請求，逾時後重跑可能重複計費（每張 US$0.04）。

## 角色、物件與畫風參考

`--characters` 的 JSON 可以同時描述角色、固定物件與畫風（範例見 [characters.json](examples/little_lantern/characters.json)）：

```json
[
  {"name": "畫風", "kind": "style", "reference_prompt": "水彩夜景基調…"},
  {"name": "少女", "kind": "character", "description": "黑色齊肩髮…", "voice": "輕、低的年輕女聲",
   "reference_prompt": "全身站姿與臉部特寫並排…"},
  {"name": "修燈人", "kind": "character", "description": "…", "reference_inputs": ["少女"]},
  {"name": "玻璃瓶", "kind": "object", "description": "透明圓柱玻璃瓶…", "reference_prompt": "…"}
]
```

- `storybook refs ID` 依序產生參考圖，存在 `assets/references/<名稱>/v001.jpg`。畫風先做，角色與物件以畫風參考圖為輸入。
- `reference_inputs`：額外借用其他角色參考圖的畫法，用來對齊畫風。送出時會標註「只沿用畫法，不是同一個人」。
- 分鏡時，每幕的 `characters` 會決定要帶哪些參考圖：畫風參考每幕都帶，再加上出場的角色與物件，最多 5 張。生成圖片時會告訴模型每張參考圖代表什麼。
- 已有分鏡後重做某個參考圖，只有用到它的幕會升 revision、確認失效。

## 創作說明（brief）

`init --brief brief.md` 可以提供情緒方向、視覺設定和分鏡骨架（幕數與順序）。分鏡時會一併交給 LLM，並以它為最高優先。

每幕除了旁白／台詞，還有：
- `sound_prompt`：聲音設計，例如海浪、風、腳步、雨。
- `audio_mode=ambient`：只有環境音、沒有人聲。用來保留不說話的鏡頭；和 `none`（完全無聲）不同。

## 設定

- 環境變數，或寫在 `.env`：`XAI_API_KEY`、`STORYBOOK_PROJECTS_DIR`、`XAI_TEXT_MODEL`、`XAI_IMAGE_MODEL`、`XAI_VIDEO_MODEL`、`XAI_VIDEO_RESOLUTION`。設定後會覆蓋專案的設定值，但不會寫回 `project.json`。
- 專案層級的設定在 `project.json` 的 `settings`：
  - `provider`、`text_model`、`image_model`、`video_model`、`video_resolution`（480p／720p／1080p）、`default_duration_seconds`、`language`
  - `narration_style`：旁白語氣描述，會寫進影片提示，例如「溫暖的女性說書人，像睡前講童話……」
  - `dialogue_style`：台詞語氣描述，預設「像童話動畫一樣生動有感情」
- 角色設定（`--characters` JSON）可以加 `voice`，描述角色說話的聲音，用於對話幕。範例見 [characters.json](examples/little_lantern/characters.json)。
- 只想改模型時，可以直接改 `project.json` 的 `settings.video_model`，例如改成較便宜的 `grok-imagine-video-1.5-lite`。

## 查看預覽與影片

預覽頁和素材都是本機檔案。在 Codespaces 等遠端環境，可以在專案資料夾啟動簡單的網頁伺服器，再用轉發的連接埠開啟：

```bash
cd projects/lantern
python -m http.server 8765 --bind 127.0.0.1
# 瀏覽 /preview/index.html，或直接開 /assets/videos/s01/v001.mp4
```

## 合成與背景音樂

```bash
# 產生一段原創音樂盒搖籃曲（示範用，可換成任何你有權使用的音樂檔）
python scripts/make_demo_bgm.py projects/lantern-xai/assets/music/musicbox.wav

storybook render lantern-xai                      # 無背景音樂
storybook render lantern-xai --bgm projects/lantern-xai/assets/music/musicbox.wav --bgm-volume 0.18

# 環境音底（原創合成：sea＝海浪＋風，rain＝雨＋風，breeze＝微風＋樹葉）
python scripts/make_ambience.py projects/glass-sea/assets/music/ambience_sea.m4a --preset sea --duration 120
storybook trim glass-sea -s s03 --seconds 6.5    # 沒講話的幕可以剪短
storybook render glass-sea --bgm projects/glass-sea/assets/music/musicbox.wav --bgm-volume 0.15 \
  --ambience projects/glass-sea/assets/music/ambience_sea.m4a --ambience-volume 0.5
```

- 各幕先統一成第一幕的解析度、24fps、48kHz 立體聲，再依 `order` 接起來；沒有音軌的幕會補靜音。
- 整支影片頭尾各有 0.5 秒淡入、1 秒淡出。
- 背景音樂會循環鋪滿全片、淡入淡出，有人聲時用 sidechain 壓縮自動壓低。整本用同一首曲子，換幕時不會突然換曲。
- 環境音底（`--ambience`）和背景音樂分開調整音量，有人聲時只稍微讓位。用來補足影片模型在「沒有人聲」的幕只產生極小環境音的問題。
- `trim` 設定的長度只影響合成：原始影片完整保留，確認狀態也不受影響。
- 每次合成都另存新檔（`output/story_v001.mp4`、`v002`…），不覆蓋舊檔；預覽頁上方會顯示最新版。

## 公開展示網站（GitHub Pages）

```bash
storybook publish lantern-xai [其他專案ID ...]    # 輸出到 site/
```

- 網站只放成品：首頁（專案介紹、作品列表）＋每個故事一頁（完整影片、每頁插圖與文字）。
- 不放提示詞、任務 ID、費用、生成紀錄與舊版本；完整影片會重新壓縮成網頁用（約 32 MB → 10 MB）。
- 最新成品必須是用目前確認過的各幕影片合成的，否則會要求先重新 `render`。
- 每次 `publish` 都要列出所有要公開的專案；沒列到的故事會從網站移除。
- `site/` 不提交到 main，而是部署到 `gh-pages` 分支：

```bash
git worktree add /tmp/gh-pages gh-pages          # 第一次：git worktree add --orphan -b gh-pages /tmp/gh-pages
rsync -a --delete --exclude .git site/ /tmp/gh-pages/
cd /tmp/gh-pages && git add -A && git commit -m "Update site" && git push origin gh-pages
```

  GitHub repo 的 Settings → Pages 選擇 `gh-pages` 分支、根目錄 `/`。

## 程式結構

```
src/storybook_video/
  cli.py          Typer 指令
  config.py       環境設定（pydantic-settings）
  models.py       Project / Scene / Approval / GenerationJob / StoryPlan
  workflow.py     分鏡、圖片、修改、確認、影片提交與查詢的規則
  storage.py      原子寫入、版本化素材路徑、生成紀錄
  preview.py      靜態 HTML 預覽
  rendering.py    合成前檢查、FFmpeg 合成
  publish.py      公開展示網站輸出
  providers/
    base.py       StoryPlanner / ImageGenerator / VideoGenerator 介面與錯誤類型
    mock.py       離線 mock
    xai.py        xAI REST adapter（httpx）
scripts/
  make_demo_bgm.py  合成示範用音樂盒背景音樂（只用標準函式庫）
  make_ambience.py  用 FFmpeg 合成原創環境音底（海浪、風、雨）
tests/            pytest（全部離線；xAI 測試用 httpx.MockTransport；合成測試需要 ffmpeg，沒有時自動略過）
examples/         範例故事與角色設定
```

## 測試

```bash
pytest
```

## 目前進度

已完成：

- CLI 全流程，以及檔案、版本、紀錄管理
- revision、確認、任務狀態規則，以及 mock provider
- xAI 分鏡、圖片、影片的實際串接，已用範例故事實測三幕
- 靜態預覽頁（含完整影片播放）、合成前檢查
- FFmpeg 合成：格式統一、淡入淡出、背景音樂與自動壓低，已用真實三幕影片合成 38 秒成品

待實作：

- 固定旁白聲音：`reference_audios` 指定預設聲音（需 `grok-imagine-video-1.5`）
- 換幕轉場（目前是直接接續）
- Slides、GIF、網頁編輯器、多使用者、背景任務佇列

實作新的 xAI 功能前，請先查閱 https://docs.x.ai 的當前文件（REST 參考：`/developers/rest-api-reference/inference/videos`），不要憑記憶猜 API。
