"""用 FFmpeg 合成原創環境音底（海浪、風、雨），作為 render --ambience 的素材。

全部由噪音產生器加濾波與緩慢的音量起伏做成，沒有取樣任何既有錄音，可自由使用。
左右聲道使用不同的隨機種子，聽起來比較寬。

用法：
    python scripts/make_ambience.py projects/<專案>/assets/music/ambience_sea.m4a --preset sea --duration 120
    python scripts/make_ambience.py out.m4a --preset rain
    python scripts/make_ambience.py out.m4a --preset breeze
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
from pathlib import Path

TARGET_MEAN_DB = -26.0

PRESETS = {
    # 海浪：低頻棕噪音，約 9 秒一次湧起退去；風：粉紅噪音帶通，緩慢起伏
    "sea": [
        ("brown", "lowpass=f=650,highpass=f=40",
         "0.18+0.82*pow(max(0,sin(2*PI*t/9.3)),2)*(0.75+0.25*sin(2*PI*t/37))", 1.0),
        ("pink", "bandpass=f=520:width_type=o:w=1.6",
         "0.35+0.2*sin(2*PI*t/17)+0.1*sin(2*PI*t/6.1)", 0.35),
    ],
    # 微風：柔和的風聲起伏＋細碎的樹葉沙沙聲，適合森林與四季場景
    "breeze": [
        ("pink", "bandpass=f=420:width_type=o:w=1.4",
         "0.35+0.3*pow(max(0,sin(2*PI*t/11.7)),2)+0.12*sin(2*PI*t/4.3)", 0.7),
        ("white", "highpass=f=2500,lowpass=f=7000",
         "0.08+0.22*pow(max(0,sin(2*PI*t/11.7+0.6)),3)", 0.3),
    ],
    # 雨：高頻白噪音（雨絲）＋低頻粉紅噪音（遠處雨幕）＋少量風
    "rain": [
        ("white", "highpass=f=1800,lowpass=f=9000", "0.55+0.1*sin(2*PI*t/11)", 0.45),
        ("pink", "lowpass=f=900,highpass=f=80", "0.6", 0.5),
        ("pink", "bandpass=f=520:width_type=o:w=1.6", "0.3+0.2*sin(2*PI*t/19)", 0.25),
    ],
}


def build_graph(preset: str, duration: float, gain_db: float = 0.0) -> str:
    layers = PRESETS[preset]
    parts = []
    for ch in ("L", "R"):
        names = []
        for i, (color, filters, vol, _) in enumerate(layers):
            seed = (1 if ch == "L" else 101) + i * 7
            name = f"{ch}{i}"
            parts.append(
                f"anoisesrc=d={duration}:c={color}:a=0.5:seed={seed},{filters},"
                f"volume='{vol}':eval=frame[{name}]"
            )
            names.append(f"[{name}]")
        weights = " ".join(str(w) for *_, w in layers)
        parts.append(f"{''.join(names)}amix=inputs={len(layers)}:weights='{weights}':normalize=0[{ch}]")
    parts.append(f"[L][R]join=inputs=2:channel_layout=stereo,volume={gain_db}dB[out]")
    return ";".join(parts)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("output", type=Path)
    ap.add_argument("--preset", choices=sorted(PRESETS), default="sea")
    ap.add_argument("--duration", type=float, default=120)
    args = ap.parse_args()
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SystemExit("找不到 ffmpeg")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # 先量平均音量，再用固定增益調到目標（不用 loudnorm，避免開頭音量爬升造成循環接縫）
    probe = subprocess.run(
        [ffmpeg, "-hide_banner", "-filter_complex", build_graph(args.preset, args.duration).replace("[out]", "[o];[o]volumedetect"),
         "-f", "null", "-"], capture_output=True, text=True, check=True,
    )
    mean = float(re.search(r"mean_volume: (-?[\d.]+) dB", probe.stderr).group(1))
    gain = TARGET_MEAN_DB - mean
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-filter_complex", build_graph(args.preset, args.duration, gain), "-map", "[out]",
        "-ar", "48000", "-c:a", "aac", "-b:a", "160k", str(args.output),
    ]
    subprocess.run(cmd, check=True)
    print(f"已輸出 {args.output}（{args.preset}，{args.duration:g} 秒）")


if __name__ == "__main__":
    main()
