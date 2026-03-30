"""
tg_utils.py — Shared Telegram helpers for the Matrix AV1 Encoder.

Provides robust Pyrogram session management with:
  - 20-lane session rotation (A–T) keyed on GITHUB_RUN_NUMBER % 20
  - FloodWait handling: skip to next session, retry shortest-wait session
  - connect_telegram()    — background auth + status message
  - tg_edit()             — safe edit with flood guard
  - tg_notify_failure()   — failure card + log attachment

Pyrogram is an optional dependency — if not installed every exported
function becomes a graceful no-op so the pipeline still runs locally.
"""
from __future__ import annotations

import asyncio
import os

import config
from ui import get_failure_ui

# ── Lane helpers ──────────────────────────────────────────────────────────────
ALL_LANES = [chr(ord("A") + i) for i in range(20)]   # ["A", …, "T"]


def _resolve_lane(run_number: int) -> str:
    return ALL_LANES[run_number % 20]


def _resolve_session_names() -> list[str]:
    """
    Return session names to try, most-preferred first.
    Own lane first, then cross-lane fallbacks, then legacy bare session.
    """
    run_number = int(os.environ.get("GITHUB_RUN_NUMBER", "0"))
    lane       = _resolve_lane(run_number)
    print(f"TG session lane: {lane} (run #{run_number})")

    others = [l for l in ALL_LANES if l != lane]
    names  = [
        f"tg_session_dir/enc_session_{lane}",
        f"tg_session_dir/tg_dl_session_{lane}",
    ]
    for other in others:
        names += [
            f"tg_session_dir/enc_session_{other}",
            f"tg_session_dir/tg_dl_session_{other}",
        ]
    names.append(config.SESSION_NAME)
    return names


# ── Auth ──────────────────────────────────────────────────────────────────────
async def connect_telegram(tg_state: dict, tg_ready: asyncio.Event, label: str):
    """
    Try each session in priority order.  FloodWait on a session → skip to
    next.  Sets tg_state['app'] and tg_state['status'] on success.
    """
    if not config.API_ID or not config.BOT_TOKEN:
        print("TG auth skipped — credentials not set.")
        return

    try:
        from pyrogram import Client, enums
        from pyrogram.errors import FloodWait
    except ImportError:
        print("pyrogram not installed — TG disabled.")
        return

    flood_waits: dict[str, int] = {}
    app = None

    for session_name in _resolve_session_names():
        try:
            candidate = Client(
                session_name,
                api_id=config.API_ID,
                api_hash=config.API_HASH,
                bot_token=config.BOT_TOKEN,
            )
            await candidate.start()
            app = candidate
            print(f"TG auth OK: {session_name}")
            break
        except FloodWait as e:
            flood_waits[session_name] = e.value
            print(f"FloodWait {e.value}s on '{session_name}' — trying next…")
        except Exception as e:
            print(f"TG auth error on '{session_name}': {e} — trying next…")

    # All sessions flooded — sleep shortest wait and retry
    if app is None and flood_waits:
        best   = min(flood_waits, key=flood_waits.get)
        waitsec = flood_waits[best]
        attempt = 0
        while True:
            attempt += 1
            print(f"All flooded. Sleeping {waitsec}s (attempt {attempt})…")
            await asyncio.sleep(waitsec + 5)
            try:
                candidate = Client(
                    best,
                    api_id=config.API_ID,
                    api_hash=config.API_HASH,
                    bot_token=config.BOT_TOKEN,
                )
                await candidate.start()
                app = candidate
                print(f"TG auth OK post-flood: {best}")
                break
            except FloodWait as e:
                waitsec = e.value
            except Exception as e:
                print(f"TG post-flood attempt {attempt} failed: {e}")
                return

    if app is None:
        print("TG auth failed — no usable session.")
        return

    try:
        status = await app.send_message(
            config.CHAT_ID,
            f"<b>[ MATRIX ONLINE ] {label}</b>",
            parse_mode=enums.ParseMode.HTML,
        )
    except FloodWait as e:
        await asyncio.sleep(e.value)
        status = await app.send_message(
            config.CHAT_ID,
            f"<b>[ MATRIX ONLINE ] {label}</b>",
            parse_mode=enums.ParseMode.HTML,
        )

    tg_state["app"]    = app
    tg_state["status"] = status
    tg_ready.set()
    print("Telegram connected.")


# ── Safe edit ─────────────────────────────────────────────────────────────────
async def tg_edit(
    tg_state: dict,
    tg_ready: asyncio.Event,
    text: str,
    reply_markup=None,
):
    if not tg_ready.is_set():
        return
    app    = tg_state.get("app")
    status = tg_state.get("status")
    if not app or not status:
        return
    try:
        from pyrogram import enums
        from pyrogram.errors import FloodWait
        kwargs: dict = dict(parse_mode=enums.ParseMode.HTML)
        if reply_markup:
            kwargs["reply_markup"] = reply_markup
        await app.edit_message_text(config.CHAT_ID, status.id, text, **kwargs)
    except Exception:
        try:
            from pyrogram.errors import FloodWait
            raise
        except Exception:
            pass


# ── Failure notifier ──────────────────────────────────────────────────────────
async def tg_notify_failure(
    tg_state: dict,
    tg_ready: asyncio.Event,
    file_name: str,
    reason: str,
    phase: str = "ENCODE",
):
    app    = tg_state.get("app")
    status = tg_state.get("status")
    if not app or not status:
        print(f"[TG-FAIL] TG unavailable — reason: {reason}")
        return
    try:
        from pyrogram import enums
        await app.edit_message_text(
            config.CHAT_ID, status.id,
            get_failure_ui(file_name, reason, phase),
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception as e:
        print(f"[TG-FAIL] edit failed: {e}")

    if config.LOG_FILE and os.path.exists(config.LOG_FILE):
        try:
            await app.send_document(
                config.CHAT_ID, config.LOG_FILE,
                caption="<b>FULL MATRIX LOG</b>",
                parse_mode=enums.ParseMode.HTML,
            )
        except Exception as e:
            print(f"[TG-FAIL] log send failed: {e}")
