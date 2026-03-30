"""
main.py — Matrix AV1 Encoder — Local Pipeline Orchestrator.

Runs all steps end-to-end on a single machine:
  1. Download    (download.py router)
  2. Probe       (media.py → duration, HDR, fps, etc.)
  3. Rename      (rename.py → structured filename if ANIME_NAME set)
  4. Crop detect (media.py → black-bar detection)
  5. Split       (split.py  → N keyframe-aligned chunks)
  6. Encode      (encode.py → SVT-AV1 + Opus, parallel workers)
  7. Merge       (merge.py  → ffmpeg concat)
  8. Post-process(merge.py  → mkvmerge remux, thumbnail, VMAF, cloud, TG report)
  9. Cleanup     (cleanup.py)

Usage:
    python3 main.py <url> [options]

Examples:
    python3 main.py "https://anibd.app/407332/" --episode 5 --crf 45
    python3 main.py "https://example.com/video.mkv" --chunks 8 --preset 6
    python3 main.py "tg_file:<id>|<name>" --crf 48 --grain 10 --anime-name "Medalist"
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

# ── Resolve project root ──────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))

import config
from utils import R, B, GR, RD, CY, DIM, check_ffmpeg, fmt_size

import download as dl
import split    as sp
import encode   as enc
import merge    as mg
import cleanup  as cl


_CPU_CORES = os.cpu_count() or 1


# ─── Banner ───────────────────────────────────────────────────────────────────

def banner(args: argparse.Namespace) -> None:
    print(f"""
{CY}{B}  ┌──────────────────────────────────────────────────┐
  │     Matrix AV1 Encoder  —  Integrated Pipeline  │
  └──────────────────────────────────────────────────┘{R}
  URL      : {args.url}
  CRF      : {args.crf}    Preset  : {args.preset}
  Chunks   : {args.chunks}    Workers : {args.workers}
  Grain    : {args.grain}    Res     : {args.res or 'original'}
  Output   : {args.output}.mkv
