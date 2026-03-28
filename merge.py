#!/usr/bin/env python3
"""
merge.py — Concatenate encoded chunks into the final MKV.

Verifies the expected chunk count before merging to catch partial failures.

CLI usage:
    python3 merge.py --output encoded --expected 10 [--enc-dir encoded-parts]
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from utils import R, B, GR, RD, CY, DIM, fmt_size, FFMPEG


# ─── Public API ──────────────────────────────────────────────────────────────
def merge(
    encoded_chunks: list[Path],
    output: Path,
    expected_count: int | None = None,
) -> None:
    """
    Concatenate *encoded_chunks* (sorted) into a single *output* MKV.
    Raises SystemExit if expected_count is given and doesn't match.
    """
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)

    print(f"\n{B}━━━ Merge ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{R}", flush=True)

    if not encoded_chunks:
        print(f"{RD}❌  No encoded chunks provided.{R}")
        sys.exit(1)

    chunks = sorted(encoded_chunks)
    actual = len(chunks)

    if expected_count is not None and actual != expected_count:
        print(f"{RD}❌  Chunk count mismatch: expected {expected_count}, "
              f"found {actual}.  One or more encode jobs likely failed.{R}")
        sys.exit(1)

    print(f"  Chunks  : {actual}", flush=True)
    print(f"  Output  : {output}", flush=True)

    # Write ffmpeg concat list
    list_file = output.parent / ".concat_list.txt"
    with open(list_file, "w") as f:
        for c in chunks:
            f.write(f"file '{c.resolve()}'\n")

    cmd = [
        FFMPEG,
        "-f", "concat", "-safe", "0",
        "-i", str(list_file),
        "-map", "0",
        "-c", "copy",
        str(output), "-y",
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    list_file.unlink(missing_ok=True)

    if result.returncode != 0:
        print(f"{RD}❌  ffmpeg merge failed:\n{result.stderr}{R}")
        sys.exit(1)

    size_mb = output.stat().st_size / 1_048_576
    print(f"{GR}✅  Merge complete → {output}  ({fmt_size(size_mb)}){R}", flush=True)


def print_summary(
    output: Path,
    crf: int,
    preset: int,
    chunk_count: int,
) -> None:
    """Print a job summary similar to the YAML Summary step."""
    size_mb = output.stat().st_size / 1_048_576
    print(f"""
{GR}{B}━━━ ✅  Encode Complete ━━━━━━━━━━━━━━━━━━━━━━━━{R}
  Output        : {CY}{output}{R}
  Output size   : {fmt_size(size_mb)}
  CRF           : {crf}   Preset : {preset}
  Chunks merged : {chunk_count}
{GR}{B}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{R}
""", flush=True)


# ─── CLI entry point ──────────────────────────────────────────────────────────
def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Merge encoded chunks into final MKV")
    p.add_argument("--output",   default="encoded",
                   help="Output filename without extension (default: encoded)")
    p.add_argument("--enc-dir",  default="encoded-parts",
                   help="Directory containing *-encoded.mkv files (default: encoded-parts)")
    p.add_argument("--expected", type=int, default=None,
                   help="Expected chunk count (abort if mismatch)")
    p.add_argument("--crf",      type=int, default=None, help="CRF used (for summary only)")
    p.add_argument("--preset",   type=int, default=None, help="Preset used (for summary only)")
    return p.parse_args()


if __name__ == "__main__":
    args     = _parse_args()
    enc_dir  = Path(args.enc_dir)
    chunks   = sorted(enc_dir.glob("*-encoded.mkv"))
    output   = Path(f"{args.output}.mkv")

    merge(chunks, output, expected_count=args.expected)

    if args.crf is not None and args.preset is not None:
        print_summary(output, args.crf, args.preset, len(chunks))
