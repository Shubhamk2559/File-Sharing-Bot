"""Bulk Posts: channel ki range (start link -> end link) scan karke posts banane ka engine.

Bot API channel history nahi padh sakta, isliye har message id ko owner ki private chat me
forward karte hain (type/caption pata chalne ke liye), turant delete kar dete hain.

Channel ka pattern:
    [caption wala post (photo/video)]   <- thumbnail
    [video bina caption]                <- is post ke videos
    [video bina caption]
    [caption wala post]                 <- agla group ...
"""
import asyncio
import logging
import re
from dataclasses import dataclass, field

from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError

log = logging.getLogger("bulk")

_PRIV = re.compile(r"(?:telegram\.me|t\.me)/c/(\d+)/(?:\d+/)?(\d+)", re.I)
_PUB = re.compile(r"(?:telegram\.me|t\.me)/([A-Za-z][A-Za-z0-9_]{3,31})/(?:\d+/)?(\d+)", re.I)
_RESERVED = {"joinchat", "addlist", "share", "addstickers", "proxy", "socks"}


def parse_link(text: str):
    """'https://t.me/c/123/45' -> (-100123, 45); 'https://t.me/name/45' -> ('@name', 45); warna None."""
    text = (text or "").strip()
    m = _PRIV.search(text)
    if m:
        return int("-100" + m.group(1)), int(m.group(2))
    m = _PUB.search(text)
    if m and m.group(1).lower() not in _RESERVED:
        return "@" + m.group(1), int(m.group(2))
    return None


# ---------- flood control ----------
def _wait_secs(e: RetryAfter) -> float:
    ra = e.retry_after
    return ra.total_seconds() if hasattr(ra, "total_seconds") else float(ra)


async def with_retry(fn, tries: int = 6):
    """fn() ek coroutine banata hai. RetryAfter (flood) par ruk kar dobara try."""
    for i in range(tries):
        try:
            return await fn()
        except RetryAfter as e:
            if i == tries - 1:
                raise
            await asyncio.sleep(min(_wait_secs(e), 60) + 1)


# ---------- message classification ----------
@dataclass
class Scanned:
    msg_id: int
    kind: str          # photo | video | animation | document | other
    caption: str
    file_id: str | None


@dataclass
class Group:
    head: Scanned                       # caption wala post (thumbnail)
    items: list = field(default_factory=list)   # uske niche ke bina-caption videos


@dataclass
class ScanResult:
    groups: list = field(default_factory=list)
    orphans: int = 0        # pehle caption-post se pehle ke videos
    missing: int = 0        # deleted / forward na hone wale ids
    errors: int = 0
    last_error: str = ""
    aborted: str = ""       # fatal error text
    stopped: bool = False
    scanned: int = 0


def classify(m) -> Scanned:
    cap = m.caption or ""
    if m.photo:
        return Scanned(m.message_id, "photo", cap, m.photo[-1].file_id)
    if m.video:
        return Scanned(m.message_id, "video", cap, m.video.file_id)
    if m.animation:
        return Scanned(m.message_id, "animation", cap, m.animation.file_id)
    if m.document:
        return Scanned(m.message_id, "document", cap, m.document.file_id)
    return Scanned(m.message_id, "other", cap, None)


def is_head(s: Scanned) -> bool:
    """Caption wala photo/video = post ka thumbnail."""
    return s.kind in ("photo", "video", "animation") and bool(s.caption.strip())


def is_item(s: Scanned) -> bool:
    """Bina caption wala video/animation/document = post ka video."""
    return s.kind in ("video", "animation", "document")


_MISSING_HINTS = ("message to forward not found", "message can't be forwarded", "message_id_invalid")


async def probe(bot, src, msg_id: int, scratch: int):
    """Check: bot src channel ka message forward kar sakta hai? -> (ok, error_text)."""
    try:
        m = await with_retry(lambda: bot.forward_message(scratch, src, msg_id, disable_notification=True))
    except Forbidden:
        return False, "Bot ko us channel me access nahi hai (admin banao 👑)"
    except BadRequest as e:
        t = str(e).lower()
        if "chat not found" in t:
            return False, "Channel nahi mila. Bot ko us channel ka admin banao 👑"
        if any(h in t for h in _MISSING_HINTS):
            return False, "Ye message nahi mila (delete ho gaya ya service message hai). Dusra link bhejo."
        return False, str(e)
    except TelegramError as e:
        return False, str(e)
    try:
        await bot.delete_message(scratch, m.message_id)
    except TelegramError:
        pass
    return True, None


async def scan(bot, src, start: int, end: int, scratch: int, job: dict, progress=None, delay: float = 0.4) -> ScanResult:
    """start..end ke har message ko forward karke classify karo; groups banao."""
    res = ScanResult()
    total = end - start + 1
    pending: list[int] = []
    cur: Group | None = None
    consecutive_err = 0

    async def flush():
        nonlocal pending
        if not pending:
            return
        ids, pending = pending, []
        try:
            await with_retry(lambda: bot.delete_messages(scratch, ids))
        except TelegramError as e:
            log.warning("scratch delete failed: %s", e)

    try:
        for n, mid in enumerate(range(start, end + 1), 1):
            if job.get("stop"):
                res.stopped = True
                break
            try:
                m = await with_retry(lambda: bot.forward_message(scratch, src, mid, disable_notification=True))
                consecutive_err = 0
            except Forbidden as e:
                res.aborted = f"Access nahi hai: {e}"
                break
            except BadRequest as e:
                t = str(e).lower()
                if "chat not found" in t:
                    res.aborted = "Channel nahi mila. Bot ko us channel ka admin banao."
                    break
                if any(h in t for h in _MISSING_HINTS):
                    res.missing += 1
                    consecutive_err = 0
                else:
                    res.errors += 1
                    res.last_error = str(e)
                    consecutive_err += 1
                    if consecutive_err >= 8:
                        res.aborted = f"Messages forward nahi ho rahe (channel me 'Restrict saving content' on hai?): {e}"
                        break
                if progress:
                    await progress(n, total, len(res.groups))
                await asyncio.sleep(delay)
                continue
            except TelegramError as e:
                res.errors += 1
                res.last_error = str(e)
                consecutive_err += 1
                if consecutive_err >= 8:
                    res.aborted = f"Telegram error: {e}"
                    break
                await asyncio.sleep(delay)
                continue

            res.scanned += 1
            pending.append(m.message_id)
            s = classify(m)
            s.msg_id = mid  # source channel ka id (forward ka nahi)
            if is_head(s):
                cur = Group(head=s)
                res.groups.append(cur)
            elif is_item(s):
                if cur is None:
                    res.orphans += 1
                else:
                    cur.items.append(s)
            if len(pending) >= 20:
                await flush()
            if progress:
                await progress(n, total, len(res.groups))
            await asyncio.sleep(delay)
    finally:
        await flush()
    return res