""", flush=True)


# ─── Argument parser ──────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Matrix AV1 Encoder — integrated local pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("url",                             help="Video URL or tg_file: link")
    p.add_argument("--crf",          type=int,  default=50)
    p.add_argument("--preset",       type=int,  default=4)
    p.add_argument("--grain",        type=int,  default=0,     help="Film grain 0–50")
    p.add_argument("--chunks",       type=int,  default=10)
    p.add_argument("--workers",      type=int,  default=_CPU_CORES)
    p.add_argument("--res",          default=None,              help="Scale height e.g. 1080")
    p.add_argument("--audio-bitrate",default="64k",             help="Opus bitrate")
    p.add_argument("--output",       default="encoded",         help="Output name (no ext)")
    p.add_argument("--episode",      type=int,  default=None)
    p.add_argument("--season",       type=int,  default=1)
    p.add_argument("--anime-name",   default="",               help="Structured rename")
    p.add_argument("--content-type", default="Anime")
    p.add_argument("--audio-type",   default="Auto")
    p.add_argument("--sub-tracks",   default="",               help="Subtitle track labels")
    p.add_argument("--audio-tracks", default="",               help="Audio track labels")
    p.add_argument("--run-vmaf",     action="store_true",       help="Run VMAF + SSIM")
    p.add_argument("--run-upload",   action="store_true",       help="Upload to Gofile")
    p.add_argument("--no-crop",      action="store_true",       help="Skip crop detection")
    p.add_argument("--no-cleanup",   action="store_true",       help="Keep intermediates")
    p.add_argument("--skip-download",action="store_true")
    p.add_argument("--skip-split",   action="store_true")
    p.add_argument("--demo-start",   default="0",               help="Demo seek position")
    p.add_argument("--demo-duration",default="",                help="Demo length in seconds")
    return p.parse_args()


# ─── Main pipeline ────────────────────────────────────────────────────────────

def main() -> None:
    args     = _parse_args()
    work_dir = Path(".")
    source   = work_dir / "source.mkv"
    out_file = work_dir / f"{args.output}.mkv"

    check_ffmpeg()
    banner(args)

    # Propagate CLI args into config (so merge.py / media.py read them)
    config.ANIME_NAME    = args.anime_name    or config.ANIME_NAME
    config.CONTENT_TYPE  = args.content_type  or config.CONTENT_TYPE
    config.AUDIO_TYPE    = args.audio_type    or config.AUDIO_TYPE
    config.SUB_TRACKS    = args.sub_tracks    or config.SUB_TRACKS
    config.AUDIO_TRACKS  = args.audio_tracks  or config.AUDIO_TRACKS
    config.RUN_VMAF      = args.run_vmaf      or config.RUN_VMAF
    config.RUN_UPLOAD    = args.run_upload    or config.RUN_UPLOAD
    config.DEMO_START    = args.demo_start    or config.DEMO_START
    config.DEMO_DURATION = args.demo_duration or config.DEMO_DURATION
    if args.season:
        config.SEASON  = str(args.season)
    if args.episode:
        config.EPISODE = str(args.episode)

    # ── Step 1: Download ──────────────────────────────────────────────────
    if args.skip_download:
        if not source.exists():
            print(f"{RD}❌  --skip-download but source.mkv not found.{R}")
            sys.exit(1)
        print(f"{DIM}⏭   Skipping download{R}", flush=True)
    else:
        dl.download(args.url, source,
                    episode=args.episode, season=args.season)

    # ── Step 2: Probe ─────────────────────────────────────────────────────
    from media import get_video_info, get_crop_params
    try:
        duration, width, height, is_hdr, total_frames, channels, fps_val = \
            get_video_info()
    except Exception as e:
        print(f"{RD}❌  Metadata extraction failed: {e}{R}")
        sys.exit(1)

    print(f"\n  {DIM}Source: {duration:.0f}s  {width}×{height}  "
          f"{'HDR' if is_hdr else 'SDR'}  {fps_val:.2f}fps{R}", flush=True)

    # Demo mode: clamp duration for la-depth calculation
    demo_duration_sec = 0.0
    if config.DEMO_DURATION:
        try:
            demo_duration_sec = float(config.DEMO_DURATION)
            duration = min(demo_duration_sec, duration)
            print(f"  {DIM}⚡ DEMO MODE: {demo_duration_sec:.0f}s from {config.DEMO_START}{R}",
                  flush=True)
        except ValueError:
            pass

    # ── Step 3: Crop detection ─────────────────────────────────────────────
    crop_val: str | None = None
    if not args.no_crop:
        print(f"  {DIM}Detecting black bars…{R}", flush=True)
        crop_val = get_crop_params(duration)
        if crop_val:
            print(f"  {GR}Crop detected: {crop_val}{R}", flush=True)
        else:
            print(f"  {DIM}No crop needed{R}", flush=True)

    # ── Step 4: Split ─────────────────────────────────────────────────────
    if args.skip_split:
        chunks = sorted(work_dir.glob("part_*.mkv"))
        if not chunks:
            print(f"{RD}❌  --skip-split but no part_*.mkv found.{R}")
            sys.exit(1)
        print(f"{DIM}⏭   Skipping split — {len(chunks)} existing chunks{R}", flush=True)
    else:
        chunks = sp.split(source, args.chunks, work_dir)

    # ── Step 5: Encode ────────────────────────────────────────────────────
    enc_dir = work_dir / "encoded-parts"
    encoded = enc.encode_all(
        chunks,
        enc_dir,
        crf           = args.crf,
        preset        = args.preset,
        workers       = args.workers,
        grain         = args.grain,
        crop_val      = crop_val,
        res           = args.res,
        is_hdr        = is_hdr,
        audio_bitrate = args.audio_bitrate,
        duration      = duration,
    )

    # ── Step 6: Verify ────────────────────────────────────────────────────
    if len(encoded) != len(chunks):
        print(f"{RD}❌  Encode mismatch: {len(encoded)} encoded / {len(chunks)} chunks.{R}")
        sys.exit(1)

    # ── Step 7: Merge ─────────────────────────────────────────────────────
    mg.merge(encoded, out_file, expected_count=len(chunks))

    # Save encode params for post-processing
    params = {
        "duration":      duration,
        "width":         width,
        "height":        height,
        "fps_val":       fps_val,
        "crop_val":      crop_val,
        "is_hdr":        is_hdr,
        "grain":         args.grain,
        "audio_bitrate": args.audio_bitrate,
    }
    with open("encode_params.json", "w") as f:
        json.dump(params, f)

    # ── Step 8: Post-process ──────────────────────────────────────────────
    asyncio.run(mg._post_process(
        output           = out_file,
        source           = source,
        crf              = args.crf,
        preset           = args.preset,
        chunk_count      = len(chunks),
        run_vmaf         = config.RUN_VMAF,
        run_upload       = config.RUN_UPLOAD,
        encoder_title    = config.ENCODER_TITLE,
        anime_name       = config.ANIME_NAME,
        season           = config.SEASON,
        episode          = config.EPISODE,
        audio_type       = config.AUDIO_TYPE,
        content_type     = config.CONTENT_TYPE,
        sub_tracks_lbl   = config.SUB_TRACKS,
        audio_tracks_lbl = config.AUDIO_TRACKS,
        **params,
    ))

    # ── Step 9: Cleanup ───────────────────────────────────────────────────
    if not args.no_cleanup:
        cl.cleanup_local(work_dir=work_dir, keep_source=False, keep_encoded=False)
        if enc_dir.exists():
            shutil.rmtree(enc_dir, ignore_errors=True)
        for tmp in ("encode_params.json",):
            if Path(tmp).exists():
                Path(tmp).unlink()
    else:
        print(f"{DIM}⏭   Skipping cleanup (--no-cleanup){R}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{RD}Interrupted by user.{R}")
        sys.exit(1)
