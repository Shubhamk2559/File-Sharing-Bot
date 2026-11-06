"""Bulk Posts engine (scan -> save posts -> publish -> delete-all) as plain async functions.

Both the Telegram panel (handlers.py) and the web admin panel (webadmin/) call these. They know nothing about
Telegram message panels or HTTP: progress goes through an async callback `progress(stage, **data)`, cancellation
through the shared `job` dict (`job["stop"] = True`)."""
import asyncio
import logging
import secrets
from dataclasses import dataclass, field

from telegram.error import TelegramError

import bulk
import db
from config import BULK_MAX_RANGE, BULK_POST_DELAY, BULK_SCAN_DELAY, POST_CAPTION, POST_CHANNEL
from services.files import delete_file_everywhere
from services.posts import send_thumb_post, store_to_channels

log = logging.getLogger("services.bulkjobs")


class BulkInputError(ValueError):
    """Message is safe to show to the owner."""


@dataclass
class BulkParams:
    chat: object        # -100... channel id or "@name"
    start: int
    end: int
    mode: str = "default"     # "default" = POST_CAPTION, "original" = channel caption


@dataclass
class BulkResult:
    scan: object = None             # bulk.ScanResult
    outcome: str = "done"           # done | aborted | scan_stopped | stopped
    made: int = 0
    videos: int = 0
    dup: int = 0
    empty: int = 0
    failed: int = 0
    warns: int = 0
    last_err: str = ""
    tokens: list = field(default_factory=list)
    batch_id: str | None = None


@dataclass
class PublishResult:
    sent: int = 0
    todo: int = 0
    last_err: str = ""


def ordered_range(a: int, b: int):
    """-> (lo, hi, count). Raises BulkInputError when the range is bigger than BULK_MAX_RANGE."""
    lo, hi = sorted((int(a), int(b)))
    n = hi - lo + 1
    if n > BULK_MAX_RANGE:
        raise BulkInputError(f"Range too big ({n}). Max {BULK_MAX_RANGE} messages per run.")
    return lo, hi, n


async def validate_links(bot, start_link: str, end_link: str, scratch: int) -> BulkParams:
    """Web equivalent of the bot's two-step 'send start link / send end link' dialog."""
    s = bulk.parse_link(start_link)
    e = bulk.parse_link(end_link)
    if not s:
        raise BulkInputError("Start link is not a valid Telegram post link (example: https://t.me/c/1234567890/15).")
    if not e:
        raise BulkInputError("End link is not a valid Telegram post link.")
    if s[0] != e[0]:
        raise BulkInputError("Start and end links must be from the same channel.")
    for chat, mid in (s, e):
        ok, err = await bulk.probe(bot, chat, mid, scratch)
        if not ok:
            raise BulkInputError(err or "Unknown error")
    lo, hi, _n = ordered_range(s[1], e[1])
    return BulkParams(chat=s[0], start=lo, end=hi)


async def run_bulk(bot, p: BulkParams, scratch: int, job: dict, progress) -> BulkResult:
    """Scan start..end, group into posts, store videos in both channels, save posts + a batch."""
    total_msgs = p.end - p.start + 1

    async def on_scan(n: int, total: int, groups: int):
        await progress("scan", n=n, total=total, groups=groups)

    await progress("scan", n=0, total=total_msgs, groups=0, force=True)
    res = await bulk.scan(bot, p.chat, p.start, p.end, scratch, job, on_scan, BULK_SCAN_DELAY)
    out = BulkResult(scan=res)
    if res.aborted:
        out.outcome = "aborted"
        return out
    if res.stopped:
        out.outcome = "scan_stopped"
        return out

    consecutive_fail = 0
    total_g = len(res.groups)
    for gi, g in enumerate(res.groups, 1):
        if job["stop"]:
            out.outcome = "stopped"
            break
        await progress("save", i=gi, total=total_g, made=out.made, videos=out.videos)
        if not g.items:
            out.empty += 1
            continue
        if await db.find_post_by_source(p.chat, g.head.msg_id):
            out.dup += 1
            continue

        gtoken = secrets.token_urlsafe(6)
        items: list = []
        try:
            for i, it in enumerate(g.items, 1):
                locs, warn = await store_to_channels(bot, p.chat, it.msg_id, it.caption[:900], f"{gtoken} #{i}")
                items.append({"locations": locs, "caption": it.caption[:900]})
                out.warns += 1 if warn else 0
                await asyncio.sleep(0.3)
        except TelegramError as e:
            for it in items:  # adhura group wapas hata do
                for loc in it["locations"]:
                    try:
                        await bot.delete_message(loc["channel"], loc["msg"])
                    except TelegramError:
                        pass
            out.failed += 1
            consecutive_fail += 1
            out.last_err = str(e)
            if consecutive_fail >= 3:
                break
            continue
        consecutive_fail = 0

        caption = g.head.caption[:1024] if p.mode == "original" else POST_CAPTION
        await db.save_post(
            gtoken, items, caption, g.head.file_id,
            thumb_type=g.head.kind, src_chat=p.chat, src_head=g.head.msg_id,
        )
        out.tokens.append(gtoken)
        out.made += 1
        out.videos += len(items)

    if out.tokens:
        out.batch_id = secrets.token_urlsafe(5)
        await db.save_batch(out.batch_id, out.tokens)
    return out


async def publish_batch(bot, batch: dict, dest, to_channel: bool, job: dict, progress, manager=None) -> PublishResult:
    """Send a batch's thumbnail posts in order to `dest` (POST_CHANNEL, or the owner's chat for a preview)."""
    already = set(batch.get("posted", [])) if to_channel else set()
    todo = [t for t in batch["tokens"] if t not in already]
    out = PublishResult(todo=len(todo))
    failed_in_row = 0
    await progress("publish", i=0, total=len(todo), force=True)
    for i, t in enumerate(todo, 1):
        if job["stop"]:
            break
        f = await db.get_file(t)
        if not f or not f.get("photo"):
            continue
        try:
            sent_msg = await send_thumb_post(bot, dest, f, manager)
            out.sent += 1
            failed_in_row = 0
            if to_channel:
                await db.add_channel_post(t, dest, sent_msg.message_id)
                await db.mark_batch_posted(batch["id"], t)
        except TelegramError as e:
            failed_in_row += 1
            out.last_err = str(e)
            if failed_in_row >= 3:
                break
        await progress("publish", i=i, total=len(todo))
        await asyncio.sleep(BULK_POST_DELAY if to_channel else 0.8)
    return out


async def delete_all_bulk(bot, job: dict, progress):
    """Delete every bulk-made post everywhere. -> (deleted, total)."""
    deleted = 0
    tokens = await db.list_bulk_tokens()
    for i, t in enumerate(tokens, 1):
        if job["stop"]:
            break
        await progress("delete", i=i, total=len(tokens))
        if await delete_file_everywhere(bot, t):
            deleted += 1
    return deleted, len(tokens)


def post_channel_ready() -> bool:
    return POST_CHANNEL is not None
