#!/usr/bin/env python3
"""
encode.py — Encode video chunks to AV1 (SVT-AV1) + Opus audio.

Two modes
─────────
Single-chunk mode  (GitHub Actions matrix job):
    python3 encode.py part_00.mkv [--crf 50] [--preset 4]
    → writes part_00-encoded.mkv next to the input

All-chunks mode  (local pipeline):
    python3 encode.py --all [--crf 50] [--preset 4] [--workers 2] [--out-dir encoded-parts]
    → encodes every part_*.mkv found in the current directory in parallel

Progress is written to /tmp/.prog_<stem>.txt for monitor.py to read.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from utils import (R, B, GR, RD, CY, YL, DIM,
                   check_ffmpeg, get_subtitle_maps, get_frame_count,
                   fmt_size, fmt_duration, progress_bar, FFMPEG)

DEFAULT_CRF     = 50
DEFAULT_PRESET  = 4
DEFAULT_WORKERS = os.cpu_count() or 1   # auto-detect physical cores
PROG_DIR        = Path("/tmp")   # where .prog_<stem>.txt files are written


# ─── Single-chunk encoder ────────────────────────────────────────────────────
def encode_chunk(
    chunk: Path,
    out_dir: Path,
    crf: int    = DEFAULT_CRF,
    preset: int = DEFAULT_PRESET,
) -> tuple[Path, bool]:
    """
    Encode a single chunk with SVT-AV1 + Opus.
    Returns (output_path, success).
    Progress is streamed to PROG_DIR/.prog_<stem>.txt.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out       = out_dir / chunk.name.replace(".mkv", "-encoded.mkv")
    prog_file = PROG_DIR / f".prog_{chunk.stem}.txt"

    sub_maps = get_subtitle_maps(chunk)
    total_frames = get_frame_count(chunk)
    print(f"  {DIM}[{chunk.name}] frames={total_frames}  "
          f"sub_maps={sub_maps or 'none'}{R}", flush=True)

    cmd = [
        FFMPEG,
        "-progress", str(prog_file), "-nostats",
        "-analyzeduration", "100M", "-probesize", "100M",
        "-i", str(chunk),
        "-map", "0:v", "-map", "0:a", *sub_maps,
        "-c:v", "libsvtav1",
        "-preset", str(preset),
        "-crf", str(crf),
        "-svtav1-params", "tune=2:enable-overlays=1:tile-columns=1",
        "-c:a", "libopus", "-b:a", "64k",
        "-c:s", "copy",
        str(out), "-y",
    ]

    start = time.time()
    proc  = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    _, stderr = proc.communicate()

    # Clean up progress file
    prog_file.unlink(missing_ok=True)

    elapsed = time.time() - start

    if proc.returncode != 0:
        err = stderr.decode(errors="replace")
        print(f"\n{RD}❌  Encode failed: {chunk.name}\n{err}{R}", flush=True)
        return out, False

    size_mb = out.stat().st_size / 1_048_576 if out.exists() else 0
    print(f"  {GR}✅  {chunk.name} → {out.name}  "
          f"({fmt_size(size_mb)})  [{fmt_duration(elapsed)}]{R}", flush=True)
    return out, True


# ─── All-chunks encoder (local parallel mode) ────────────────────────────────
def encode_all(
    chunks: list[Path],
    out_dir: Path    = Path("encoded-parts"),
    crf: int         = DEFAULT_CRF,
    preset: int      = DEFAULT_PRESET,
    workers: int     = DEFAULT_WORKERS,
) -> list[Path]:
    """
    Encode all *chunks* in parallel (up to *workers* simultaneous ffmpeg processes).
    Returns sorted list of successfully encoded output paths.
    """
    check_ffmpeg()
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{B}━━━ Encode ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{R}", flush=True)
    print(f"  Chunks  : {len(chunks)}", flush=True)
    print(f"  CRF     : {crf}   Preset : {preset}", flush=True)
    print(f"  Workers : {workers}", flush=True)
    print(f"  Out dir : {out_dir}", flush=True)

    results: list[Path] = []
    done = 0

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(encode_chunk, c, out_dir, crf, preset): c
            for c in chunks
        }
        for future in as_completed(futures):
            chunk = futures[future]
            out_path, ok = future.result()
            done += 1
            tag = f"[{done:02d}/{len(chunks):02d}]"
            if ok:
                results.append(out_path)
            else:
                print(f"  {RD}{tag} FAILED: {chunk.name}{R}", flush=True)

    print(f"\n{GR}✅  Encode complete: {len(results)}/{len(chunks)} chunks succeeded{R}",
          flush=True)
    return sorted(results)


