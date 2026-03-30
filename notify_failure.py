"""
notify_failure.py
Sends a structured failure notification to Telegram when the pipeline fails.
Reads env: TG_BOT_TOKEN, TG_CHAT_ID, DOWNLOAD_OUTCOME, ENCODE_OUTCOME,
           MERGE_OUTCOME, GITHUB_RUN_NUMBER, UI_TITLE
Attaches the relevant log file if present.
"""
import os
import json
import subprocess
from pathlib import Path

BOT_TOKEN        = os.environ.get("TG_BOT_TOKEN",       "")
CHAT_ID          = os.environ.get("TG_CHAT_ID",         "")
DOWNLOAD_OUTCOME = os.environ.get("DOWNLOAD_OUTCOME",   "")
ENCODE_OUTCOME   = os.environ.get("ENCODE_OUTCOME",     "")
MERGE_OUTCOME    = os.environ.get("MERGE_OUTCOME",      "")
RUN_NUMBER       = os.environ.get("GITHUB_RUN_NUMBER",  "?")
UI_TITLE         = os.environ.get("UI_TITLE",           "Unknown")
GITHUB_REPO      = os.environ.get("GITHUB_REPOSITORY",  "")
RUN_ID           = os.environ.get("GITHUB_RUN_ID",      "")

# Resolve file name
file_name = (
    Path("tg_fname.txt").read_text().strip()
    if Path("tg_fname.txt").exists()
    else UI_TITLE
)

# Determine which phase failed
if DOWNLOAD_OUTCOME == "failure":
    phase, log_file, icon = "DOWNLOAD", "download.log", "📥"
elif ENCODE_OUTCOME == "failure":
    phase, log_file, icon = "ENCODE",   "encode.log",   "⚙️"
elif MERGE_OUTCOME  == "failure":
    phase, log_file, icon = "MERGE",    "encode.log",   "🔗"
else:
    phase, log_file, icon = "UNKNOWN",  None,           "❌"

# Grab last 5 lines of log
log_path = Path(log_file) if log_file else None
if log_path and log_path.exists():
    lines   = log_path.read_text().splitlines()
    snippet = " ".join(lines[-5:])
else:
    snippet = "No log available."

log_url = f"https://github.com/{GITHUB_REPO}/actions/runs/{RUN_ID}" if GITHUB_REPO and RUN_ID else ""

message = (
    f"<code>"
    f"┌─── ⚠️ [ MATRIX.CRITICAL.FAILURE ] ───┐\n"
    f"│\n"
    f"│ 📂 FILE: {file_name}\n"
    f"│ {icon} PHASE: {phase} FAILED\n"
    f"│ 🔢 RUN: #{RUN_NUMBER}\n"
    f"│ ❌ ERROR:\n"
    f"│ {snippet[:180]}\n"
    f"│\n"
    f"│ 🛠️ STATUS: Core dumped.\n"
    f"└────────────────────────────────────┘"
    f"</code>"
)
if log_url:
    message += f'\n<a href="{log_url}">📋 Open Actions Log</a>'


def tg_send(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        return
    subprocess.run([
        "curl", "-s", "-X", "POST",
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        "-H", "Content-Type: application/json",
        "-d", json.dumps({"chat_id": CHAT_ID, "text": text,
                          "parse_mode": "HTML", "disable_web_page_preview": True}),
    ], check=False, stdout=subprocess.DEVNULL)


def tg_send_doc(filepath: str, caption: str):
    if not BOT_TOKEN or not CHAT_ID:
        return
    subprocess.run([
        "curl", "-s", "-X", "POST",
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument",
        "-F", f"chat_id={CHAT_ID}",
        "-F", f"document=@{filepath}",
        "-F", f"caption={caption}",
    ], check=False, stdout=subprocess.DEVNULL)


tg_send(message)

if log_path and log_path.exists():
    tg_send_doc(str(log_path), f"📋 {phase} log — Run #{RUN_NUMBER}")

print(f"✅ Failure notification sent for phase: {phase}")
