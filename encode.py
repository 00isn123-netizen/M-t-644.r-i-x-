"""
encode.py — Encode video chunks to AV1 (SVT-AV1) + Opus audio.

Two modes
─────────
Single-chunk mode  (GitHub Actions matrix job):
    python3 encode.py part_00.mkv [options]
    → writes part_00-encoded.mkv to --out-dir

All-chunks mode  (local pipeline):
    python3 encode.py --all [options]
    → encodes every part_*.mkv in parallel

New options vs original:
    --grain INT        Film grain synthesis 0–50 (default 0)
    --crop VAL         cropdetect string e.g. 1920:800:0:140 (default: none)
    --res INT          Scale height, e.g. 1080 (default: no scale)
    --hdr              Source is HDR10 — apply tonemap to SDR
    --audio-bitrate    Opus bitrate (default 64k)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from utils import (
    R, B, GR, RD, CY, YL, DIM,
    check_ffmpeg, get_subtitle_maps, get_all_subtitle_info, get_frame_count,
    fmt_size, fmt_duration, progress_bar, FFMPEG,
)

# ─── Defaults ─────────────────────────────────────────────────────────────────
DEFAULT_CRF     = 50
DEFAULT_PRESET  = 4
DEFAULT_WORKERS = os.cpu_count() or 1
PROG_DIR        = Path("/tmp")

# ─── SVT-AV1 param builder ────────────────────────────────────────────────────

def _build_svtav1_params(
    grain:    int   = 0,
    duration: float = 1500.0,
) -> str:
    """
    Build the SVT-AV1 tune string with dynamic la-depth.

    la-depth scales to content length:
      < 5 min  → 90  (shorts, OPs, demos)
      < 25 min → 60  (standard episodes)
      25 min + → 40  (movies, long OVAs)
    """
    grain_val = max(0, min(50, int(grain)))

    if duration < 300:
        la_depth = 90
    elif duration < 1500:
        la_depth = 60
    else:
        la_depth = 40

    return (
        f"tune=2:film-grain={grain_val}:enable-overlays=1:"
        f"aq-mode=2:variance-boost-strength=3:variance-octile=6:"
        f"enable-qm=1:qm-min=0:qm-max=8:sharpness=1:"
        f"scd=1:scd-sensitivity=10:enable-tf=1:"
        f"pin=0:lp=2:tile-columns=2:tile-rows=1:la-depth={la_depth}:"
        f"fast-decode=1"
    )


# ─── Video filter builder ─────────────────────────────────────────────────────

def _build_vf(
    crop_val: str | None = None,
    res:      str | None = None,
    is_hdr:   bool       = False,
) -> list[str]:
    """Return the ffmpeg -vf argument list (empty if no filters needed)."""
    filters: list[str] = []

    if crop_val:
        filters.append(f"crop={crop_val}")

    if res and str(res).strip().isdigit():
        filters.append(f"scale=-2:{res}:flags=lanczos")

    if is_hdr:
        # HDR10 → SDR via zscale + tonemap (hable) pipeline
        filters += [
            "zscale=t=linear:npl=100",
            "format=gbrpf32le",
            "zscale=p=bt709",
            "tonemap=hable:desat=0",
            "zscale=t=bt709:m=bt709:r=tv",
            "format=yuv420p10le",
        ]

    return ["-vf", ",".join(filters)] if filters else []


# ─── PGS + subtitle metadata ──────────────────────────────────────────────────

def _build_sub_args(chunk: Path) -> tuple[list[str], list[str]]:
    """
    Returns (pgs_exclusion_args, sub_title_meta_args).

    pgs_exclusions — -map -0:s:N for every PGS/bitmap sub stream.
    sub_title_meta — -metadata:s:s:N title=<lang_name> for kept text subs.
    """
    _PGS = {"hdmv_pgs_subtitle", "dvd_subtitle", "pgssub", "hdmv_pgs_bitmap"}
    sub_info = get_all_subtitle_info(chunk)

    pgs_exclusions: list[str] = []
    sub_title_meta: list[str] = []
    out_sub_idx = 0

    for i, st in enumerate(sub_info):
        codec = st.get("codec", "").lower()
        if codec in _PGS:
            pgs_exclusions += ["-map", f"-0:s:{i}"]
            print(f"  {DIM}[{chunk.name}] Stripping PGS s:{i} ({st['lang']}){R}")
            continue
        # Rename kept text subs to human-readable lang name
        try:
            from rename import lang_code_to_name
            lang_name = lang_code_to_name(st["lang"])
        except Exception:
            lang_name = st["lang"].upper() or "Unknown"
        sub_title_meta += [f"-metadata:s:s:{out_sub_idx}", f"title={lang_name}"]
        out_sub_idx += 1

    return pgs_exclusions, sub_title_meta


# ─── Single-chunk encoder ─────────────────────────────────────────────────────

def encode_chunk(
    chunk:         Path,
    out_dir:       Path,
    crf:           int   = DEFAULT_CRF,
    preset:        int   = DEFAULT_PRESET,
    grain:         int   = 0,
    crop_val:      str | None = None,
    res:           str | None = None,
    is_hdr:        bool  = False,
    audio_bitrate: str   = "64k",
    duration:      float = 1500.0,
) -> tuple[Path, bool]:
    """
    Encode a single chunk with SVT-AV1 + Opus.
    Returns (output_path, success).
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out       = out_dir / chunk.name.replace(".mkv", "-encoded.mkv")
    prog_file = PROG_DIR / f".prog_{chunk.stem}.txt"

    total_frames    = get_frame_count(chunk)
    pgs_exclusions, sub_title_meta = _build_sub_args(chunk)
    vf_args         = _build_vf(crop_val, res, is_hdr)
    svtav1_params   = _build_svtav1_params(grain, duration)

    # SDR color tagging (prevents players misreading levels after tonemap)
    sdr_tags = (
        ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"]
        if not is_hdr else []
    )

    print(
        f"  {DIM}[{chunk.name}] frames={total_frames}  "
        f"crf={crf}  preset={preset}  grain={grain}  "
        f"crop={crop_val or 'none'}  hdr={is_hdr}{R}",
        flush=True,
    )

    cmd = [
        FFMPEG,
        "-progress", str(prog_file), "-nostats",
        "-analyzeduration", "100M", "-probesize", "100M",
        "-i", str(chunk),
        "-map", "0:v", "-map", "0:a", "-map", "0:s?",
        *pgs_exclusions,
        *vf_args,
        "-c:v", "libsvtav1",
        "-pix_fmt", "yuv420p10le",
        "-crf", str(crf),
        *sdr_tags,
        "-preset", str(preset),
        "-svtav1-params", svtav1_params,
        "-threads", "0",
        "-c:a", "libopus", "-b:a", audio_bitrate, "-vbr", "on",
        *sub_title_meta,
        "-c:s", "copy",
        "-map_chapters", "0",
        str(out), "-y",
    ]

    start = time.time()
    proc  = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    _, stderr = proc.communicate()
    prog_file.unlink(missing_ok=True)
    elapsed = time.time() - start

    if proc.returncode != 0:
        err = stderr.decode(errors="replace")
        print(f"\n{RD}❌  Encode failed: {chunk.name}\n{err}{R}", flush=True)
        return out, False

    size_mb = out.stat().st_size / 1_048_576 if out.exists() else 0
    print(
        f"  {GR}✅  {chunk.name} → {out.name}  "
        f"({fmt_size(size_mb)})  [{fmt_duration(elapsed)}]{R}",
        flush=True,
    )
    return out, True


