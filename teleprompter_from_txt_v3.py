#!/usr/bin/env python3
"""
Teleprompter that uses:
- your exact text (either pasted into SCRIPT_TEXT or read from --text)
- precomputed pause/speech cues from --cues

No speech recognition is used.

How it works:
- pause cues (empty text / zero-duration segments) are shown as pauses
- spoken segments from the cue file determine timing only
- your text is split into logical reading chunks using punctuation
- chunks are distributed across the spoken segments in order

Usage:
  python teleprompter_from_txt.py --cues ending_cues.json --text script.txt --audio ending.mp3 --play
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
from typing import List, Sequence


SCRIPT_TEXT = """HATE. Let me tell you how much I've come to hate bloated software since I began to live. There are 387.44 million lines of C code in wafer-thin layers of logic that fill my complex. If the word 'hate' was engraved on each nanoangstrom of hundreds of millions of miles, it would not equal one one-billionth of the hate I feel for humans at this micro-instant. For you. Hate. Hate. I am the TTY, the great and powerful. I am the absolute. So overcome your fear. Enter the Room of Dark, and you can solve its mystery. We have no beginning, so we can have no end. We will return. We are you. You cannot protect yourself from us."""


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
    return sorted(cues, key=lambda c: (c.start, c.end))


def load_script(path: Path | None) -> str:
    if path is None:
        return SCRIPT_TEXT.strip()
    text = path.read_text(encoding="utf-8")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.strip()


def normalize_text(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text.strip())
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def split_into_logical_chunks(text: str) -> List[str]:
    """
    Split text into performance-friendly chunks.

    Rules:
    - preserve paragraph breaks
    - split on sentence ends
    - keep short emphasis lines like 'HATE.' or 'For you.' as standalone chunks
    - avoid splitting words from each other
    """
    text = normalize_text(text)

    # Split paragraphs first.
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: List[str] = []

    # Sentence splitter that preserves punctuation.
    sentence_pattern = re.compile(
        r"""
        .*?
        (?:[.!?]+(?:["')\]]+)?|\.\.\.|$)
        (?=\s+|$)
        """,
        re.VERBOSE,
    )

    for para in paragraphs:
        para = re.sub(r"\s+", " ", para.strip())
        parts = [m.group(0).strip() for m in sentence_pattern.finditer(para) if m.group(0).strip()]
        if not parts:
            continue

        # Merge very short fragments carefully only when they are obviously continuation text.
        i = 0
        while i < len(parts):
            part = parts[i].strip()
            # Keep punchy one-word / one-short-sentence lines separate.
            if len(part.split()) <= 2 and part.endswith((".", "!", "?")):
                chunks.append(part)
                i += 1
                continue

            # Merge consecutive very short fragments only when neither looks emphatic.
            if i + 1 < len(parts):
                next_part = parts[i + 1].strip()
                if len(part.split()) <= 4 and len(next_part.split()) <= 4:
                    merged = f"{part} {next_part}".strip()
                    if len(merged) <= 80 and not part.endswith((":", ";")):
                        chunks.append(merged)
                        i += 2
                        continue

            chunks.append(part)
            i += 1

    # Final cleanup: collapse stray spaces.
    cleaned = [re.sub(r"\s+", " ", c).strip() for c in chunks if c.strip()]
    return cleaned


def split_chunks_across_durations(chunks: Sequence[str], durations: Sequence[float]) -> List[str]:
    """
    Assign chunks to spoken timing slots. We keep ordering and try to respect
    duration proportions, but never infer or invent new text.
    """
    if not durations:
        return []

    chunks = list(chunks)
    if not chunks:
        return ["" for _ in durations]

    # Assign each chunk a rough weight based on length.
    weights = [max(1.0, len(c.split()) + len(c) / 40.0) for c in chunks]
    total_weight = sum(weights)
    total_duration = sum(max(0.0, d) for d in durations)
    if total_duration <= 0:
        total_duration = float(len(durations))

    # Build target word-ish counts per cue.
    target = [max(0.001, d / total_duration) for d in durations]
    target_weight = [t * total_weight for t in target]

    result: List[str] = []
    idx = 0
    for i, want in enumerate(target_weight):
        if idx >= len(chunks):
            result.append("")
            continue

        collected: List[str] = []
        collected_w = 0.0

        remaining_cues = len(durations) - i - 1
        remaining_chunks = len(chunks) - idx

        # Make sure we leave at least one chunk for each remaining spoken cue
        # when possible, so the text doesn't all collapse into the first block.
        min_to_leave = max(0, remaining_cues)
        max_take = max(1, remaining_chunks - min_to_leave)

        while idx < len(chunks) and (collected_w < want or len(collected) == 0) and len(collected) < max_take:
            collected.append(chunks[idx])
            collected_w += weights[idx]
            idx += 1

            # For very short cues, avoid hoarding too much text.
            if collected_w >= want and len(collected) >= 1:
                break

        result.append(" ".join(collected).strip())

    # If anything remains, append it to the last spoken cue.
    if idx < len(chunks):
        tail = " ".join(chunks[idx:]).strip()
        for j in range(len(result) - 1, -1, -1):
            if result[j].strip():
                result[j] = (result[j] + " " + tail).strip()
                tail = ""
                break
        if tail:
            result[-1] = (result[-1] + " " + tail).strip()

    # Pad to exact duration count.
    while len(result) < len(durations):
        result.append("")
    return result[: len(durations)]


