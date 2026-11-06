"""Userbot (Telethon): owner ke apne Telegram account(s) se kisi bhi chat ka media (photo/video/file + caption)
private channel me copy karta hai.

- Copy = Telegram ka server-side forward with drop_author  -> "Forwarded from" tag nahi, download/upload nahi.
- Destination: post-rights wala channel, apna Saved Messages, ya koi bhi personal (user) chat.
- Jis chat me "Restrict saving content" on hai, Telegram copy hone hi nahi deta; ye module usse bypass nahi karta.
- Session string Fernet se encrypt hokar MongoDB me jaati hai.
"""
import asyncio
import logging
import time

from cryptography.fernet import Fernet
from telethon import TelegramClient, errors
from telethon.helpers import generate_random_long
from telethon.sessions import StringSession
from telethon.tl import functions, types

import db
from config import FERNET_KEY, TG_API_HASH, TG_API_ID, UB_BATCH, UB_DELAY

log = logging.getLogger("userbot")
ENABLED = bool(TG_API_ID and TG_API_HASH)

_fernet = Fernet(FERNET_KEY.encode() if isinstance(FERNET_KEY, str) else FERNET_KEY)
_clients: dict[int, TelegramClient] = {}
_pending: dict = {}          # login chal raha hai: {"client", "phone", "hash", "ts"}
_PENDING_TTL = 600


class UbError(Exception):
    """User ko dikhane layak error text."""


def _friendly(e: Exception) -> str:
    code = getattr(e, "message", "") or type(e).__name__
    table = {
        "PHONE_NUMBER_INVALID": "Number galat hai. Country code ke saath bhejo, jaise +919876543210",
        "PHONE_NUMBER_BANNED": "Ye number Telegram par banned hai.",
        "PHONE_CODE_INVALID": "Code galat hai. Dobara bhejo.",
        "PHONE_CODE_EXPIRED": "Code expire ho gaya. Account dobara add karo.",
        "PASSWORD_HASH_INVALID": "2-step password galat hai. Dobara bhejo.",
        "API_ID_INVALID": "TG_API_ID / TG_API_HASH galat hain.",
        "PHONE_PASSWORD_FLOOD": "Bahut try ho gaye. Thodi der baad karo.",
    }
    if isinstance(e, (errors.FloodWaitError, errors.SlowModeWaitError)):
        return f"Telegram ne {getattr(e, 'seconds', '?')} sec ruk ne ko kaha hai. Baad me try karo."
    return table.get(code, code)


def _client(session: str = "") -> TelegramClient:
    return TelegramClient(StringSession(session), TG_API_ID, TG_API_HASH, connection_retries=5, auto_reconnect=True)


# ===================== accounts / login =====================
def has_pending() -> bool:
    return bool(_pending)


async def login_cancel():
    p = _pending.pop("login", None)
    if p:
        try:
            await p["client"].disconnect()
        except Exception:  # noqa: BLE001
            pass


async def login_send_code(phone: str):
    await login_cancel()
    c = _client()
    await c.connect()
    try:
        sent = await c.send_code_request(phone)
    except errors.RPCError as e:
        await c.disconnect()
        raise UbError(_friendly(e)) from e
    _pending["login"] = {"client": c, "phone": phone, "hash": sent.phone_code_hash, "ts": time.time()}


def _p():
    p = _pending.get("login")
    if not p or time.time() - p["ts"] > _PENDING_TTL:
        raise UbError("Login session expire ho gaya. Account dobara add karo.")
    return p


async def login_code(code: str) -> str:
    """'ok' (login ho gaya) ya '2fa' (password chahiye)."""
    p = _p()
    try:
        await p["client"].sign_in(phone=p["phone"], code=code, phone_code_hash=p["hash"])
    except errors.SessionPasswordNeededError:
        return "2fa"
    except errors.RPCError as e:
        raise UbError(_friendly(e)) from e
    return "ok"


async def login_password(pwd: str):
    p = _p()
    try:
        await p["client"].sign_in(password=pwd)
    except errors.RPCError as e:
        raise UbError(_friendly(e)) from e


async def login_finish() -> dict:
    p = _p()
    c = p["client"]
    me = await c.get_me()
    name = " ".join(x for x in (me.first_name, me.last_name) if x) or (me.username or str(me.id))
    enc = _fernet.encrypt(c.session.save().encode()).decode()
    await db.save_userbot(me.id, name, p["phone"], enc)
    old = _clients.get(me.id)
    if old and old is not c:
        try:
            await old.disconnect()
        except Exception:  # noqa: BLE001
            pass
    _clients[me.id] = c
    _pending.pop("login", None)
    return {"uid": me.id, "name": name}