# ─── All-chunks encoder (local parallel mode) ─────────────────────────────────

def encode_all(
    chunks:        list[Path],
    out_dir:       Path    = Path("encoded-parts"),
    crf:           int     = DEFAULT_CRF,
    preset:        int     = DEFAULT_PRESET,
    workers:       int     = DEFAULT_WORKERS,
    grain:         int     = 0,
    crop_val:      str | None = None,
    res:           str | None = None,
    is_hdr:        bool    = False,
    audio_bitrate: str     = "64k",
    duration:      float   = 1500.0,
) -> list[Path]:
    """
    Encode all chunks in parallel.  Returns sorted list of successful outputs.
    """
    check_ffmpeg()
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{B}━━━ Encode ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{R}", flush=True)
    print(f"  Chunks  : {len(chunks)}", flush=True)
    print(f"  CRF     : {crf}   Preset : {preset}   Grain : {grain}", flush=True)
    print(f"  Crop    : {crop_val or 'none'}   HDR : {is_hdr}   "
          f"Res : {res or 'original'}", flush=True)
    print(f"  Audio   : opus @ {audio_bitrate}", flush=True)
    print(f"  Workers : {workers}", flush=True)

    results: list[Path] = []
    done = 0

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                encode_chunk, c, out_dir, crf, preset,
                grain, crop_val, res, is_hdr, audio_bitrate, duration,
            ): c
            for c in chunks
        }
        for future in as_completed(futures):
            chunk                = futures[future]
            out_path, ok         = future.result()
            done                += 1
            tag                  = f"[{done:02d}/{len(chunks):02d}]"
            if ok:
                results.append(out_path)
            else:
                print(f"  {RD}{tag} FAILED: {chunk.name}{R}", flush=True)

    print(
        f"\n{GR}✅  Encode complete: {len(results)}/{len(chunks)} chunks succeeded{R}",
        flush=True,
    )
    return sorted(results)


