"""Auto Posting actions as plain async functions.

Both the Telegram panel (autoposter/handlers.py) and the web admin panel (webadmin/) call these, so validation and
side effects live in exactly one place. Error messages are the same texts the bot always showed."""
from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Any

from telegram.constants import ChatType
from telegram.error import TelegramError

from .config import SETTINGS
from .sender import after_posts_added
from .state import STATE
from .utils import chat_opts, parse_message_link, utcnow

IMPORT_LIMIT = 5000


class ActionError(Exception):
    """Message is safe to show to the admin (bot reply or web error)."""


def parse_chat_ref(raw: str) -> int | str:
    raw = raw.strip()
    if "t.me/" in raw:
        raw = raw.split("t.me/", 1)[1].split("/")[0].split("?")[0]
    if raw.lstrip("-").isdigit():
        return int(raw)
    return raw if raw.startswith("@") else f"@{raw}"


async def upsert_target_from_chat(chat_obj) -> dict[str, Any]:
    chat_type = "channel" if chat_obj.type == ChatType.CHANNEL else "group"
    title = chat_obj.title or str(chat_obj.id)
    doc = await STATE.db.ensure_chat(chat_obj.id, title, chat_type, getattr(chat_obj, "username", None))
    STATE.title_cache[chat_obj.id] = title
    return doc


# --------------------------------------------------------------------------- #
# Targets
# --------------------------------------------------------------------------- #
async def add_target(bot, chat_ref: int | str) -> dict[str, Any]:
    """Validate that the bot can post there, then register it. Returns the saved chat document."""
    try:
        chat_obj = await bot.get_chat(chat_ref)
        member = await bot.get_chat_member(chat_obj.id, bot.id)
    except TelegramError as exc:
        raise ActionError(f"❌ Chat access nahi mil raha: {exc}\nBot ko wahan admin banao, phir dobara bhejo (ya /cancel).") from exc
    if chat_obj.type not in (ChatType.CHANNEL, ChatType.GROUP, ChatType.SUPERGROUP):
        raise ActionError("❌ Ye channel ya group nahi hai. Dobara bhejo ya /cancel.")
    if member.status in ("left", "kicked"):
        raise ActionError("❌ Bot is chat me member nahi hai. Pehle bot ko add/admin karo, phir dobara bhejo.")
    if chat_obj.type == ChatType.CHANNEL:
        if member.status not in ("administrator", "creator"):
            raise ActionError("❌ Channel me bot ko admin banana zaroori hai (Post messages permission ke saath). Phir dobara bhejo.")
        if getattr(member, "can_post_messages", None) is False:
            raise ActionError("❌ Bot admin hai lekin 'Post messages' permission off hai. Permission on karke dobara bhejo.")
    return await upsert_target_from_chat(chat_obj)


async def set_force_sub(bot, target_chat_id: int, raw: str) -> int | str | None:
    """'off' removes the gate. Otherwise validates the bot is in that channel. Returns the stored value (or None)."""
    raw = (raw or "").strip()
    if raw.lower() == "off":
        await STATE.db.chats.update_one({"chat_id": target_chat_id}, {"$unset": {"force_sub_channel": ""}, "$set": {"updated_at": utcnow()}})
        return None
    value = parse_chat_ref(raw)
    try:
        await bot.get_chat_member(value, bot.id)
    except TelegramError as exc:
        raise ActionError(f"❌ Bot can't access {raw} — make sure the bot is an admin there. ({exc})") from exc
    await STATE.db.chats.update_one({"chat_id": target_chat_id}, {"$set": {"force_sub_channel": value, "updated_at": utcnow()}})
    return value


async def publish_promo(bot, chat: dict[str, Any], send):
    """`send` is an async callable returning the sent Message. Pins it and unpins the previous prompt."""
    chat_id = chat["chat_id"]
    old_promo_id = chat.get("promo_message_id")
    sent = await send()
    await bot.pin_chat_message(chat_id=chat_id, message_id=sent.message_id, disable_notification=True)
    if old_promo_id:
        try:
            await bot.unpin_chat_message(chat_id=chat_id, message_id=old_promo_id)
        except TelegramError:
            pass
    await STATE.db.chats.update_one({"chat_id": chat_id}, {"$set": {"promo_message_id": sent.message_id, "updated_at": utcnow()}})
    return sent.message_id