async def get_client(uid: int):
    """Connected + authorized client, warna None."""
    c = _clients.get(uid)
    if c is None:
        doc = await db.get_userbot(uid)
        if not doc:
            return None
        c = _client(_fernet.decrypt(doc["session_enc"].encode()).decode())
        _clients[uid] = c
    if not c.is_connected():
        await c.connect()
    if not await c.is_user_authorized():
        return None
    return c


async def remove_account(uid: int):
    """Telegram se session logout + DB se delete."""
    c = await get_client(uid)
    if c:
        try:
            await c.log_out()
        except Exception as e:  # noqa: BLE001
            log.warning("logout failed: %s", e)
    _clients.pop(uid, None)
    await db.delete_userbot(uid)


async def shutdown():
    await login_cancel()
    for c in list(_clients.values()):
        try:
            await c.disconnect()
        except Exception:  # noqa: BLE001
            pass
    _clients.clear()


# ===================== dialogs =====================
def _dest_ok(kind: str, post: bool, dead: bool = False) -> bool:
    """Destination ban sakta hai? Saved Messages, personal chat (bot/deleted nahi) ya post-rights wala channel."""
    if kind == "saved":
        return True
    if kind == "user":
        return not dead
    return bool(post)


async def list_dialogs(client: TelegramClient, limit: int = 400) -> list[dict]:
    me = await client.get_me()
    out = []
    async for d in client.iter_dialogs(limit=limit):
        e = d.entity
        dead = bool(getattr(e, "deleted", False))
        if getattr(e, "is_self", False) or d.id == me.id:
            kind, title = "saved", "Saved Messages"
        elif d.is_channel and not d.is_group:
            kind, title = "channel", d.name or "Channel"
        elif d.is_group:
            kind, title = "group", d.name or "Group"
        elif getattr(e, "bot", False):
            kind, title = "bot", d.name or "Bot"
        else:
            kind, title = "user", d.name or ("Deleted account" if dead else "User")
        post = bool(d.is_channel and (getattr(e, "creator", False) or getattr(e, "admin_rights", None)))
        out.append({"id": d.id, "title": title, "kind": kind, "post": post, "dest": _dest_ok(kind, post, dead)})
    # Saved Messages hamesha sabse upar (khali ho to Telegram dialog list me nahi deta)
    out = [x for x in out if x["kind"] != "saved"]
    out.insert(0, {"id": me.id, "title": "Saved Messages", "kind": "saved", "post": False, "dest": True})
    return out


async def resolve(client: TelegramClient, text: str):
    """@username / t.me link -> {"id", "title", "kind", "post", "dest"} ya None."""
    try:
        e = await client.get_entity(text.strip())
    except Exception:  # noqa: BLE001
        return None
    from telethon import utils

    title = utils.get_display_name(e) or "Chat"
    dead = bool(getattr(e, "deleted", False))
    if isinstance(e, types.User):
        kind = "saved" if getattr(e, "is_self", False) else ("bot" if e.bot else "user")
        if kind == "saved":
            title = "Saved Messages"
    elif isinstance(e, types.Channel) and not e.megagroup:
        kind = "channel"
    else:
        kind = "group"
    post = bool(isinstance(e, types.Channel) and (e.creator or e.admin_rights))
    return {"id": utils.get_peer_id(e), "title": title, "kind": kind, "post": post, "dest": _dest_ok(kind, post, dead)}


# ===================== copy engine =====================
def kind_of(m) -> str | None:
    """photo | video | file | None (sticker, text, poll, link-preview ... skip)."""
    media = m.media
    if isinstance(media, types.MessageMediaPhoto):
        return "photo"
    if isinstance(media, types.MessageMediaDocument):
        doc = media.document
        attrs = tuple(type(a) for a in getattr(doc, "attributes", []) or [])
        if types.DocumentAttributeSticker in attrs:
            return None
        if types.DocumentAttributeVideo in attrs or types.DocumentAttributeAnimated in attrs:
            return "video"
        return "file"
    return None


async def _units(client, src, min_id: int):
    """Messages ko units me do: single message ya poora album (grouped_id), purane se naye."""
    group, gid = [], None
    async for m in client.iter_messages(src, reverse=True, min_id=min_id):
        g = m.grouped_id
        if group and g != gid:
            yield group
            group = []
        if g:
            gid = g
            group.append(m)
        else:
            yield [m]
    if group:
        yield group


async def _forward(client, from_peer, to_peer, ids: list[int]):
    """Server-side copy, forward tag ke bina. FloodWait par ruk kar retry."""
    for attempt in range(6):
        try:
            await client(
                functions.messages.ForwardMessagesRequest(
                    from_peer=from_peer,
                    id=ids,
                    to_peer=to_peer,
                    random_id=[generate_random_long() for _ in ids],
                    drop_author=True,
                    silent=True,
                )
            )
            return
        except (errors.FloodWaitError, errors.SlowModeWaitError) as e:
            if attempt == 5:
                raise
            await asyncio.sleep(min(getattr(e, "seconds", 30), 300) + 1)


