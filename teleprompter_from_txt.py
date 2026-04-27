#!/usr/bin/env python3
"""
Teleprompter synced to a precomputed cue file.

What this script does:
- reads a plain .txt script from you
- reads the pause/speech timing from a cue JSON file
- spreads the text across the spoken intervals
- shows blank/pause screens during pause intervals
- wraps text neatly in a terminal
- optionally plays the audio if ffplay exists

Expected cue format (same as the previous solution):
[
  {"start": 0.0, "end": 1.087, "text": ""},
  {"start": 1.087, "end": 17.285, "text": "spoken"},
  ...
]

Text file format:
- plain text
- blank lines separate paragraphs
- the whole file is treated as one script and automatically distributed
  across the non-pause cue intervals by duration

Example:
  python teleprompter_from_txt.py --audio ending.mp3 --cues ending_cues.json --text script.txt --play
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
from typing import List


@dataclass
class Cue:
    start: float
    end: float
    text: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def is_pause(self) -> bool:
        return not self.text.strip()


def fmt_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    m = int(seconds // 60)
    s = seconds - (m * 60)
    return f"{m:02d}:{s:06.3f}"


def clear_screen() -> None:
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()


def load_cues(path: Path) -> List[Cue]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    cues = [Cue(float(item["start"]), float(item["end"]), str(item.get("text", ""))) for item in raw]
    cues = sorted(cues, key=lambda c: (c.start, c.end))
    return cues


def load_text(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.strip()


def tokenize_words(text: str) -> List[str]:
    # Keep punctuation attached to the preceding word so the output reads naturally.
    return re.findall(r"\S+", text)


def split_text_by_durations(text: str, durations: List[float]) -> List[str]:
    words = tokenize_words(text)
    if not durations:
        return []
    if not words:
        return ["" for _ in durations]

    if len(durations) == 1:
        return [" ".join(words)]

    total_duration = sum(max(0.0, d) for d in durations)
    if total_duration <= 0:
        total_duration = float(len(durations))

    # Estimate boundaries by proportional duration, then adjust to avoid empty chunks.
    total_words = len(words)
    boundaries = [0]
    consumed = 0

    for i, dur in enumerate(durations[:-1], start=1):
        remaining_segments = len(durations) - i
        remaining_words = total_words - consumed
        if remaining_words <= 0:
            boundaries.append(total_words)
            continue

        target = consumed + round(total_words * (dur / total_duration))
        # Keep enough words for the remaining segments.
        min_allowed = consumed + 1
        max_allowed = total_words - remaining_segments
        target = max(min_allowed, min(max_allowed, target))
        boundaries.append(target)
        consumed = target

    boundaries.append(total_words)

    # Ensure strictly increasing boundaries when possible.
    fixed = [boundaries[0]]
    for b in boundaries[1:]:
        fixed.append(max(fixed[-1], b))

    chunks: List[str] = []
    for a, b in zip(fixed[:-1], fixed[1:]):
        chunk = " ".join(words[a:b]).strip()
        chunks.append(chunk)

    # If we ended up with fewer chunks than durations because of edge cases, pad.
    while len(chunks) < len(durations):
        chunks.append("")
    return chunks[: len(durations)]


def wrap_block(text: str, width: int) -> str:
    if not text.strip():
        return ""
    paras = text.split("\n")
    lines: List[str] = []
    for para in paras:
        if not para.strip():
            lines.append("")
        else:
            lines.extend(textwrap.wrap(para, width=max(20, width), break_long_words=False, break_on_hyphens=False) or [""])
    return "\n".join(lines)


def render_big(text: str) -> str:
    figlet = shutil.which("figlet")
    if figlet and text.strip():
        try:
            out = subprocess.check_output([figlet, text], text=True)
            return out.rstrip("\n")
        except Exception:
            pass
    return text.upper()


def current_index(cues: List[Cue], t: float) -> int:
    if not cues:
        return -1
    for i, cue in enumerate(cues):
        if cue.start <= t < cue.end:
            return i
    return len(cues) - 1 if t >= cues[-1].end else 0


def play_audio(audio_path: Path) -> subprocess.Popen | None:
    ffplay = shutil.which("ffplay")
    if not ffplay:
        return None
    return subprocess.Popen(
        [ffplay, "-nodisp", "-autoexit", "-loglevel", "quiet", str(audio_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def run(audio_path: Path | None, cues: List[Cue], text_chunks: List[str], play: bool, offset: float, refresh: float, title: str) -> None:
    if len(text_chunks) < sum(1 for cue in cues if not cue.is_pause):
        text_chunks = text_chunks + [""] * (sum(1 for cue in cues if not cue.is_pause) - len(text_chunks))

    speech_i = 0
    cue_display: List[str] = []
    for cue in cues:
        if cue.is_pause:
            cue_display.append("")
        else:
            cue_display.append(text_chunks[speech_i] if speech_i < len(text_chunks) else "")
            speech_i += 1

    duration = cues[-1].end if cues else 0.0

    player = None
    if play and audio_path is not None:
        player = play_audio(audio_path)

    start_wall = time.monotonic()
    try:
        while True:
            elapsed = time.monotonic() - start_wall + offset
            if elapsed > duration + 0.15:
                break

            idx = current_index(cues, elapsed)
            cue = cues[idx] if idx >= 0 else Cue(0.0, duration, "")
            body = cue_display[idx] if idx >= 0 else ""

            cols = shutil.get_terminal_size((100, 30)).columns
            body_width = min(84, max(30, cols - 10))

            clear_screen()
            print("=" * cols)
            print(title.center(cols))
            print(f"{fmt_time(elapsed)} / {fmt_time(duration)}".center(cols))
            print("=" * cols)
            print()

            if cue.is_pause:
                pause_label = "[pause]"
                print(pause_label.center(cols))
                print()
                print("…".center(cols))
            else:
                stripped = body.strip()
                short = len(stripped.split()) <= 3 and len(stripped) <= 22
                rendered = render_big(stripped) if short else body
                rendered = wrap_block(rendered, body_width)

                for line in rendered.splitlines() or [""]:
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
    ap = argparse.ArgumentParser(description="Teleprompter synced to existing pause cues")
    ap.add_argument("--cues", required=True, type=Path, help="Cue JSON file from the previous solution")
    ap.add_argument("--text", required=True, type=Path, help="Plain text script (.txt)")
    ap.add_argument("--audio", type=Path, help="Optional audio file to play with ffplay")
    ap.add_argument("--play", action="store_true", help="Play the audio while rendering")
    ap.add_argument("--offset", type=float, default=0.0, help="Display offset in seconds (+ delays, - advances)")
    ap.add_argument("--refresh", type=float, default=0.05, help="Refresh interval in seconds")
    ap.add_argument("--title", default="TELEPROMPTER", help="Header text")
    args = ap.parse_args()

    if not args.cues.exists():
        raise SystemExit(f"Cue file not found: {args.cues}")
    if not args.text.exists():
        raise SystemExit(f"Text file not found: {args.text}")
    if args.play and args.audio is not None and not args.audio.exists():
        raise SystemExit(f"Audio file not found: {args.audio}")

    cues = load_cues(args.cues)
    script_text = load_text(args.text)

    speech_durations = [cue.duration for cue in cues if not cue.is_pause]
    text_chunks = split_text_by_durations(script_text, speech_durations)

    run(
        audio_path=args.audio,
        cues=cues,
        text_chunks=text_chunks,
        play=args.play,
        offset=args.offset,
        refresh=args.refresh,
        title=args.title,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
