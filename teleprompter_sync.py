#!/usr/bin/env python3
"""
Teleprompter sync tool for an audio recording.

Features:
- optional cue generation from the audio itself using ffmpeg's PocketSphinx ASR
- terminal teleprompter view with wrapping and optional figlet banners
- audio playback via ffplay
- cue timing based on start timestamps, with a simple offset knob for fine sync

Usage examples:
  python teleprompter_sync.py --audio ending.mp3 --cues ending_cues.json --play
  python teleprompter_sync.py --audio ending.mp3 --generate-cues ending_cues.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


@dataclass
class Cue:
    start: float
    end: float
    text: str

    @property
    def is_pause(self) -> bool:
        return not self.text.strip()


def fmt_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    m = int(seconds // 60)
    s = seconds - m * 60
    return f"{m:02d}:{s:06.3f}"


def get_duration(path: Path) -> float:
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json",
        str(path),
    ]
    out = subprocess.check_output(cmd, text=True)
    return float(json.loads(out)["format"]["duration"])


def run_ffmpeg_asr(audio_path: Path) -> List[tuple[float, str]]:
    """
    Returns a list of (timestamp_seconds, transcript_snippet) pairs.
    ffmpeg prints PocketSphinx ASR metadata as log lines via ametadata.
    """
    hmm = "/usr/share/pocketsphinx/model/en-us/en-us"
    dict_path = "/usr/share/pocketsphinx/model/en-us/cmudict-en-us.dict"
    lm = "/usr/share/pocketsphinx/model/en-us/en-us.lm.bin"

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-i", str(audio_path),
        "-af",
        (
            f"asr=hmm={hmm}:dict={dict_path}:lm={lm},"
            "ametadata=mode=print:key=lavfi.asr.text:file=-"
        ),
        "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    stdout = proc.stdout.strip().splitlines()

    events: List[tuple[float, str]] = []
    current_time: Optional[float] = None

    for line in stdout:
        if line.startswith("frame:"):
            m = re.search(r"pts_time:([0-9.]+)", line)
            if m:
                current_time = float(m.group(1))
        elif line.startswith("lavfi.asr.text="):
            text = line[len("lavfi.asr.text="):]
            next_time = None
            if "frame:" in text:
                text, tail = text.split("frame:", 1)
                text = text.strip()
                m = re.search(r"pts_time:([0-9.]+)", "frame:" + tail)
                if m:
                    next_time = float(m.group(1))
            else:
                text = text.strip()

            if current_time is not None:
                events.append((current_time, text))
            if next_time is not None:
                current_time = next_time

    # Keep only non-empty snippets for cue generation.
    return [(t, txt) for t, txt in events if txt.strip()]


def build_cues(audio_path: Path) -> List[Cue]:
    duration = get_duration(audio_path)
    events = run_ffmpeg_asr(audio_path)
    if not events:
        return [Cue(0.0, duration, "")]

    cues: List[Cue] = []

    # Initial pause until first detected snippet.
    first_t = max(0.0, events[0][0])
    if first_t > 0:
        cues.append(Cue(0.0, first_t, ""))

    for idx, (start, text) in enumerate(events):
        end = events[idx + 1][0] if idx + 1 < len(events) else duration
        cues.append(Cue(start, end, text))

    # Final pause if needed.
    if cues and cues[-1].end < duration:
        cues.append(Cue(cues[-1].end, duration, ""))

    # Normalize monotonicity.
    normalized: List[Cue] = []
    for cue in cues:
        if normalized and cue.start < normalized[-1].end:
            cue = Cue(normalized[-1].end, cue.end, cue.text)
        if cue.end > cue.start:
            normalized.append(cue)

    return normalized


def load_cues(path: Path) -> List[Cue]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [Cue(float(item["start"]), float(item["end"]), str(item.get("text", ""))) for item in raw]


def save_cues(path: Path, cues: List[Cue]) -> None:
    path.write_text(
        json.dumps([cue.__dict__ for cue in cues], indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def render_banner(text: str) -> str:
    figlet = shutil.which("figlet")
    if figlet and text.strip():
        try:
            out = subprocess.check_output([figlet, text], text=True)
            return out.rstrip("\n")
        except Exception:
            pass
    return text


def wrap_block(text: str, width: int) -> str:
    lines: List[str] = []
    for paragraph in text.splitlines() or [""]:
        if not paragraph.strip():
            lines.append("")
        else:
            lines.extend(textwrap.wrap(paragraph, width=max(20, width)) or [""])
    return "\n".join(lines)


def clear_screen() -> None:
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()


def current_cue(cues: List[Cue], t: float) -> int:
    if not cues:
        return -1
    for i, cue in enumerate(cues):
        if cue.start <= t < cue.end:
            return i
    return len(cues) - 1 if t >= cues[-1].end else 0


def play_audio(audio_path: Path, volume: str = "1.0") -> subprocess.Popen:
    ffplay = shutil.which("ffplay")
    if not ffplay:
        raise RuntimeError("ffplay was not found on PATH.")
    return subprocess.Popen(
        [
            ffplay,
            "-nodisp",
            "-autoexit",
            "-loglevel", "quiet",
            "-volume", str(volume),
            str(audio_path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def run_teleprompter(audio_path: Path, cues: List[Cue], play: bool, offset: float, title: str, refresh: float) -> None:
    duration = get_duration(audio_path)

    player = None
    if play:
        player = play_audio(audio_path)

    start_wall = time.monotonic()
    try:
        while True:
            elapsed = time.monotonic() - start_wall + offset

            if elapsed > duration + 0.15:
                break

            idx = current_cue(cues, elapsed)
            cue = cues[idx] if idx >= 0 else Cue(0.0, duration, "")

            cols = shutil.get_terminal_size((100, 30)).columns
            body_width = min(78, max(30, cols - 8))

            clear_screen()
            print("=" * cols)
            print(f"{title}".center(cols))
            print(f"{fmt_time(elapsed)} / {fmt_time(duration)}".center(cols))
            print("=" * cols)
            print()

            heading = cue.text.strip() if cue.text.strip() else "[pause]"
            banner = render_banner(heading[:32] if len(heading) > 32 else heading)
            banner_lines = banner.splitlines() if banner else [heading]

            for line in banner_lines:
                print(line.center(cols))
            print()

            wrapped = wrap_block(cue.text if cue.text.strip() else "…", body_width)
            for line in wrapped.splitlines():
                print(line.center(cols))

            print()
            print(f"Cue {idx + 1 if idx >= 0 else 0}/{len(cues)}".center(cols))
            print("=" * cols)

            time.sleep(refresh)
    finally:
        if player and player.poll() is None:
            try:
                player.terminate()
            except Exception:
                pass


def main() -> int:
    ap = argparse.ArgumentParser(description="Audio-synced teleprompter")
    ap.add_argument("--audio", required=True, type=Path, help="Path to the audio file")
    ap.add_argument("--cues", type=Path, help="Cue JSON file")
    ap.add_argument("--generate-cues", type=Path, dest="generate_cues", help="Write generated cues to this JSON file")
    ap.add_argument("--play", action="store_true", help="Play the audio with ffplay while rendering")
    ap.add_argument("--offset", type=float, default=0.0, help="Display offset in seconds (+ delays text, - advances text)")
    ap.add_argument("--refresh", type=float, default=0.06, help="Refresh interval in seconds")
    ap.add_argument("--title", default="TELEPROMPTER", help="Screen title")
    args = ap.parse_args()

    if not args.audio.exists():
        raise SystemExit(f"Audio file not found: {args.audio}")

    if args.generate_cues:
        cues = build_cues(args.audio)
        save_cues(args.generate_cues, cues)
        print(f"Wrote {len(cues)} cues to {args.generate_cues}")
        return 0

    if args.cues:
        cues = load_cues(args.cues)
    else:
        cues = build_cues(args.audio)

    run_teleprompter(args.audio, cues, play=args.play, offset=args.offset, title=args.title, refresh=args.refresh)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
