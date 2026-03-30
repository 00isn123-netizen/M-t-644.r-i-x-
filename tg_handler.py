"""
tg_handler.py — Download source files directly from Telegram via Pyrogram.

Supports:
  tg_file:<file_id>|<filename>   — direct file_id download
  https://t.me/...               — message link download

Writes:
  source.mkv    — downloaded file
  tg_fname.txt  — original filename for rename step
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import traceback

from ui import get_download_ui


async def _progress(current, total, app, chat_id, message, start_time):
    if total <= 0:
        return
    if not hasattr(_progress, "last_pct"):
        _progress.last_pct = -1

    percent   = (current / total) * 100
    milestone = int(percent // 5) * 5

    if milestone <= _progress.last_pct:
        return
    _progress.last_pct = milestone

    elapsed    = time.time() - start_time
    speed_mb   = (current / elapsed / 1_048_576) if elapsed > 0 else 0
    size_mb    = total / 1_048_576
    eta        = (total - current) / (current / elapsed) if current > 0 and elapsed > 0 else 0
    ui_text    = get_download_ui(percent, speed_mb, size_mb, elapsed, eta)

    try:
        from pyrogram import enums
        from pyrogram.errors import FloodWait
        await app.edit_message_text(
            chat_id, message.id, ui_text,
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception:
        pass


async def main():
    try:
        from pyrogram import Client, enums
        from pyrogram.errors import FloodWait
    except ImportError:
        print("❌ pyrogram not installed — cannot download from Telegram.")
        sys.exit(1)

    api_id    = int(os.environ.get("TG_API_ID",   "0").strip())
    api_hash  = os.environ.get("TG_API_HASH",      "").strip()
    bot_token = os.environ.get("TG_BOT_TOKEN",     "").strip()
    chat_id   = int(os.environ.get("TG_CHAT_ID",   "0").strip())
    url       = os.environ.get("VIDEO_URL",         "").strip()

    if not api_id or not bot_token:
        print("❌ TG_API_ID / TG_BOT_TOKEN not set.")
        sys.exit(1)

    # Lane-based session
    _ALL_LANES  = [chr(ord("A") + i) for i in range(20)]
    run_number  = int(os.environ.get("GITHUB_RUN_NUMBER", "0"))
    lane        = _ALL_LANES[run_number % 20]
    print(f"Session lane: {lane} (run #{run_number})")

    session_dir  = "tg_session_dir"
    os.makedirs(session_dir, exist_ok=True)
    session_path = os.path.join(session_dir, f"tg_dl_session_{lane}")

    app = Client(session_path, api_id=api_id, api_hash=api_hash, bot_token=bot_token)

    for attempt in range(5):
        try:
            await app.start()
            break
        except FloodWait as e:
            print(f"⏳ FloodWait {e.value}s (attempt {attempt + 1}/5)")
            await asyncio.sleep(e.value + 5)
    else:
        print("❌ Could not authenticate with Telegram after 5 attempts.")
        sys.exit(1)

    try:
        status = await app.send_message(
            chat_id,
            "📡 <b>[ MATRIX.DOWNLOAD ] Establishing Downlink...</b>",
            parse_mode=enums.ParseMode.HTML,
        )

        start_time = time.time()
        final_name = "video.mkv"
        _progress.last_pct = -1

        if "t.me/" in url:
            parts = url.rstrip("/").split("/")
            try:
                msg_id = int(parts[-1].split("?")[0])
            except (ValueError, IndexError):
                print("❌ Could not parse message ID from link.")
                sys.exit(1)

            target_chat = (
                int(f"-100{parts[-2]}")
                if len(parts) >= 4 and parts[-3] == "c"
                else parts[-2]
            )
            msg   = await app.get_messages(target_chat, msg_id)
            if not msg or not msg.media:
                await app.edit_message_text(
                    chat_id, status.id,
                    "❌ <b>No media found in link.</b>",
                    parse_mode=enums.ParseMode.HTML,
                )
                sys.exit(1)

            media      = msg.video or msg.document or msg.audio
            final_name = getattr(media, "file_name", "video.mkv")
            await app.download_media(
                msg, file_name="./source.mkv",
                progress=_progress,
                progress_args=(app, chat_id, status, start_time),
            )

        elif "tg_file:" in url:
            raw_data = url.replace("tg_file:", "")
            if "|" in raw_data:
                file_id, final_name = raw_data.split("|", 1)
            else:
                file_id = raw_data
            await app.download_media(
                message=file_id.strip(), file_name="./source.mkv",
                progress=_progress,
                progress_args=(app, chat_id, status, start_time),
            )
        else:
            await app.edit_message_text(
                chat_id, status.id,
                "❌ <b>Unsupported URL format for tg_handler.</b>",
                parse_mode=enums.ParseMode.HTML,
            )
            sys.exit(1)

        await app.edit_message_text(
            chat_id, status.id,
            "✅ <b>[ DOWNLOAD.COMPLETE ] Transferring to Encoder...</b>",
            parse_mode=enums.ParseMode.HTML,
        )

        with open("tg_fname.txt", "w", encoding="utf-8") as f:
            f.write(final_name)
        print(f"📝 tg_fname.txt → {final_name}")

    except Exception as e:
        print(f"FATAL: {e}")
        traceback.print_exc()
        sys.exit(1)
    finally:
        await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
