"""
merge.py — Merge encoded chunks + post-processing for Matrix AV1 Encoder.

Steps:
  1. Concat encoded chunks → final.mkv   (ffmpeg concat)
  2. mkvmerge remux          (chapters from source, encoder title tag)
  3. Thumbnail generation    (ffmpeg frame capture)
  4. VMAF + SSIM             (optional, --run-vmaf)
  5. Gofile cloud upload     (optional, --run-upload)
  6. Telegram final report   (optional, requires TG credentials)

CLI usage (GitHub Actions merge job):
    python3 merge.py --output encoded --expected 10 [options]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from utils import R, B, GR, RD, CY, DIM, fmt_size, fmt_duration, FFMPEG

# ─── Public merge API ─────────────────────────────────────────────────────────

def merge(
    encoded_chunks:  list[Path],
    output:          Path,
    expected_count:  int | None = None,
) -> None:
    """Concatenate sorted encoded chunks into a single output MKV."""
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
              f"found {actual}.{R}")
        sys.exit(1)

    print(f"  Chunks  : {actual}", flush=True)
    print(f"  Output  : {output}", flush=True)

    list_file = output.parent / ".concat_list.txt"
    with open(list_file, "w") as f:
        for c in chunks:
            f.write(f"file '{c.resolve()}'\n")

    result = subprocess.run(
        [FFMPEG, "-f", "concat", "-safe", "0",
         "-i", str(list_file),
         "-map", "0", "-c", "copy",
         str(output), "-y"],
        capture_output=True, text=True,
    )
    list_file.unlink(missing_ok=True)

    if result.returncode != 0:
        print(f"{RD}❌  ffmpeg merge failed:\n{result.stderr}{R}")
        sys.exit(1)

    size_mb = output.stat().st_size / 1_048_576
    print(f"{GR}✅  Merge complete → {output}  ({fmt_size(size_mb)}){R}", flush=True)


# ─── mkvmerge remux ───────────────────────────────────────────────────────────

def remux_with_source(
    output:        Path,
    source:        Path,
    encoder_title: str = "",
) -> None:
    """
    Copy chapters, attachments, and tags from source into output.
    Optionally stamp the MKV Title tag with encoder_title.
    Uses a temp file to avoid in-place remux issues.
    """
    if not source.exists():
        print(f"  {DIM}source.mkv not found — skipping remux{R}", flush=True)
        return

    fixed    = output.parent / f"FIXED_{output.name}"
    title_args = ["--title", encoder_title] if encoder_title.strip() else []

    result = subprocess.run(
        ["mkvmerge", "-o", str(fixed),
         *title_args,
         str(output),
         "--no-video", "--no-audio", "--no-subtitles", "--no-attachments",
         str(source)],
        capture_output=True,
    )

    if result.returncode == 0 and fixed.exists():
        output.unlink(missing_ok=True)
        fixed.rename(output)
        print(f"  {GR}✅  mkvmerge remux complete{R}", flush=True)
    else:
        print(f"  {DIM}mkvmerge failed (rc={result.returncode}) — skipping{R}",
              flush=True)
        fixed.unlink(missing_ok=True)


# ─── Post-processing (async) ──────────────────────────────────────────────────

async def _post_process(
    output:        Path,
    source:        Path,
    crf:           int,
    preset:        int,
    chunk_count:   int,
    run_vmaf:      bool = False,
    run_upload:    bool = False,
    encoder_title: str  = "",
    anime_name:    str  = "",
    season:        str  = "1",
    episode:       str  = "1",
    audio_type:    str  = "Auto",
    content_type:  str  = "Anime",
    sub_tracks_lbl:str  = "",
    audio_tracks_lbl:str = "",
    duration:      float = 0.0,
    width:         int   = 0,
    height:        int   = 0,
    fps_val:       float = 24.0,
    crop_val:      str | None = None,
    is_hdr:        bool  = False,
    grain:         int   = 0,
    audio_bitrate: str   = "64k",
    audio_tracks:  list  | None = None,
    sub_tracks:    list  | None = None,
):
    """Full post-encode pipeline: remux → thumbnail → VMAF → upload → TG report."""
    import config
    from media import async_generate_thumbnail, get_vmaf, upload_to_cloud
    from ui import format_time, upload_progress, get_vmaf_ui

    audio_tracks = audio_tracks or []
    sub_tracks   = sub_tracks   or []

    # ── 1. mkvmerge remux ─────────────────────────────────────────────────
    remux_with_source(output, source, encoder_title)
    final_size = output.stat().st_size / 1_048_576

    # ── 2. Determine filename (rename if ANIME_NAME set) ──────────────────
    file_name = output.name
    if anime_name:
        try:
            from rename import (resolve_output_name, get_track_info,
                                detect_quality, format_track_report)
            resolved, audio_type_label, audio_tracks, sub_tracks = resolve_output_name(
                source               = str(output),
                anime_name           = anime_name,
                season               = season,
                episode              = episode,
                height               = height,
                audio_type_override  = audio_type,
                content_type         = content_type,
            )
            new_path = output.parent / resolved
            output.rename(new_path)
            output    = new_path
            file_name = resolved
            print(f"  {GR}✅  Renamed → {file_name}{R}", flush=True)
        except Exception as exc:
            print(f"  {DIM}Rename failed: {exc}{R}", flush=True)
    else:
        try:
            from rename import get_track_info, format_track_report
            audio_tracks, sub_tracks = get_track_info(str(output))
        except Exception:
            pass

    # ── 3. Telegram connection (background) ───────────────────────────────
    tg_state: dict        = {}
    tg_ready              = asyncio.Event()
    config.FILE_NAME      = file_name

    tg_task = asyncio.create_task(
        _tg_connect_background(tg_state, tg_ready, file_name)
    )

    # ── 4. Thumbnail + VMAF + Upload (concurrent) ─────────────────────────
    grid_task = asyncio.create_task(
        async_generate_thumbnail(duration or 1.0, str(output))
    )

    if run_upload:
        cloud_task = asyncio.create_task(
            upload_to_cloud(str(output))
        )
    else:
        cloud_task = None

    if run_vmaf and source.exists():
        async def _vmaf_progress(payload):
            from tg_utils import tg_edit as _tg_edit
            ui = get_vmaf_ui(payload["vmaf_percent"], payload["fps"], payload["eta"])
            await _tg_edit(tg_state, tg_ready, ui)

        vmaf_val, ssim_val = await get_vmaf(
            str(output), crop_val, width, height,
            duration or 1.0, fps_val, kv_writer=_vmaf_progress,
        )
    else:
        vmaf_val = ssim_val = "N/A"

    await grid_task
    cloud = await cloud_task if cloud_task else {"direct": None, "page": None, "source": "disabled"}

    # Wait for TG
    if not tg_ready.is_set():
        try:
            await asyncio.wait_for(tg_ready.wait(), timeout=300)
        except asyncio.TimeoutError:
            pass

    await tg_task

    # ── 5. Build + send Telegram report ───────────────────────────────────
    await _send_final_report(
        tg_state, tg_ready, output, file_name,
        crf, preset, chunk_count, final_size,
        vmaf_val, ssim_val, cloud,
        is_hdr, grain, crop_val, audio_bitrate,
        audio_tracks, sub_tracks,
        audio_type, content_type,
        sub_tracks_lbl, audio_tracks_lbl,
    )


async def _tg_connect_background(tg_state, tg_ready, label):
    try:
        from tg_utils import connect_telegram
        await connect_telegram(tg_state, tg_ready, label)
    except Exception as e:
        print(f"[TG] connect failed: {e}")


async def _send_final_report(
    tg_state, tg_ready, output: Path, file_name: str,
    crf, preset, chunk_count, final_size,
    vmaf_val, ssim_val, cloud,
    is_hdr, grain, crop_val, audio_bitrate,
    audio_tracks, sub_tracks,
    audio_type, content_type,
    sub_tracks_lbl, audio_tracks_lbl,
):
    from tg_utils import tg_edit as _tg_edit
    from ui import format_time, upload_progress
    import config

    app    = tg_state.get("app")
    status = tg_state.get("status")

    if not app or not status:
        print("[TG] No connection — skipping Telegram report.")
        return

    # Build inline buttons
    try:
        from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        from pyrogram import enums
        btn_row: list = []
        gh_repo = os.environ.get("GITHUB_REPOSITORY", "")
        run_id  = os.environ.get("GITHUB_RUN_ID",     "")
        if gh_repo and run_id:
            log_url = f"https://github.com/{gh_repo}/actions/runs/{run_id}"
            btn_row.append(InlineKeyboardButton("📋 Actions Log", url=log_url))
        if cloud["source"] == "gofile" and cloud.get("page"):
            btn_row.append(InlineKeyboardButton("Gofile", url=cloud["page"]))
        elif cloud["source"] == "litterbox" and cloud.get("direct"):
            btn_row.append(InlineKeyboardButton("Litterbox", url=cloud["direct"]))
        buttons = InlineKeyboardMarkup([btn_row]) if btn_row else None
    except Exception:
        buttons = None

    # Size overflow
    if final_size > 2000:
        await _tg_edit(
            tg_state, tg_ready,
            "<b>[ SIZE OVERFLOW ]</b> File too large for Telegram. Cloud link above.",
            reply_markup=buttons,
        )
        if app:
            await app.stop()
        return

    # Track report
    track_report = ""
    try:
        from rename import format_track_report
        track_report = format_track_report(audio_tracks, sub_tracks)
    except Exception:
        pass

    user_notes = ""
    if sub_tracks_lbl:
        user_notes += f"\n🔤 <b>SUB LABELS:</b> <code>{sub_tracks_lbl}</code>"
    if audio_tracks_lbl:
        user_notes += f"\n🔊 <b>AUDIO LABELS:</b> <code>{audio_tracks_lbl}</code>"

    hdr_label   = "HDR10" if is_hdr else "SDR"
    grain_label = f" | Grain: {grain}" if grain else ""
    crop_label  = " | Cropped" if crop_val else ""

    report = (
        f"✅ <b>MATRIX MISSION ACCOMPLISHED</b>\n\n"
        f"📄 <b>FILE:</b> <code>{file_name}</code>\n"
        f"📦 <b>SIZE:</b> <code>{final_size:.2f} MB</code>\n"
        f"📊 <b>QUALITY:</b> VMAF: <code>{vmaf_val}</code> | SSIM: <code>{ssim_val}</code>\n\n"
        f"🛠 <b>SPECS:</b>\n"
        f"└ Preset: {preset} | CRF: {crf} | Chunks: {chunk_count}\n"
        f"└ Video: {hdr_label}{crop_label}{grain_label}\n"
        f"└ Audio: opus @ {audio_bitrate}\n"
        f"└ Type: {content_type}\n"
        f"\n{track_report}"
        f"{user_notes}"
    )

    thumb = config.SCREENSHOT if os.path.exists(config.SCREENSHOT) else None

    try:
        import ui as _ui
        _ui.last_up_pct = -1; _ui.last_up_update = 0; _ui.up_start_time = 0

        await _tg_edit(tg_state, tg_ready,
                       "<b>[ MATRIX.UPLINK ] Transmitting Final Video...</b>")

        from pyrogram import enums
        await app.send_document(
            chat_id=config.CHAT_ID,
            document=str(output),
            file_name=file_name,
            thumb=thumb,
            caption=report,
            parse_mode=enums.ParseMode.HTML,
            reply_markup=buttons,
            progress=upload_progress,
            progress_args=(app, config.CHAT_ID, status, file_name),
        )

        try:
            await status.delete()
        except Exception:
            pass

    except Exception as exc:
        print(f"[TG] send failed: {exc}")
    finally:
        try:
            await app.stop()
        except Exception:
            pass


def print_summary(
    output:      Path,
    crf:         int,
    preset:      int,
    chunk_count: int,
) -> None:
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

def _parse_args():
    p = argparse.ArgumentParser(description="Merge + post-process encoded chunks")
    p.add_argument("--output",       default="encoded",      help="Output name without extension")
    p.add_argument("--enc-dir",      default="encoded-parts")
    p.add_argument("--expected",     type=int, default=None)
    p.add_argument("--crf",          type=int, default=50)
    p.add_argument("--preset",       type=int, default=4)
    p.add_argument("--grain",        type=int, default=0)
    p.add_argument("--crop",         default=None)
    p.add_argument("--hdr",          action="store_true")
    p.add_argument("--audio-bitrate",default="64k")
    p.add_argument("--run-vmaf",     action="store_true")
    p.add_argument("--run-upload",   action="store_true")
    p.add_argument("--encoder-title",default="MatrixEncodes")
    p.add_argument("--anime-name",   default="")
    p.add_argument("--season",       default="1")
    p.add_argument("--episode",      default="1")
    p.add_argument("--audio-type",   default="Auto")
    p.add_argument("--content-type", default="Anime")
    p.add_argument("--sub-tracks",   default="")
    p.add_argument("--audio-tracks", default="")
    p.add_argument("--duration",     type=float, default=0.0)
    p.add_argument("--width",        type=int,   default=0)
    p.add_argument("--height",       type=int,   default=0)
    p.add_argument("--fps",          type=float, default=24.0)
    p.add_argument("--params-file",  default=None,
                   help="Path to encode_params.json with video metadata")
    return p.parse_args()


if __name__ == "__main__":
    args    = _parse_args()
    enc_dir = Path(args.enc_dir)
    chunks  = sorted(enc_dir.glob("*-encoded.mkv"))
    output  = Path(f"{args.output}.mkv")

    merge(chunks, output, expected_count=args.expected)

    # Load video params from JSON if provided (GitHub Actions passes this)
    params: dict = {}
    if args.params_file and Path(args.params_file).exists():
        with open(args.params_file) as f:
            params = json.load(f)

    print_summary(output, args.crf, args.preset, len(chunks))

    # Run post-processing if TG creds are available or VMAF/upload requested
    import config
    run_vmaf   = args.run_vmaf   or config.RUN_VMAF
    run_upload = args.run_upload or config.RUN_UPLOAD
    has_tg     = bool(config.API_ID and config.BOT_TOKEN and config.CHAT_ID)

    if run_vmaf or run_upload or has_tg:
        source = Path("source.mkv")
        asyncio.run(_post_process(
            output        = output,
            source        = source,
            crf           = args.crf,
            preset        = args.preset,
            chunk_count   = len(chunks),
            run_vmaf      = run_vmaf,
            run_upload    = run_upload,
            encoder_title = args.encoder_title or config.ENCODER_TITLE,
            anime_name    = args.anime_name    or config.ANIME_NAME,
            season        = args.season        or config.SEASON,
            episode       = args.episode       or config.EPISODE,
            audio_type    = args.audio_type    or config.AUDIO_TYPE,
            content_type  = args.content_type  or config.CONTENT_TYPE,
            sub_tracks_lbl  = args.sub_tracks  or config.SUB_TRACKS,
            audio_tracks_lbl= args.audio_tracks or config.AUDIO_TRACKS,
            duration      = params.get("duration",  args.duration),
            width         = params.get("width",     args.width),
            height        = params.get("height",    args.height),
            fps_val       = params.get("fps_val",   args.fps),
            crop_val      = params.get("crop_val",  args.crop),
            is_hdr        = params.get("is_hdr",    args.hdr),
            grain         = params.get("grain",     args.grain),
            audio_bitrate = params.get("audio_bitrate", args.audio_bitrate),
        ))