# --------------------------------------------------------------------------- #
# Import a range of posts from a source channel
# --------------------------------------------------------------------------- #
async def check_import_source(bot, first_link: str):
    """-> (source_chat, first_message_id)."""
    parsed = parse_message_link(first_link or "")
    if not parsed or parsed[0] is None:
        raise ActionError("Link samajh nahi aaya. Aisa link bhejo: https://t.me/c/1234567890/120 (ya /cancel).")
    ref, first_id = parsed
    try:
        source = await bot.get_chat(ref)
        member = await bot.get_chat_member(source.id, bot.id)
    except TelegramError as exc:
        raise ActionError(f"❌ Source channel access nahi mil raha: {exc}\nBot ko wahan admin banao aur link dobara bhejo (ya /cancel).") from exc
    if member.status in ("left", "kicked"):
        raise ActionError("❌ Bot us channel me member nahi hai. Pehle add/admin karo, phir link bhejo.")
    return source, first_id


@dataclass
class ImportResult:
    inserted: int
    dups: int
    vault: str          # "archiving" | "no_admin" | "no_vault"
    target: dict


async def import_range(bot, target_chat_id: int, source_chat_id: int, from_message_id: int, last_link: str) -> ImportResult:
    parsed = parse_message_link(last_link or "")
    if not parsed:
        raise ActionError("Link samajh nahi aaya. Last post ka link bhejo (ya /cancel).")
    ref, last_id = parsed
    if ref is not None:
        try:
            same = (await bot.get_chat(ref)).id == source_chat_id
        except TelegramError:
            same = False
        if not same:
            raise ActionError("❌ Ye link doosre channel ka hai. Pehle wale hi channel ka link bhejo.")
    lo, hi = sorted((int(from_message_id), last_id))
    total = hi - lo + 1
    if total > IMPORT_LIMIT:
        raise ActionError(f"❌ Range bahut bada hai ({total}). Ek baar me {IMPORT_LIMIT} tak import karo, ya /cancel.")
    target = await STATE.db.get_chat(target_chat_id)
    if not target:
        raise ActionError("❌ Target channel ab exist nahi karta.")
    posts = [
        {"media_type": "copy", "source_chat_id": source_chat_id, "source_message_id": mid, "fingerprint": f"c:{source_chat_id}:{mid}",
         "caption": None, "silent": False, "protect_content": False, "buttons": []}
        for mid in range(lo, hi + 1)
    ]
    vault = "no_vault"
    if SETTINGS.vault_chat_id:
        try:
            await bot.get_chat_member(SETTINGS.vault_chat_id, bot.id)
            for post in posts:
                post["archive_pending"] = True
            vault = "archiving"
        except TelegramError:
            vault = "no_admin"
    inserted, dups = await STATE.db.bulk_add_posts(target_chat_id, posts, chat_opts(target)["dedupe"])
    if inserted and vault == "archiving":
        await STATE.db.chats.update_one({"chat_id": target_chat_id}, {"$set": {"archiving": True}})
    await after_posts_added(target_chat_id)
    return ImportResult(inserted, dups, vault, target)


# --------------------------------------------------------------------------- #
# Destructive actions + the ADMIN_PASSWORD gate
# --------------------------------------------------------------------------- #
def password_required() -> bool:
    return bool(SETTINGS.admin_password)


def password_ok(entered: str | None) -> bool:
    """Constant-time check against ADMIN_PASSWORD (True when no password is configured)."""
    if not SETTINGS.admin_password:
        return True
    a = (entered or "").encode()
    b = SETTINGS.admin_password.encode()
    return hmac.compare_digest(a, b)


async def wipe_all() -> None:
    await STATE.db.wipe_all()
    STATE.title_cache.clear()


async def wipe_chat(chat_id: int) -> bool:
    if not await STATE.db.get_chat(chat_id):
        return False
    await STATE.db.wipe_chat(chat_id)
    return True


async def remove_chat(chat_id: int) -> str | None:
    """-> title of the removed target, or None when it did not exist."""
    chat = await STATE.db.get_chat(chat_id)
    if not chat:
        return None
    await STATE.db.delete_chat(chat_id)
    STATE.title_cache.pop(chat_id, None)
    return str(chat.get("title", chat_id))


async def restore_backup(text: str) -> tuple[int, int]:
    from .database import loads_json  # noqa: PLC0415

    chats, posts = await STATE.db.restore_payload(loads_json(text))
    STATE.title_cache.clear()
    return chats, posts
