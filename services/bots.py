"""Bot registry operations shared by the Telegram owner menu and the web admin panel.

Nothing in here ever returns a bot token (plain or encrypted)."""
import logging

from telegram import Bot
from telegram.error import TelegramError

import db
from config import BASE_URL, CHANNELS

log = logging.getLogger("services.bots")


def serialize(b: dict, manager=None) -> dict:
    running = bool(manager and b["bot_id"] in getattr(manager, "apps", {}))
    return {
        "bot_id": b["bot_id"],
        "username": b.get("username"),
        "status": b.get("status"),
        "running": running,
        "added_at": b.get("added_at"),
        "updated_at": b.get("updated_at"),
    }


async def list_bots(manager=None) -> list:
    return [serialize(b, manager) for b in await db.list_bots()]


async def disable_bot(manager, bot_id: str) -> bool:
    b = await db.get_bot(bot_id)
    if not b:
        return False
    await db.set_status(b["bot_id"], "disabled")
    await manager.release_bot(b)
    await manager.refresh()
    return True


async def enable_bot(manager, bot_id: str) -> bool:
    """Enable / re-check: pending -> health check promotes it to active when it is admin in both channels."""
    b = await db.get_bot(bot_id)
    if not b:
        return False
    await db.set_status(b["bot_id"], "pending")
    await manager.check_all()
    await manager.refresh()
    return True


async def remove_bot(manager, bot_id: str) -> bool:
    b = await db.get_bot(bot_id)
    if not b:
        return False
    await manager.release_bot(b)
    await db.delete_bot(b["bot_id"])
    await manager.refresh()
    return True


async def check_all(manager) -> None:
    await manager.check_all()
    await manager.refresh()


async def add_bot(manager, token: str):
    """-> (ok, message). manager.add_bot returns a human string that starts with ❌ on failure."""
    msg = await manager.add_bot(token)
    return (not msg.startswith("❌")), msg


async def health(manager, bot_id: str) -> dict:
    """Live getMe + getWebhookInfo + channel-admin check. Uses the stored token server-side only."""
    if bot_id == "main":
        bot = manager.main.bot if getattr(manager, "main", None) else None
        if bot is None:
            return {"found": False, "reason": "main bot is not running in this ROLE"}
        return await _probe(manager, bot, "main")
    b = await db.get_bot(bot_id)
    if not b:
        return {"found": False, "reason": "unknown bot"}
    from manager import _dec  # lazy: manager imports handlers which imports this module

    try:
        async with Bot(_dec(b["token_enc"])) as tmp:
            return await _probe(manager, tmp, bot_id)
    except TelegramError as e:
        return {"found": True, "alive": False, "error": str(e)[:200]}


async def _probe(manager, bot, bot_id: str) -> dict:
    try:
        me = await bot.get_me()
        wh = await bot.get_webhook_info()
        ok, missing = await manager.check_admin(bot)
    except TelegramError as e:
        return {"found": True, "alive": False, "error": str(e)[:200]}
    expected = f"{BASE_URL}/webhook/{bot_id}"
    return {
        "found": True,
        "alive": True,
        "username": me.username,
        "webhook_set": bool(wh.url),
        "webhook_expected": (wh.url or "") == expected,
        "pending_updates": wh.pending_update_count,
        "last_error": (wh.last_error_message or "")[:200] or None,
        "admin_in_channels": ok,
        "missing_channels": [str(c) for c in missing],
        "channels_checked": [str(c) for c in CHANNELS],
    }
