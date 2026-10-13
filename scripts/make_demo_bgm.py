"""合成一段原創的音樂盒搖籃曲（WAV），作為示範背景音樂。

只用 Python 標準函式庫；旋律為本專案原創，無版權問題。
輸出可無縫循環（尾音會繞回開頭），render --bgm 會自動循環鋪滿全片。

用法：
    python scripts/make_demo_bgm.py projects/<專案>/assets/music/musicbox.wav
"""

from __future__ import annotations

import math
import struct
import sys
import wave
from pathlib import Path

RATE = 44100
BPM = 72
BEAT = 60 / BPM

NOTE_INDEX = {"C": 0, "C#": 1, "D": 2, "D#": 3, "E": 4, "F": 5, "F#": 6, "G": 7, "G#": 8, "A": 9, "A#": 10, "B": 11}


def freq(name: str) -> float:
    pitch, octave = name[:-1], int(name[-1])
    midi = 12 * (octave + 1) + NOTE_INDEX[pitch]
    return 440.0 * 2 ** ((midi - 69) / 12)


# 3/4 拍，每小節 (旋律[(音, 拍)], 和弦分解音)
BARS = [
    ([("E5", 1), ("G5", 1), ("C6", 1)], ["C3", "G3", "E4", "G4", "C4", "G3"]),
    ([("B5", 1), ("A5", 1), ("E5", 1)], ["A2", "E3", "C4", "E4", "A3", "E3"]),
    ([("F5", 1), ("A5", 1), ("C6", 1)], ["F2", "C3", "A3", "C4", "F3", "C3"]),
    ([("B5", 1.5), ("A5", 0.5), ("G5", 1)], ["G2", "D3", "B3", "D4", "G3", "D3"]),
    ([("E5", 1), ("G5", 1), ("C6", 1)], ["C3", "G3", "E4", "G4", "C4", "G3"]),
    ([("E6", 1), ("D6", 1), ("C6", 1)], ["A2", "E3", "C4", "E4", "A3", "E3"]),
    ([("A5", 1), ("B5", 1), ("D6", 1)], ["F2", "C3", "A3", "G2", "D3", "B3"]),
    ([("C6", 3)], ["C3", "G3", "E4", "G4", "C4", "G3"]),
]


def add_note(buf: list[float], start: float, f: float, amp: float, decay: float) -> None:
    """音樂盒音色：基音 + 少量泛音，快速起音、指數衰減；超出長度的尾音繞回開頭（可無縫循環）。"""
    n = len(buf)
    s0 = int(start * RATE)
    length = int(decay * 5 * RATE)
    partials = ((1.0, 1.0), (2.0, 0.35), (3.0, 0.12), (4.2, 0.05))
    for i in range(length):
        t = i / RATE
        env = math.exp(-t / decay) * min(1.0, t / 0.004)
        v = sum(a * math.sin(2 * math.pi * f * k * t) for k, a in partials)
        buf[(s0 + i) % n] += amp * env * v


def synthesize() -> list[float]:
    total = sum(sum(d for _, d in melody) for melody, _ in BARS) * BEAT
    buf = [0.0] * int(total * RATE)
    t = 0.0
    for melody, arp in BARS:
        mt = t
        for note, beats in melody:
            add_note(buf, mt, freq(note), 0.32, 0.55)
            mt += beats * BEAT
        for j, note in enumerate(arp):
            add_note(buf, t + j * BEAT / 2, freq(note), 0.16 if j else 0.22, 0.8)
        t += 3 * BEAT
    peak = max(abs(x) for x in buf) or 1.0
    return [x / peak * 0.5 for x in buf]  # 約 -6 dBFS


def write_wav(path: Path, samples: list[float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"".join(struct.pack("<h", int(max(-1, min(1, x)) * 32767)) for x in samples))


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "musicbox.wav")
    samples = synthesize()
    write_wav(out, samples)
    print(f"已輸出 {out}（{len(samples) / RATE:.1f} 秒，可循環）")


if __name__ == "__main__":
    main()