# ─── Progress snapshot logger (used by GitHub Actions bash loop) ─────────────
def log_progress_snapshot(part_id: str, payload_json: str) -> None:
    """
    Parse a JSON progress payload (from ffmpeg -progress) and pretty-print it.
    Mirrors the push_progress.py inline script from the original workflow.
    """
    import json
    try:
        data  = json.loads(payload_json)
        pct   = data.get("progress", 0)
        spd   = data.get("speed", 0)
        eta   = data.get("eta_seconds", 0)
        done  = data.get("finished", False)
        if done:
            mb = data.get("size_bytes", 0) / 1_048_576
            print(f"[{part_id}] ✅  done — {mb:.1f} MB", flush=True)
        else:
            bar = progress_bar(pct)
            print(f"[{part_id}] {bar} {pct:5.1f}%  {spd:.2f}x  ETA {int(eta)}s",
                  flush=True)
    except Exception as e:
        print(f"[progress] parse error: {e}", file=sys.stderr)


# ─── CLI entry point ─────────────────────────────────────────────────────────
def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Encode video chunk(s) with SVT-AV1 + Opus"
    )
    # Single-chunk mode
    p.add_argument("chunk", nargs="?",
                   help="Single chunk file to encode (GitHub Actions matrix mode)")
    # All-chunks mode
    p.add_argument("--all", action="store_true",
                   help="Encode all part_*.mkv in cwd (local pipeline mode)")
    # Shared options
    p.add_argument("--crf",     type=int, default=DEFAULT_CRF,
                   help=f"CRF value (default {DEFAULT_CRF})")
    p.add_argument("--preset",  type=int, default=DEFAULT_PRESET,
                   help=f"SVT-AV1 preset 0-13 (default {DEFAULT_PRESET})")
    p.add_argument("--out-dir", default=None,
                   help="Output directory (default: next to input / encoded-parts)")
    p.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                   help=f"Parallel workers for --all mode "
                        f"(default: auto-detect CPU cores, currently {DEFAULT_WORKERS})")
    # Progress logger sub-command (called by shell loop in YAML)
    p.add_argument("--log-progress", metavar="JSON",
                   help="Parse a JSON progress payload and print it (internal)")
    p.add_argument("--part-id", default="?",
                   help="Part ID for --log-progress output label")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    if args.log_progress:
        log_progress_snapshot(args.part_id, args.log_progress)
        sys.exit(0)

    check_ffmpeg()

    if args.all:
        chunks  = sorted(Path(".").glob("part_*.mkv"))
        if not chunks:
            print(f"{RD}❌  No part_*.mkv files found in current directory.{R}")
            sys.exit(1)
        out_dir = Path(args.out_dir or "encoded-parts")
        encode_all(chunks, out_dir, args.crf, args.preset, args.workers)

    elif args.chunk:
        chunk   = Path(args.chunk)
        if not chunk.exists():
            print(f"{RD}❌  Chunk not found: {chunk}{R}")
            sys.exit(1)
        out_dir = Path(args.out_dir) if args.out_dir else chunk.parent
        print(f"\n{B}━━━ Encode (single chunk) ━━━━━━━━━━━━━━━━━━━━━━{R}", flush=True)
        print(f"  Chunk  : {chunk}", flush=True)
        print(f"  CRF    : {args.crf}   Preset : {args.preset}", flush=True)
        _, ok = encode_chunk(chunk, out_dir, args.crf, args.preset)
        sys.exit(0 if ok else 1)

    else:
        print(f"{RD}Provide a chunk file or --all.  See --help.{R}")
        sys.exit(1)
