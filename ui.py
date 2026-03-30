"""
ui.py — Rich Telegram UI panels for the Matrix AV1 Encoder.
Provides formatted HTML blocks for encode progress, download, upload,
VMAF analysis, failures, and cancellation.

Pyrogram is used for upload_progress; if not installed the helper is
still importable — callers guard with try/except.
"""
import time
from datetime import timedelta

# ─── Progress bar ─────────────────────────────────────────────────────────────

def generate_progress_bar(percentage: float, width: int = 15) -> str:
    filled = int((max(0.0, min(100.0, percentage)) / 100) * width)
    return "[" + "▰" * filled + "▱" * (width - filled) + "]"

def format_time(seconds: float) -> str:
    return str(timedelta(seconds=int(seconds))).zfill(8)

# ─── Panel builders ───────────────────────────────────────────────────────────

def get_encode_ui(
    file_name, speed, fps, elapsed, eta,
    curr_sec, duration, percent,
    final_crf, final_preset, res_label,
    crop_label, hdr_label, grain_label,
    u_audio, u_bitrate, size,
    cpu=None, ram=None, demo_label="",
    chunk_info="",
) -> str:
    bar      = generate_progress_bar(percent)
    sys_line = (f"│ 🖥️ SYSTEM: CPU {cpu:.1f}% | RAM {ram:.1f}%\n"
                if cpu is not None and ram is not None else "")
    demo_line  = f"│ ⚡ DEMO:{demo_label}\n" if demo_label else ""
    chunk_line = f"│ 🧩 CHUNKS:{chunk_info}\n" if chunk_info else ""
    est_final  = (size / percent * 100) if percent > 1 else 0
    size_line  = (
        f"│ 📦 SIZE: {size:.2f} MB → ~{est_final:.1f} MB est\n"
        if percent > 1
        else f"│ 📦 SIZE: {size:.2f} MB\n"
    )
    return (
        "<code>"
        "┌─── 🛰️ [ MATRIX.ENCODE.PROCESS ] ──────┐\n"
        "│                                    \n"
        f"│ 📂 FILE: {file_name}\n"
        f"│ ⚡ SPEED: {speed:.1f}x ({int(fps)} FPS)\n"
        f"│ ⏳ TIME: {format_time(elapsed)} / ETA: {format_time(eta)}\n"
        f"│ 🕒 DONE: {format_time(curr_sec)} / {format_time(duration)}\n"
        "│                                    \n"
        f"│ 📊 PROG: {bar} {percent:.1f}%\n"
        "│                                    \n"
        f"│ 🛠️ SETTINGS: CRF {final_crf} | Preset {final_preset}\n"
        f"│ 🎞️ VIDEO: {res_label}{crop_label} | 10-bit | {hdr_label}{grain_label}\n"
        f"│ 🔊 AUDIO: {u_audio.upper()} @ {u_bitrate}\n"
        + size_line
        + chunk_line
        + "│                                    \n"
        + demo_line
        + sys_line
        + "└────────────────────────────────────┘</code>"
    )


def get_download_ui(percent: float, speed: float, size_mb: float,
                    elapsed: float, eta: float) -> str:
    bar = generate_progress_bar(percent)
    return (
        f"<code>┌─── 🛰️ [ MATRIX.DOWNLOAD.ACTIVE ] ───┐\n"
        f"│                                    \n"
        f"│ 📥 STATUS: Fetching source         \n"
        f"│ 📊 PROG: {bar} {percent:.1f}%\n"
        f"│ ⚡ SPEED: {speed:.2f} MB/s\n"
        f"│ 📦 SIZE: {size_mb:.2f} MB\n"
        f"│ ⏳ TIME: {format_time(elapsed)} / {format_time(eta)}\n"
        f"│                                    \n"
        f"└────────────────────────────────────┘</code>"
    )


def get_vmaf_ui(percent: float, speed: float, eta: float) -> str:
    bar = generate_progress_bar(percent)
    return (
        f"<code>┌─── 🧠 [ MATRIX.ANALYSIS ] ─────────┐\n"
        f"│                                    \n"
        f"│ 🔬 METRICS: VMAF + SSIM\n"
        f"│ 📊 PROG: {bar} {percent:.1f}%\n"
        f"│ ⚡ SPEED: {speed:.1f} FPS\n"
        f"│ ⏳ ETA: {format_time(eta)}\n"
        f"│                                    \n"
        f"└────────────────────────────────────┘</code>"
    )


def get_failure_ui(file_name: str, error_snippet: str, phase: str = "ENCODE") -> str:
    phase_icons = {"DOWNLOAD": "📥", "ENCODE": "⚙️", "UPLOAD": "☁️", "MERGE": "🔗"}
    icon = phase_icons.get(phase.upper(), "❌")
    return (
        f"<code>┌─── ⚠️ [ MATRIX.CRITICAL.FAILURE ] ───┐\n"
        f"│                                    \n"
        f"│ 📂 FILE: {file_name}\n"
        f"│ {icon} PHASE: {phase.upper()} FAILED\n"
        f"│ ❌ ERROR DETECTED:\n"
        f"│ {error_snippet[:200]}\n"
        f"│                                    \n"
        f"│ 🛠️ STATUS: Core dumped.\n"
        f"│ 📑 Check attached log for details.\n"
        f"└────────────────────────────────────┘</code>"
    )


def get_cancelled_ui(file_name: str, elapsed_str: str) -> str:
    return (
        f"<code>┌─── 🛑 [ MATRIX.MISSION.CANCELLED ] ───┐\n"
        f"│                                    \n"
        f"│ 📂 FILE: {file_name}\n"
        f"│ ⏱ ELAPSED: {elapsed_str}\n"
        f"│                                    \n"
        f"│ 🚫 STATUS: Encode aborted by user.\n"
        f"└────────────────────────────────────┘</code>"
    )


def get_download_fail_ui(error_msg: str) -> str:
    return (
        f"<code>┌─── ❌ [ MATRIX.DOWNLOAD.FAILED ] ───┐\n"
        f"│                                    \n"
        f"│ ❌ ERROR: {error_msg}\n"
        f"│ 🛠️ STATUS: Downlink terminated.\n"
        f"│                                    \n"
        f"└────────────────────────────────────┘</code>"
    )


# ─── Upload progress (Pyrogram callback) ──────────────────────────────────────

last_up_update: float = 0
last_up_pct:    float = -1
up_start_time:  float = 0


async def upload_progress(current, total, app, chat_id, status_msg, file_name):
    """Pyrogram upload progress callback — updates every 8 seconds."""
    global last_up_update
    now = time.time()
    if now - last_up_update < 8:
        return
    percent = (current / total) * 100 if total else 0
    bar     = generate_progress_bar(percent)
    cur_mb  = current / 1_048_576
    tot_mb  = total   / 1_048_576
    ui = (
        f"<code>┌─── 🛰️ [ MATRIX.UPLINK.ACTIVE ] ────┐\n"
        f"│                                    \n"
        f"│ 📂 FILE: {file_name}\n"
        f"│ 📊 PROG: {bar} {percent:.1f}%\n"
        f"│ 📦 SIZE: {cur_mb:.2f} / {tot_mb:.2f} MB\n"
        f"│ 📡 STATUS: Transmitting to Orbit...\n"
        f"│                                    \n"
        f"└────────────────────────────────────┘</code>"
    )
    try:
        from pyrogram import enums as _enums
        await app.edit_message_text(
            chat_id, status_msg.id, ui,
            parse_mode=_enums.ParseMode.HTML
        )
    except Exception:
        pass
    last_up_update = now
