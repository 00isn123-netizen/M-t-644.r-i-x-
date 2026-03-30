"""
config.py — Centralized configuration for the Matrix AV1 Encoder.

All values are read from environment variables (GitHub Actions secrets/inputs),
falling back to safe defaults for local pipeline runs.
"""
import os

# ─── FILE PATHS ───────────────────────────────────────────────────────────────
SOURCE       = "source.mkv"
SCREENSHOT   = "grid_preview.jpg"
LOG_FILE     = "encode_log.txt"

# ─── TELEGRAM CREDENTIALS ─────────────────────────────────────────────────────
# Pyrogram-based (full upload + progress).  All optional — pipeline degrades
# gracefully to curl-only notifications if these are absent.
API_ID       = int(os.getenv("TG_API_ID",   "0").strip() or "0")
API_HASH     = os.getenv("TG_API_HASH",     "").strip()
BOT_TOKEN    = os.getenv("TG_BOT_TOKEN",    os.getenv("TG_BOT_TOKEN", "")).strip()
CHAT_ID_RAW  = os.getenv("TG_CHAT_ID",      "0").strip()
CHAT_ID      = int(CHAT_ID_RAW) if CHAT_ID_RAW.lstrip("-").isdigit() else 0
SESSION_NAME = os.getenv("SESSION_NAME",    "enc_session")
FILE_NAME    = os.getenv("FILE_NAME",       "output.mkv")

# ─── GITHUB RUN INFO ──────────────────────────────────────────────────────────
GITHUB_RUN_ID     = os.getenv("GITHUB_RUN_ID",     "local")
GITHUB_RUN_NUMBER = os.getenv("GITHUB_RUN_NUMBER", "?")
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY", "")

# ─── ENCODER SETTINGS ─────────────────────────────────────────────────────────
USER_CRF     = os.getenv("USER_CRF",     "").strip()   # e.g. "50"
USER_PRESET  = os.getenv("USER_PRESET",  "").strip()   # e.g. "4"
USER_RES     = os.getenv("USER_RES",     "").strip()   # e.g. "1080"
USER_GRAIN   = os.getenv("USER_GRAIN",   "0").strip()  # 0–50
AUDIO_MODE   = os.getenv("AUDIO_MODE",   "opus").strip()
AUDIO_BITRATE= os.getenv("AUDIO_BITRATE","64k").strip()

# ─── POST-ENCODE TOGGLES ──────────────────────────────────────────────────────
RUN_VMAF   = os.getenv("RUN_VMAF",   "false").lower() == "true"
RUN_UPLOAD = os.getenv("RUN_UPLOAD", "false").lower() == "true"

# ─── ANIME RENAME SETTINGS ────────────────────────────────────────────────────
ANIME_NAME   = os.getenv("ANIME_NAME",   "").strip()
SEASON       = os.getenv("SEASON",       "1").strip()
EPISODE      = os.getenv("EPISODE",      "1").strip()
AUDIO_TYPE   = os.getenv("AUDIO_TYPE",   "Auto").strip()   # Auto|Sub|Dual|Tri|Multi
CONTENT_TYPE = os.getenv("CONTENT_TYPE", "Anime").strip()  # Anime|Donghua|Hentai|HMV|AMV|custom
SUB_TRACKS   = os.getenv("SUB_TRACKS",   "").strip()       # "English, Arabic"
AUDIO_TRACKS = os.getenv("AUDIO_TRACKS", "").strip()       # "Japanese, English (Dub)"

# ─── ENCODER BRANDING ─────────────────────────────────────────────────────────
# Sets the MKV Title tag on every output file.
ENCODER_TITLE = os.getenv("ENCODER_TITLE", "MatrixEncodes").strip()

# ─── DEMO / PARTIAL ENCODE ────────────────────────────────────────────────────
# Set DEMO_DURATION (seconds) to encode only a slice of the source.
DEMO_START    = os.getenv("DEMO_START",    "0").strip()
DEMO_DURATION = os.getenv("DEMO_DURATION", "").strip()   # blank = full encode

# ─── CHUNK CONFIG ─────────────────────────────────────────────────────────────
CHUNK_COUNT = int(os.getenv("CHUNK_COUNT", "10"))
WORKERS     = int(os.getenv("WORKERS",     str(__import__("os").cpu_count() or 4)))
