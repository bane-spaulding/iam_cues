#!/usr/bin/env python3
"""
Simple teleprompter synced to an existing cue file.

Input format for script.txt:
  start|end|text

Example:
  0.000|1.087|HATE.
  1.087|17.285|Let me tell you how much I've come to hate bloated software.
  17.285|17.808| 
  17.808|24.964|There are 387.44 million lines of C code...

Rules:
- start/end are seconds
- use blank text for pauses
- cue file (ending_cues.json) supplies the timeline
- your text file supplies the words to show at those times

This keeps the logic simple:
- no speech recognition
- no text inference from audio
- no automatic sentence splitting
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


@dataclass
class Segment:
    start: float
    end: float
    text: str

    @property
    def is_pause(self) -> bool:
        return not self.text.strip()


def clear_screen() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def load_cues(path: Path) -> List[Segment]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [Segment(float(x["start"]), float(x["end"]), str(x.get("text", ""))) for x in raw]


def load_script(path: Path) -> List[Segment]:
    segments: List[Segment] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("|", 2)
        if len(parts) != 3:
            raise SystemExit(f"{path}:{line_no}: expected 'start|end|text'")
        try:
            start = float(parts[0].strip())
            end = float(parts[1].strip())
        except ValueError:
            raise SystemExit(f"{path}:{line_no}: bad start/end time")
        text = parts[2].rstrip()
        segments.append(Segment(start, end, text))
    return sorted(segments, key=lambda s: (s.start, s.end))


def find_text_for_time(t: float, script_segments: List[Segment]) -> str:
    # Show the segment that contains the current time.
    for seg in script_segments:
        if seg.start <= t < seg.end:
            return seg.text
    # If we're between segments, show the most recent one.
    past = [seg for seg in script_segments if seg.start <= t]
    return past[-1].text if past else ""


def play_audio(audio: Path) -> Optional[subprocess.Popen]:
    ffplay = shutil.which("ffplay")
    if not ffplay:
        return None
    return subprocess.Popen(
        [ffplay, "-nodisp", "-autoexit", "-loglevel", "quiet", str(audio)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Simple teleprompter using timestamped text and cue timing.")
    ap.add_argument("--cues", required=True, type=Path, help="Cue file like ending_cues.json")
    ap.add_argument("--text", required=True, type=Path, help="Timestamped text file: start|end|text")
    ap.add_argument("--audio", type=Path, help="Optional audio file")
    ap.add_argument("--play", action="store_true", help="Play audio with ffplay")
    ap.add_argument("--offset", type=float, default=0.0, help="Timing offset in seconds")
    ap.add_argument("--refresh", type=float, default=0.05, help="Screen refresh interval")
    ap.add_argument("--title", default="TELEPROMPTER", help="Header text")
    args = ap.parse_args()

    if not args.cues.exists():
        raise SystemExit(f"Cue file not found: {args.cues}")
    if not args.text.exists():
        raise SystemExit(f"Text file not found: {args.text}")
    if args.play and args.audio and not args.audio.exists():
        raise SystemExit(f"Audio file not found: {args.audio}")

    cues = load_cues(args.cues)
    script_segments = load_script(args.text)

    if not cues:
        raise SystemExit("No cues found.")
    if not script_segments:
        raise SystemExit("No script segments found.")

    duration = max(c.end for c in cues)
    player = play_audio(args.audio) if args.play and args.audio else None

    start = time.monotonic()
    try:
        while True:
            now = time.monotonic() - start + args.offset
            if now >= duration:
                break

            cue = next((c for c in cues if c.start <= now < c.end), cues[-1])
            text = find_text_for_time(now, script_segments)

            cols = shutil.get_terminal_size((100, 30)).columns
            clear_screen()
            print("=" * cols)
            print(args.title.center(cols))
            print(f"{now:06.2f} / {duration:06.2f}".center(cols))
            print("=" * cols)
            print()

            if cue.is_pause or not text.strip():
                print("[pause]".center(cols))
            else:
                # Keep it simple: show exactly the provided text.
                for line in text.split("\\n"):
                    print(line.center(cols))

            print()
            print(f"cue {cue.start:.2f} - {cue.end:.2f}".center(cols))
            print("=" * cols)

            time.sleep(args.refresh)
    finally:
        if player and player.poll() is None:
            try:
                player.terminate()
            except Exception:
                pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
