#!/usr/bin/env python3
"""
main.py — Local pipeline orchestrator.

Runs all steps end-to-end on a single machine:
  1. Download  (anibd.py  or  curl)
  2. Split     (ffmpeg segment muxer)
  3. Encode    (SVT-AV1 + Opus, parallel)
  4. Merge     (ffmpeg concat)
  5. Cleanup   (remove intermediates)

Usage:
    python3 main.py <url> [options]

Examples:
    # Download episode 5 from anibd.app and encode at CRF 45
    python3 main.py "https://anibd.app/407332/" --episode 5 --crf 45

    # Encode a direct MKV link, 8 chunks, preset 6, keep intermediate files
    python3 main.py "https://example.com/video.mkv" --chunks 8 --preset 6 --no-cleanup

Options:
    --crf INT          CRF value (lower = better quality).  Default: 50
    --preset INT       SVT-AV1 preset 0=slowest, 13=fastest.  Default: 4
    --chunks INT       Number of video chunks for parallel encode.  Default: 10
    --workers INT      Parallel encode processes (local CPU, default: auto-detect cores)
    --output NAME      Output filename without extension.  Default: encoded
    --episode INT      Episode number for anibd.app downloads
    --season INT       Season number for anibd.app downloads.  Default: 1
    --no-cleanup       Keep intermediate files after encode
    --skip-download    Re-use existing source.mkv (skip download step)
    --skip-split       Re-use existing part_*.mkv files (skip split step)
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

_CPU_CORES = os.cpu_count() or 1

sys.path.insert(0, str(Path(__file__).parent))
from utils import R, B, GR, RD, CY, DIM, check_ffmpeg, fmt_size

import download as dl
import split    as sp
import encode   as enc
import merge    as mg
import cleanup  as cl


# ─── Banner ───────────────────────────────────────────────────────────────────
def banner(args: argparse.Namespace) -> None:
    print(f"""
{CY}{B}  ┌──────────────────────────────────────────────────┐
  │     Matrix AV1 Encoder  —  Python Local Pipeline  │
  └──────────────────────────────────────────────────┘{R}
  URL      : {args.url}
  CRF      : {args.crf}    Preset  : {args.preset}
  Chunks   : {args.chunks}    Workers : {args.workers}
  Output   : {args.output}.mkv
""", flush=True)


# ─── Argument parser ──────────────────────────────────────────────────────────
def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Matrix AV1 Encoder — local Python pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("url",
                   help="anibd.app URL or direct video link")
    p.add_argument("--crf",          type=int, default=50,
                   help="CRF value (default: 50)")
    p.add_argument("--preset",       type=int, default=4,
                   help="SVT-AV1 preset 0-13 (default: 4)")
    p.add_argument("--chunks",       type=int, default=10,
                   help="Number of chunks (default: 10)")
    p.add_argument("--workers",      type=int, default=_CPU_CORES,
                   help=f"Parallel encode workers (default: auto-detect, currently {_CPU_CORES})")
    p.add_argument("--output",       default="encoded",
                   help="Output name without extension (default: encoded)")
    p.add_argument("--episode",      type=int, default=None,
                   help="Episode number (anibd.app only)")
    p.add_argument("--season",       type=int, default=1,
                   help="Season number  (anibd.app only, default: 1)")
    p.add_argument("--no-cleanup",   action="store_true",
                   help="Keep intermediate files")
    p.add_argument("--skip-download",action="store_true",
                   help="Skip download; re-use existing source.mkv")
    p.add_argument("--skip-split",   action="store_true",
                   help="Skip split; re-use existing part_*.mkv")
    return p.parse_args()


# ─── Main pipeline ────────────────────────────────────────────────────────────
def main() -> None:
    args     = _parse_args()
    work_dir = Path(".")
    source   = work_dir / "source.mkv"
    out_file = work_dir / f"{args.output}.mkv"

    check_ffmpeg()
    banner(args)

    # ── Step 1 : Download ────────────────────────────────────────────────────
    if args.skip_download:
        if not source.exists():
            print(f"{RD}❌  --skip-download set but source.mkv not found.{R}")
            sys.exit(1)
        print(f"{DIM}⏭   Skipping download — using existing source.mkv{R}", flush=True)
    else:
        dl.download(
            args.url, source,
            episode=args.episode,
            season=args.season,
        )

    # ── Step 2 : Split ───────────────────────────────────────────────────────
    if args.skip_split:
        chunks = sorted(work_dir.glob("part_*.mkv"))
        if not chunks:
            print(f"{RD}❌  --skip-split set but no part_*.mkv found.{R}")
            sys.exit(1)
        print(f"{DIM}⏭   Skipping split — using {len(chunks)} existing chunks{R}",
              flush=True)
    else:
        chunks = sp.split(source, args.chunks, work_dir)

    # ── Step 3 : Encode ──────────────────────────────────────────────────────
    enc_dir = work_dir / "encoded-parts"
    encoded = enc.encode_all(
        chunks, enc_dir,
        crf=args.crf,
        preset=args.preset,
        workers=args.workers,
    )

    # ── Step 4 : Verify chunk count ──────────────────────────────────────────
    if len(encoded) != len(chunks):
        print(f"{RD}❌  Encode mismatch: {len(encoded)} encoded / {len(chunks)} chunks. "
              f"Check logs above for failed chunks.{R}")
        sys.exit(1)

    # ── Step 5 : Merge ───────────────────────────────────────────────────────
    mg.merge(encoded, out_file, expected_count=len(chunks))
    mg.print_summary(out_file, args.crf, args.preset, len(chunks))

    # ── Step 6 : Cleanup ─────────────────────────────────────────────────────
    if not args.no_cleanup:
        cl.cleanup_local(
            work_dir     = work_dir,
            keep_source  = False,
            keep_encoded = False,
        )
        # Also remove encoded-parts dir
        if enc_dir.exists():
            shutil.rmtree(enc_dir, ignore_errors=True)
    else:
        print(f"{DIM}⏭   Skipping cleanup (--no-cleanup){R}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{RD}Interrupted by user.{R}")
        sys.exit(1)
