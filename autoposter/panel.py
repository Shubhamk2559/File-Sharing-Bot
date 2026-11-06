"""HelpBot <-> Auto Posting glue.

Auto Posting ab alag bot nahi hai: HelpBot ke main (manager) bot ke andar chalta hai.
- Control Center me "🤖 Auto Posting" button -> `ap:nav:dashboard`. Is panel ke saare buttons `ap:` prefix se aate hain.
- Owner jab upload / add-target / footer jaise "mode" me hota hai (DB me `admins.mode`), tab uske private messages
  yahan handle hote hain. HelpBot ka koi bhi menu button ya /start us mode ko band kar deta hai, taaki HelpBot ka
  apna file-upload kabhi galti se Auto Posting me na chala jaye.
- Scheduler / housekeeping / daily backup JobQueue se nahi, plain asyncio loops se chalte hain
  (HelpBot ka webhook Application JobQueue ke bina bana hai).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from types import SimpleNamespace

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import handlers, jobs
from .config import SETTINGS, TZ
from .database import Database
from .state import STATE
from .ui import CB_PREFIX

log = logging.getLogger("autoposter.panel")

# HelpBot ke flows jo context.user_data me rehte hain (handlers.AWAIT_KEY / POST_KEY / BULK_KEY, ubot_ui.KEY)
HELPBOT_FLOW_KEYS = ("await", "post", "bulk")
# Main bot ko ye updates chahiye (manager.start webhook me allowed_updates me daalta hai)
EXTRA_ALLOWED_UPDATES = ["my_chat_member", "chat_join_request"]

_admin_filter = filters.User(user_id=list(SETTINGS.admin_ids))
_owner_private = _admin_filter & filters.ChatType.PRIVATE
_mode_maybe_set = True  # restart ke baad DB me purana mode ho sakta hai, isliye pehli baar zaroor check


def enabled() -> bool:
    return STATE.db is not None


# ===================== lifecycle =====================
async def start() -> bool:
    """DB banao (running event loop me) aur index/migration chalao. Fail ho to HelpBot chalta rahe."""
    if STATE.db is not None:
        return True
    db = Database(SETTINGS)
    last: Exception | None = None
    for attempt in range(1, 4):
        try:
            await db.init()
            STATE.db = db
            await db.log("info", "autoposter_started", db=SETTINGS.mongo_db, tz=SETTINGS.timezone)
            return True
        except Exception as exc:  # noqa: BLE001 - Mongo abhi jag raha ho sakta hai
            last = exc
            log.warning("Auto Posting DB not ready (attempt %s/3): %s", attempt, exc)
            await asyncio.sleep(2 * attempt)
    log.error("Auto Posting disabled: could not init MongoDB (%s)", last)
    db.client.close()
    return False


async def shutdown() -> None:
    db, STATE.db = STATE.db, None
    if db is not None:
        try:
            await db.log("info", "autoposter_shutdown")
        except Exception:  # noqa: BLE001
            pass
        db.client.close()


async def clear_mode() -> None:
    """HelpBot ke kisi bhi menu button / /start par: Auto Posting ka koi bhi pending mode (upload, footer...) band."""
    global _mode_maybe_set
    if STATE.db is None or not _mode_maybe_set:
        return
    _mode_maybe_set = False
    try:
        await STATE.db.admins.update_many(
            {"admin_id": {"$in": list(SETTINGS.admin_ids)}, "mode": {"$exists": True}},
            {"$unset": {"mode": "", "mode_chat_id": "", "mode_extra": "", "pending_action": "", "password_deadline": ""}},
        )
    except Exception:  # noqa: BLE001 - menu kabhi isse na ruke
        _mode_maybe_set = True
        log.exception("clear_mode failed")


def _reset_helpbot_flows(context: ContextTypes.DEFAULT_TYPE) -> None:
    for key in HELPBOT_FLOW_KEYS:
        context.user_data.pop(key, None)
    import ubot_ui  # noqa: PLC0415 - lazy: circular import se bachne ke liye

    ubot_ui.clear(context)


# ===================== handlers =====================
async def ap_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global _mode_maybe_set
    query = update.callback_query
    if STATE.db is None:
        await query.answer("Auto Posting abhi start nahi hua (MongoDB check karo).", show_alert=True)
        return
    _mode_maybe_set = True
    _reset_helpbot_flows(context)
    await handlers.handle_callback(update, context)


async def ap_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner ka private message: sirf tab Auto Posting ko jab koi Auto Posting mode active ho."""
    if STATE.db is None or "ub" in context.user_data:  # Copy Chats ka login/search chal raha hai
        return
    user = update.effective_user
    if not user or not update.effective_message:
        return
    state = await handlers.admin_doc(user.id)
    if state.get("mode") not in handlers.KNOWN_MODES:
        return  # koi mode nahi -> HelpBot ka normal handler (file upload / token / menu)
    await handlers.private_admin_message(update, context)
    raise ApplicationHandlerStop


