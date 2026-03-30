"""
media.py — Media analysis and cloud helpers for the Matrix AV1 Encoder.

Provides:
  get_video_info()        — ffprobe metadata extraction
  get_crop_params()       — multi-point black-bar detection
  async_generate_thumbnail() — grab a frame for the TG report
  get_vmaf()              — VMAF + SSIM quality scoring
  upload_to_cloud()       — Gofile (primary) + Litterbox (fallback)
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from collections import Counter

import config


# ─── Video metadata ───────────────────────────────────────────────────────────

def get_video_info() -> tuple[float, int, int, bool, int, int, float]:
    """
    Returns (duration, width, height, is_hdr, total_frames, channels, fps).
    """
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_streams", "-show_format",
        config.SOURCE,
    ]
    res          = json.loads(subprocess.check_output(cmd).decode())
    video_stream = next(s for s in res["streams"] if s["codec_type"] == "video")
    audio_stream = next((s for s in res["streams"] if s["codec_type"] == "audio"), {})

    channels = int(audio_stream.get("channels", 0))
    duration = float(res["format"].get("duration", 0))
    width    = int(video_stream.get("width",  0))
    height   = int(video_stream.get("height", 0))

    fps_raw = video_stream.get("r_frame_rate", "24/1")
    try:
        if "/" in fps_raw:
            num, den = fps_raw.split("/")
            fps_val  = int(num) / int(den)
        else:
            fps_val  = float(fps_raw)
    except (ValueError, ZeroDivisionError):
        fps_val = 24.0

    total_frames = int(
        round(float(video_stream.get("nb_frames") or 0)
              or duration * fps_val)
    )
    is_hdr = "bt2020" in video_stream.get("color_primaries", "bt709")
    return duration, width, height, is_hdr, total_frames, channels, fps_val


# ─── Crop detection ───────────────────────────────────────────────────────────

def get_crop_params(duration: float) -> str | None:
    """
    Sample 6 points across the source, run cropdetect, return the most
    consistent crop string (e.g. '1920:800:0:140') or None.
    """
    if duration < 10:
        return None

    test_points    = [duration * p for p in (0.05, 0.20, 0.40, 0.60, 0.80, 0.90)]
    detected_crops = []

    for ts in test_points:
        time_str = time.strftime("%H:%M:%S", time.gmtime(ts))
        cmd = [
            "ffmpeg", "-skip_frame", "nokey",
            "-ss", time_str,
            "-i", config.SOURCE,
            "-vframes", "20",
            "-vf", "cropdetect=limit=24:round=2",
            "-f", "null", "-",
        ]
        try:
            res       = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            found     = [
                line.split("crop=")[1].split(" ")[0]
                for line in res.stderr.split("\n")
                if "crop=" in line
            ]
            if found:
                detected_crops.append(Counter(found).most_common(1)[0][0])
        except Exception:
            continue

    if not detected_crops:
        return None

    most_common, count = Counter(detected_crops).most_common(1)[0]
    if count < 4:
        return None

    w, h, x, y = most_common.split(":")
    if int(x) == 0 and int(y) == 0:
        return None   # no bars — skip

    return most_common


# ─── Thumbnail ────────────────────────────────────────────────────────────────

async def async_generate_thumbnail(duration: float, target_file: str) -> None:
    """Capture a frame at 25% of duration and save as JPEG grid_preview.jpg."""
    loop = asyncio.get_event_loop()

    def sync_thumbnail():
        ts  = duration * 0.25
        cmd = [
            "ffmpeg", "-ss", str(ts), "-i", target_file,
            "-vf", "scale=1280:-1",
            "-frames:v", "1", "-q:v", "3",
            config.SCREENSHOT, "-y",
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    await loop.run_in_executor(None, sync_thumbnail)


# ─── VMAF + SSIM ─────────────────────────────────────────────────────────────

async def get_vmaf(
    output_file:  str,
    crop_val:     str | None,
    width:        int,
    height:       int,
    duration:     float,
    fps:          float,
    kv_writer=None,
) -> tuple[str, str]:
    """
    Run VMAF + SSIM analysis on *output_file* vs *config.SOURCE*.
    Returns (vmaf_score, ssim_score) as strings, or ("N/A", "N/A") on failure.

    kv_writer: optional async callable(dict) for live TG progress updates.
    """
    ref_w, ref_h = width, height
    if crop_val:
        try:
            parts        = crop_val.split(":")
            ref_w, ref_h = parts[0], parts[1]
        except Exception:
            pass

    interval     = duration / 6
    select_parts = [
        f"between(t,{(i * interval) + (interval / 2) - 2.5},{(i * interval) + (interval / 2) + 2.5})"
        for i in range(6)
    ]
    select_filter     = f"select='{'+'.join(select_parts)}',setpts=N/FRAME_RATE/TB"
    total_vmaf_frames = int(30 * fps)
    ref_filters       = f"crop={crop_val},{select_filter}" if crop_val else select_filter
    dist_filters      = f"{select_filter},scale={ref_w}:{ref_h}:flags=bicubic"

    filter_graph = (
        f"[1:v]{ref_filters}[r];"
        f"[0:v]{dist_filters}[d];"
        f"[d]split=2[d1][d2];"
        f"[r]split=2[r1][r2];"
        f"[d1][r1]libvmaf;"
        f"[d2][r2]ssim"
    )

    cmd = [
        "ffmpeg", "-threads", "0",
        "-i", output_file, "-i", config.SOURCE,
        "-filter_complex", filter_graph,
        "-progress", "pipe:1", "-nostats",
        "-f", "null", "-",
    ]

    vmaf_score = ssim_score = "N/A"

    try:
        proc       = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        start_time = time.time()
        last_write = 0.0

        async def read_progress():
            nonlocal last_write
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                line_str = line.decode().strip()
                if line_str.startswith("frame="):
                    try:
                        curr_frame = int(line_str.split("=")[1].strip())
                        percent    = min(100.0, (curr_frame / total_vmaf_frames) * 100)
                        now        = time.time()
                        if kv_writer and (now - last_write > 5):
                            elapsed = now - start_time
                            speed   = curr_frame / elapsed if elapsed > 0 else 0
                            eta     = ((total_vmaf_frames - curr_frame) / speed
                                       if speed > 0 else 0)
                            await kv_writer({
                                "phase":        "vmaf",
                                "file":         output_file,
                                "run_id":       config.GITHUB_RUN_ID,
                                "vmaf_percent": round(percent, 1),
                                "fps":          int(speed),
                                "elapsed":      int(elapsed),
                                "eta":          int(eta),
                                "ts":           int(now),
                            })
                            last_write = now
                    except Exception:
                        pass

        async def read_stderr():
            nonlocal vmaf_score, ssim_score
            while True:
                line     = await proc.stderr.readline()
                if not line:
                    break
                line_str = line.decode("utf-8", errors="ignore").strip()
                if "VMAF score:" in line_str:
                    vmaf_score = line_str.split("VMAF score:")[1].strip()
                if "SSIM Y:" in line_str and "All:" in line_str:
                    try:
                        ssim_score = line_str.split("All:")[1].split(" ")[0]
                    except Exception:
                        pass

        await asyncio.gather(read_progress(), read_stderr())
        await proc.wait()

    except Exception as exc:
        print(f"[vmaf] error: {exc}")

    return vmaf_score, ssim_score


# ─── Cloud upload ─────────────────────────────────────────────────────────────

async def upload_to_cloud(
    filepath:   str,
    app=None,
    chat_id:    int = 0,
    status_msg=None,
) -> dict:
    """
    Upload *filepath* to Gofile (primary) or Litterbox (fallback).
    Returns {"direct": url, "page": url, "source": "gofile"|"litterbox"|"error"}.
    """
    filename  = os.path.basename(filepath)

    # Step 1: best Gofile server
    try:
        sp = await asyncio.create_subprocess_exec(
            "curl", "-s", "https://api.gofile.io/servers",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        out, _ = await sp.communicate()
        data   = json.loads(out.decode())
        if data.get("status") != "ok":
            raise ValueError(f"Gofile server API error: {data}")
        server = data["data"]["servers"][0]["name"]
    except Exception as e:
        print(f"[Gofile] server lookup failed: {e}")
        return await _litterbox_fallback(filepath)

    # Step 2: upload
    try:
        file_size   = os.path.getsize(filepath)
        up_proc     = await asyncio.create_subprocess_exec(
            "curl", "-s", "--progress-bar",
            "-F", f"file=@{filepath}",
            f"https://{server}.gofile.io/contents/uploadfile",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

        async def _drain_progress():
            async for _ in up_proc.stderr:
                pass

        await asyncio.gather(_drain_progress(), asyncio.shield(up_proc.wait()))
        upload_out  = await up_proc.stdout.read()
        upload_data = json.loads(upload_out.decode())

        if upload_data.get("status") != "ok":
            raise ValueError(f"Gofile upload error: {upload_data}")

        page_url = upload_data["data"]["downloadPage"]
        return {"direct": page_url, "page": page_url, "source": "gofile"}

    except Exception as e:
        print(f"[Gofile] upload failed: {e}")
        return await _litterbox_fallback(filepath)


async def _litterbox_fallback(filepath: str) -> dict:
    """Fallback: litterbox.catbox.moe — no size cap under 1 GB."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "curl", "-s",
            "-F", "reqtype=fileupload",
            "-F", "time=72h",
            "-F", f"fileToUpload=@{filepath}",
            "https://litterbox.catbox.moe/resources/internals/api.php",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        url       = stdout.decode().strip()
        if url.startswith("https://"):
            return {"direct": url, "page": url, "source": "litterbox"}
    except Exception as e:
        print(f"[Litterbox] fallback failed: {e}")

    return {"direct": None, "page": None, "source": "error"}
