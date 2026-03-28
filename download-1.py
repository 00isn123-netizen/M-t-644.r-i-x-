#!/usr/bin/env python3
"""
download.py — Download step.

Detects anibd.app URLs and routes them through anibd.py (HLS segment pipeline).
All other URLs are fetched with curl (direct MKV/MP4 links).

Pipeline output : source.mkv

CLI usage:
    python3 download.py <url> [--output source.mkv] [--episode N] [--season N]
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

# Resolve utils relative to this file so the module works when called from any cwd
sys.path.insert(0, str(Path(__file__).parent))
from utils import R, B, GR, RD, CY, YL, DIM, check_ffmpeg, verify_mkv_magic, fmt_size

ANIBD_HOST = "anibd.app"


# ─── Public API ──────────────────────────────────────────────────────────────
def download(url: str, output: Path = Path("source.mkv"),
             episode: int | None = None, season: int = 1) -> None:
    """
    Download *url* to *output*.
    Automatically selects anibd.py for anibd.app URLs, curl for everything else.
    """
    check_ffmpeg()
    output = Path(output)

    if ANIBD_HOST in url:
        _download_anibd(url, output, episode=episode, season=season)
    else:
        _download_direct(url, output)


# ─── anibd.app route ─────────────────────────────────────────────────────────
def _download_anibd(url: str, output: Path,
                    episode: int | None, season: int) -> None:
    """Import anibd.py and call its pipeline download() function."""
    print(f"{CY}🎌  anibd.app URL detected — routing through anibd.py{R}", flush=True)

    # Set env vars consumed by anibd.py
    if episode is not None:
        os.environ["EPISODE"] = str(episode)
    os.environ["SEASON"] = str(season)

    # Locate anibd.py next to this file (or in cwd)
    candidates = [
        Path(__file__).parent / "anibd.py",
        Path.cwd() / "anibd.py",
    ]
    anibd_path = next((p for p in candidates if p.exists()), None)
    if anibd_path is None:
        print(f"{RD}❌  anibd.py not found.  "
              f"Place it in the same directory as download.py.{R}")
        sys.exit(1)

    # Load anibd as a module without polluting sys.modules permanently
    spec   = importlib.util.spec_from_file_location("anibd", anibd_path)
    anibd  = importlib.util.module_from_spec(spec)        # type: ignore[arg-type]
    spec.loader.exec_module(anibd)                        # type: ignore[union-attr]

    # anibd.download() always writes to source.mkv in the *current* directory.
    # Change to the output's parent so the file lands in the right place.
    orig_cwd = Path.cwd()
    try:
        os.chdir(output.parent.resolve())
        anibd.download(url)                               # type: ignore[attr-defined]
    finally:
        os.chdir(orig_cwd)

    # Rename source.mkv → output if they differ
    written = output.parent / "source.mkv"
    if written.resolve() != output.resolve() and written.exists():
        written.rename(output)

    _verify_output(output)


# ─── Direct URL route ─────────────────────────────────────────────────────────
def _download_direct(url: str, output: Path) -> None:
    """Download a direct video link (MKV/MP4) with curl."""
    print(f"{CY}⬇  Direct download via curl: {url}{R}", flush=True)

    result = subprocess.run(
        ["curl", "--fail", "--retry", "3", "--location", "--globoff",
         "--output", str(output), "--progress-bar", url],
    )
    if result.returncode != 0:
        print(f"{RD}❌  curl download failed (exit {result.returncode}).{R}")
        sys.exit(1)

    size = output.stat().st_size if output.exists() else 0
    if size < 1_000_000:
        print(f"{RD}❌  Output file is too small ({size} bytes). "
              f"Download likely failed.{R}")
        sys.exit(1)

    if not verify_mkv_magic(output):
        magic = output.read_bytes()[:4].hex() if output.exists() else "N/A"
        print(f"{YL}⚠  File magic bytes are {magic} — not a standard MKV. "
              f"Inspect the file if encoding fails.{R}")

    _verify_output(output)


# ─── Output verification ──────────────────────────────────────────────────────
def _verify_output(output: Path) -> None:
    if not output.exists():
        print(f"{RD}❌  Expected output not found: {output}{R}")
        sys.exit(1)
    size_mb = output.stat().st_size / 1_048_576
    print(f"{GR}✅  Download complete → {output}  ({fmt_size(size_mb)}){R}", flush=True)


# ─── CLI entry point ──────────────────────────────────────────────────────────
def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Download video from URL → source.mkv")
    p.add_argument("url",                       help="anibd.app URL or direct video link")
    p.add_argument("--output",  default="source.mkv", help="Output filename (default: source.mkv)")
    p.add_argument("--episode", type=int, default=None, help="Episode number (anibd.app only)")
    p.add_argument("--season",  type=int, default=1,    help="Season number  (anibd.app only)")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    download(args.url, Path(args.output), episode=args.episode, season=args.season)
