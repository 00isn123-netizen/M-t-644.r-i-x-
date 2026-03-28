#!/usr/bin/env python3
"""
utils.py — Shared helpers: ffmpeg wrappers, ANSI colours, formatters.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

# ─── ffmpeg binary names ──────────────────────────────────────────────────────
FFMPEG  = "ffmpeg"
FFPROBE = "ffprobe"

# ─── ANSI Colours ─────────────────────────────────────────────────────────────
R   = "\033[0m"
B   = "\033[1m"
DIM = "\033[2m"
CY  = "\033[96m"
GR  = "\033[92m"
YL  = "\033[93m"
RD  = "\033[91m"

# ─── Dependency checks ────────────────────────────────────────────────────────
def check_ffmpeg() -> None:
    """Exit with a clear message if ffmpeg/ffprobe are not on PATH."""
    for tool in (FFMPEG, FFPROBE):
        if shutil.which(tool) is None:
            print(f"{RD}Error: '{tool}' is not installed or not in PATH.{R}")
            sys.exit(1)

# ─── ffprobe helpers ──────────────────────────────────────────────────────────
def get_duration(path: Path) -> float:
    """Return video duration in seconds."""
    result = subprocess.run(
        [FFPROBE, "-v", "error",
         "-analyzeduration", "100M", "-probesize", "100M",
         "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1",
         str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def get_frame_count(path: Path) -> int:
    """Return total frame count for the first video stream (0 if unavailable)."""
    result = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=nb_frames",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    raw = result.stdout.strip().splitlines()
    try:
        return int(raw[0]) if raw else 0
    except (ValueError, IndexError):
        return 0


def get_subtitle_maps(path: Path) -> list[str]:
    """
    Return ffmpeg -map arguments for text-based subtitle streams only.
    PGS (hdmv_pgs_subtitle) and DVD (dvd_subtitle) bitmap formats are skipped.
    """
    skip = {"hdmv_pgs_subtitle", "dvd_subtitle"}
    result = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "s",
         "-show_entries", "stream=codec_name",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    maps: list[str] = []
    for i, codec in enumerate(result.stdout.strip().splitlines()):
        if codec.strip() not in skip:
            maps.extend(["-map", f"0:s:{i}"])
    return maps


def verify_mkv_magic(path: Path) -> bool:
    """Return True if the file starts with the MKV/EBML magic bytes."""
    try:
        magic = path.read_bytes()[:4].hex()
        return magic == "1a45dfa3"
    except OSError:
        return False

# ─── Formatting helpers ───────────────────────────────────────────────────────
def fmt_size(mb: float) -> str:
    return f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{mb:.0f} MB"


def fmt_duration(secs: float) -> str:
    m, s = divmod(int(secs), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def progress_bar(pct: float, width: int = 20) -> str:
    """Return a Unicode fill-bar string for the given percentage 0–100."""
    filled = int(pct / 100 * width)
    return "▰" * filled + "▱" * (width - filled)