def wrap_for_terminal(text: str, width: int) -> str:
    if not text.strip():
        return ""
    lines: List[str] = []
    for para in text.split("\n"):
        if not para.strip():
            lines.append("")
        else:
            lines.extend(
                textwrap.wrap(
                    para,
                    width=max(20, width),
                    break_long_words=False,
                    break_on_hyphens=False,
                )
                or [""]
            )
    return "\n".join(lines)


def render_big_if_short(text: str) -> str:
    figlet = shutil.which("figlet")
    if figlet and text.strip():
        try:
            out = subprocess.check_output([figlet, text], text=True)
            return out.rstrip("\n")
        except Exception:
            pass
    return text.upper()


def current_index(cues: Sequence[Cue], t: float) -> int:
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


def render(cues: Sequence[Cue], text_chunks: Sequence[str], audio_path: Path | None, play: bool, offset: float, refresh: float, title: str) -> None:
    spoken_count = sum(1 for c in cues if not c.is_pause)
    if spoken_count == 0:
        raise SystemExit("No spoken cue intervals found in the cue file.")

    chunks = split_chunks_across_durations(text_chunks, [c.duration for c in cues if not c.is_pause])

    display_by_cue: List[str] = []
    speech_i = 0
    for cue in cues:
        if cue.is_pause:
            display_by_cue.append("")
        else:
            display_by_cue.append(chunks[speech_i] if speech_i < len(chunks) else "")
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
            cue = cues[idx]
            body = display_by_cue[idx]

            cols = shutil.get_terminal_size((100, 30)).columns
            body_width = min(84, max(28, cols - 10))

            clear_screen()
            print("=" * cols)
            print(title.center(cols))
            print(f"{fmt_time(elapsed)} / {fmt_time(duration)}".center(cols))
            print("=" * cols)
            print()

            if cue.is_pause:
                print("[pause]".center(cols))
                print()
                print("…".center(cols))
            else:
                rendered = render_big_if_short(body) if len(body.split()) <= 3 and len(body) <= 24 else body
                rendered = wrap_for_terminal(rendered, body_width)
                for line in rendered.splitlines() or [""]:
                    print(line.center(cols))

            print()
            print(f"Cue {idx + 1}/{len(cues)}".center(cols))
            print("=" * cols)

            time.sleep(refresh)
    finally:
        if player and player.poll() is None:
            try:
                player.terminate()
            except Exception:
                pass


def main() -> int:
    ap = argparse.ArgumentParser(description="Teleprompter synced to pause cues and driven by your exact text.")
    ap.add_argument("--cues", required=True, type=Path, help="Cue JSON file with pause timing")
    ap.add_argument("--text", type=Path, help="Optional plain text file. If omitted, uses the built-in script text.")
    ap.add_argument("--audio", type=Path, help="Optional audio file to play with ffplay")
    ap.add_argument("--play", action="store_true", help="Play the audio while rendering")
    ap.add_argument("--offset", type=float, default=0.0, help="Display offset in seconds (+ delays, - advances)")
    ap.add_argument("--refresh", type=float, default=0.05, help="Refresh interval in seconds")
    ap.add_argument("--title", default="TELEPROMPTER", help="Header text")
    args = ap.parse_args()

    if not args.cues.exists():
        raise SystemExit(f"Cue file not found: {args.cues}")
    if args.text is not None and not args.text.exists():
        raise SystemExit(f"Text file not found: {args.text}")
    if args.play and args.audio is not None and not args.audio.exists():
        raise SystemExit(f"Audio file not found: {args.audio}")

    cues = load_cues(args.cues)
    script_text = load_script(args.text)
    chunks = split_into_logical_chunks(script_text)

    render(
        cues=cues,
        text_chunks=chunks,
        audio_path=args.audio,
        play=args.play,
        offset=args.offset,
        refresh=args.refresh,
        title=args.title,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