_FATAL = {
    "CHAT_FORWARDS_RESTRICTED": "Is chat me 'Restrict saving content' on hai, Telegram copy nahi karne deta. (Bot isse bypass nahi karta.)",
    "CHAT_WRITE_FORBIDDEN": "Is account ko destination me message bhejne ka haq nahi hai. (Channel ho to account ko admin banao.)",
    "CHAT_ADMIN_REQUIRED": "Destination channel me account ko admin (post messages) banao.",
    "CHANNEL_PRIVATE": "Source ya destination ab accessible nahi hai (account us me nahi hai).",
    "USER_BANNED_IN_CHANNEL": "Account destination channel me banned hai.",
    "USER_IS_BLOCKED": "Is personal chat wale user ne tumhe block kiya hua hai, isliye message nahi ja sakta.",
    "YOU_BLOCKED_USER": "Tumne is user ko block kiya hua hai. Unblock karke dobara try karo.",
    "INPUT_USER_DEACTIVATED": "Destination user ka account delete ho chuka hai.",
    "USER_PRIVACY_RESTRICTED": "Is user ki privacy settings tumhe message bhejne nahi deti.",
    "PEER_ID_INVALID": "Destination/source chat valid nahi hai. Account kholke dobara chuno.",
    "PEER_FLOOD": "Telegram ne account ko temporary spam limit me daal diya (PEER_FLOOD). Thodi der baad try karo.",
}


async def _peer(client, chat_id: int, me_id: int):
    """Apna Saved Messages = InputPeerSelf (entity cache par depend nahi), baaki normal input entity."""
    if chat_id == me_id:
        return types.InputPeerSelf()
    return await client.get_input_entity(chat_id)


async def copy_chat(client, src_id: int, dest_id: int, kinds: set, cap_only: bool, min_id: int, job: dict, progress=None, acc: int = 0) -> dict:
    """src chat ke matching media ko dest me copy karo. Result dict return."""
    res = {"scanned": 0, "copied": 0, "skipped": 0, "errors": 0, "total": 0, "aborted": "", "stopped": False, "last_error": "", "last_id": min_id}
    try:
        me_id = (await client.get_me()).id
        src = await _peer(client, src_id, me_id)
        dest = await _peer(client, dest_id, me_id)
    except Exception as e:  # noqa: BLE001
        res["aborted"] = f"Chat nahi mila ({e}). Account kholke source/destination dobara chuno."
        return res
    try:
        res["total"] = (await client.get_messages(src, limit=0)).total or 0
    except Exception:  # noqa: BLE001
        pass

    buf: list = []
    consecutive_err = 0

    async def flush() -> bool:
        """True = continue, False = abort."""
        nonlocal buf, consecutive_err
        if not buf:
            return True
        ids = [m.id for m in buf]
        batch, buf = buf, []
        try:
            await _forward(client, src, dest, ids)
        except errors.RPCError as e:
            msg = getattr(e, "message", "") or type(e).__name__
            if msg in _FATAL:
                res["aborted"] = _FATAL[msg]
                return False
            if msg in ("MESSAGE_ID_INVALID", "MESSAGE_IDS_EMPTY"):
                res["skipped"] += len(batch)
                return True
            res["errors"] += len(batch)
            res["last_error"] = msg
            consecutive_err += 1
            if consecutive_err >= 5:
                res["aborted"] = f"Telegram error: {msg}"
                return False
            return True
        consecutive_err = 0
        res["copied"] += len(batch)
        res["last_id"] = max(res["last_id"], max(ids))
        if acc:
            await db.set_ub_progress(acc, src_id, dest_id, res["last_id"])
        await asyncio.sleep(UB_DELAY)
        return True

    async for unit in _units(client, src, min_id):
        if job.get("stop"):
            res["stopped"] = True
            break
        res["scanned"] += len(unit)
        items = [m for m in unit if kind_of(m) in kinds]
        if cap_only and not any((m.message or "").strip() for m in unit):
            items = []
        if not items:
            res["skipped"] += len(unit)
        else:
            buf.extend(items)
            if len(buf) >= UB_BATCH:
                if not await flush():
                    break
        if progress:
            await progress(res)
    if not res["aborted"] and not res["stopped"]:
        await flush()
    elif res["stopped"]:
        await flush()       # jo buffer me hai wo bhi copy ho jaye
    if progress:
        await progress(res, True)
    return res