def _make_cancel(on_cancel):
    async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        global _mode_maybe_set
        _mode_maybe_set = True
        await clear_mode()
        _reset_helpbot_flows(context)
        if on_cancel:
            await on_cancel(update, context)
        else:
            await update.effective_message.reply_text("❌ Cancelled.")

    return cancel


async def done_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if STATE.db is None:
        return
    await handlers.done_cmd(update, context)


async def register_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if STATE.db is None:
        return
    await handlers.register_current_chat(update, context)


async def my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if STATE.db is not None:
        await handlers.my_chat_member_handler(update, context)


async def join_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if STATE.db is not None:
        await handlers.handle_join_request(update, context)


async def reaction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if STATE.db is not None:
        await handlers.handle_reaction(update, context)


def register(app: Application, on_cancel=None) -> None:
    """HelpBot ke register_main_handlers me, owner ke generic /command handler se PEHLE call karo."""
    app.add_handler(MessageHandler(_owner_private & filters.UpdateType.MESSAGE & ~filters.COMMAND, ap_message), group=-2)
    app.add_handler(CommandHandler("cancel", _make_cancel(on_cancel), filters=_admin_filter))
    app.add_handler(CommandHandler("done", done_cmd, filters=_admin_filter))
    app.add_handler(CommandHandler("register", register_cmd, filters=_admin_filter))
    app.add_handler(CallbackQueryHandler(ap_callback, pattern=rf"^{CB_PREFIX}"))
    app.add_handler(CallbackQueryHandler(reaction, pattern=r"^react:\d+$"))
    app.add_handler(ChatMemberHandler(my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(ChatJoinRequestHandler(join_request))
    app.add_error_handler(handlers.error_handler)


# ===================== background loops =====================
async def _every(interval: float, first: float, fn, ctx, name: str) -> None:
    await asyncio.sleep(first)
    while True:
        if STATE.db is not None:
            try:
                await fn(ctx)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - ek tick ka error loop na roke
                log.exception("autoposter %s tick failed", name)
        await asyncio.sleep(interval)


async def _daily(ctx) -> None:
    """Roz 03:00 (TIMEZONE) par backup DM + purane logs saaf."""
    while True:
        now = datetime.now(TZ)
        nxt = now.replace(hour=3, minute=0, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        await asyncio.sleep(max(1.0, (nxt - now).total_seconds()))
        if STATE.db is not None:
            try:
                await jobs.daily_job(ctx)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("autoposter daily job failed")
        await asyncio.sleep(61)  # ek hi minute me do baar na chale


async def run_loops(bot) -> None:
    ctx = SimpleNamespace(bot=bot)
    tasks = [
        _every(SETTINGS.scan_interval_seconds, 5, jobs.scheduler_tick, ctx, "scheduler"),
        _every(60, 20, jobs.housekeeping_tick, ctx, "housekeeping"),
        _daily(ctx),
    ]
    if SETTINGS.vault_chat_id:
        tasks.append(_every(25, 30, jobs.archive_tick, ctx, "archive"))
    await asyncio.gather(*tasks)