# ─── Progress snapshot logger (GitHub Actions bash loop) ──────────────────────

def log_progress_snapshot(part_id: str, payload_json: str) -> None:
    try:
        data  = json.loads(payload_json)
        pct   = data.get("progress", 0)
        spd   = data.get("speed",    0)
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


# ─── CLI entry point ──────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Encode video chunk(s) with SVT-AV1 + Opus")
    p.add_argument("chunk",          nargs="?", help="Single chunk file (matrix mode)")
    p.add_argument("--all",          action="store_true", help="Encode all part_*.mkv")
    p.add_argument("--crf",          type=int,   default=DEFAULT_CRF)
    p.add_argument("--preset",       type=int,   default=DEFAULT_PRESET)
    p.add_argument("--grain",        type=int,   default=0,     help="Film grain 0–50")
    p.add_argument("--crop",         default=None,              help="Crop string e.g. 1920:800:0:140")
    p.add_argument("--res",          default=None,              help="Scale height e.g. 1080")
    p.add_argument("--hdr",          action="store_true",       help="Apply HDR→SDR tonemap")
    p.add_argument("--audio-bitrate",default="64k",             help="Opus bitrate (default 64k)")
    p.add_argument("--duration",     type=float, default=1500.0,help="Source duration for la-depth calc")
    p.add_argument("--out-dir",      default=None)
    p.add_argument("--workers",      type=int,   default=DEFAULT_WORKERS)
    p.add_argument("--log-progress", metavar="JSON", help="Parse progress payload (internal)")
    p.add_argument("--part-id",      default="?")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    if args.log_progress:
        log_progress_snapshot(args.part_id, args.log_progress)
        sys.exit(0)

    check_ffmpeg()

    _kwargs = dict(
        crf           = args.crf,
        preset        = args.preset,
        grain         = args.grain,
        crop_val      = args.crop,
        res           = args.res,
        is_hdr        = args.hdr,
        audio_bitrate = args.audio_bitrate,
        duration      = args.duration,
    )

    if args.all:
        chunks  = sorted(Path(".").glob("part_*.mkv"))
        if not chunks:
            print(f"{RD}❌  No part_*.mkv files found.{R}")
            sys.exit(1)
        out_dir = Path(args.out_dir or "encoded-parts")
        encode_all(chunks, out_dir, workers=args.workers, **_kwargs)

    elif args.chunk:
        chunk = Path(args.chunk)
        if not chunk.exists():
            print(f"{RD}❌  Chunk not found: {chunk}{R}")
            sys.exit(1)
        out_dir = Path(args.out_dir) if args.out_dir else chunk.parent
        print(f"\n{B}━━━ Encode (single chunk) ━━━━━━━━━━━━━━━━━━━━━━{R}", flush=True)
        _, ok = encode_chunk(chunk, out_dir, **_kwargs)
        sys.exit(0 if ok else 1)

    else:
        print(f"{RD}Provide a chunk file or --all.  See --help.{R}")
        sys.exit(1)
