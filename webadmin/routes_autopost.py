"""Auto Posting: every action of the Telegram Auto Posting panel, through autoposter.actions / database / sender."""
import gzip
import re

from autoposter import actions
from autoposter.config import SETTINGS
from autoposter.jobs import build_backup_file
from autoposter.payloads import UnsupportedMessage, album_post_payload, media_fingerprint, parse_bulk_text, text_post_payload
from autoposter.sender import (after_posts_added, process_chat_batch, rebuild_and_ensure_schedule, send_saved_post,
                               send_single_now)
from autoposter.state import STATE
from autoposter.utils import MAX_FOOTER_LEN, chat_opts, parse_post_markup, visible_len
from config import OWNER_ID
from services import files as files_svc
from telegram.error import TelegramError

from . import security
from .core import Resp, err, need_main_bot, route
from .jobs import JobBusy
from .models import (ApActiveIn, ApAlbumIn, ApBulkTextIn, ApDangerIn, ApForceSubIn, ApImportIn, ApOptionsIn, ApPromoIn,
                     ApRefIn, ApScheduleIn, ApTextPostIn, NoBody)
from .util import jsonable, q_int, redact, tg_error

MAX_RESTORE = 30 * 1024 * 1024
MAX_MEDIA = 50 * 1024 * 1024


def _db():
    if STATE.db is None:
        raise err(503, "autopost_off", "Auto Posting is not running in this process")
    return STATE.db


async def _chat(cid: str) -> dict:
    if not cid.lstrip("-").isdigit():
        raise err(404, "not_found", "Unknown target")
    chat = await _db().get_chat(int(cid))
    if not chat:
        raise err(404, "not_found", "Unknown target")
    return chat


def _summary(chat: dict) -> dict:
    s = chat.get("schedule") or {}
    o = chat_opts(chat)
    return jsonable({
        "chat_id": chat["chat_id"], "title": chat.get("title"), "type": chat.get("type"), "username": chat.get("username"),
        "is_active": chat.get("is_active", True), "next_post_time": chat.get("next_post_time"), "last_error": chat.get("last_error"),
        "pause_reason": chat.get("pause_reason"), "force_sub_channel": chat.get("force_sub_channel"),
        "archiving": bool(chat.get("archiving")),
        "schedule": {k: s.get(k) for k in ("interval_minutes", "batch_size", "start_time", "end_time", "days", "enabled")},
        "options": o, "cycles": int((chat.get("stats") or {}).get("total_cycles_completed", 0)),
    })


def _post_view(p: dict) -> dict:
    from autoposter.ui import post_label

    return {"id": str(p["_id"]), "media_type": p.get("media_type", "text"), "label": post_label(p),
            "text": (p.get("text") or p.get("caption") or "")[:300], "used": bool(p.get("used_in_cycle")),
            "created_at": jsonable(p.get("created_at"))}


# ---- password gate for destructive actions (same ADMIN_PASSWORD as the bot) ---------------------------------
def _gate(ctx, session, body: ApDangerIn) -> None:
    if not body.confirm:
        raise err(400, "confirm_required", "Confirm the destructive action first")
    if not actions.password_required():
        return
    key = "pw:" + session["_id"]
    wait = ctx.pw_guard.locked_for(key)
    if wait:
        raise err(429, "locked", f"Too many wrong passwords. Try again in {wait // 60 + 1} min.", retry_after=wait)
    if not body.admin_password or not actions.password_ok(body.admin_password):
        ctx.pw_guard.fail(key)
        raise err(403, "password_required", "ADMIN_PASSWORD is required for this action and was missing or wrong")
    ctx.pw_guard.success(key)


# ---- overview / settings / logs -------------------------------------------------------------------------------
@route("GET", "/autopost/overview")
async def overview(ctx, req, session, body):
    db = _db()
    chats = await db.list_chats()
    try:
        ping = round(await db.ping())
    except Exception:  # noqa: BLE001
        ping = None
    return {"stats": jsonable(await db.global_stats()), "targets": [_summary(c) for c in chats],
            "health": {"db_ping_ms": ping, "uptime_seconds": STATE.uptime_seconds},
            "settings": {"timezone": SETTINGS.timezone, "scan_interval_seconds": SETTINGS.scan_interval_seconds,
                         "retry_attempts": SETTINGS.retry_attempts, "retry_base_seconds": SETTINGS.retry_base_seconds,
                         "post_gap_seconds": SETTINGS.post_gap_seconds, "default_interval_minutes": SETTINGS.default_interval_minutes,
                         "default_batch_size": SETTINGS.default_batch_size, "default_start_time": SETTINGS.default_start_time,
                         "default_end_time": SETTINGS.default_end_time, "log_retention_days": SETTINGS.log_retention_days,
                         "password_gate": actions.password_required(), "vault_configured": bool(SETTINGS.vault_chat_id)}}


@route("GET", "/autopost/logs")
async def logs(ctx, req, session, body):
    rows = await _db().logs.find({}).sort("created_at", -1).limit(q_int(req, "limit", 50, 1, 200)).to_list(length=200)
    return {"logs": [{"level": r.get("level"), "event": r.get("event"), "at": jsonable(r.get("created_at")),
                      "detail": redact(str(r.get("payload", {})))[:300]} for r in rows]}


# ---- targets ---------------------------------------------------------------------------------------------------
@route("POST", "/autopost/targets", ApRefIn, audit="ap.add_target")
async def add_target(ctx, req, session, body):
    try:
        doc = await actions.add_target(need_main_bot(ctx), actions.parse_chat_ref(body.ref))
    except actions.ActionError as e:
        raise err(400, "action_failed", redact(e)[:300]) from None
    return {"target": _summary(doc), "_audit": {"chat_id": doc["chat_id"]}}


@route("GET", "/autopost/targets/{cid}")
async def target_detail(ctx, req, session, body, cid):
    chat = await _chat(cid)
    db, c = _db(), chat["chat_id"]
    snap = await db.queue_snapshot(c)
    days = await db.history_by_day(c, 7)
    totals, top = await db.reaction_summary(c)
    return {"target": _summary(chat), "queue": jsonable(snap), "published_today": await db.published_today(c),
            "history": [{"day": d, "posts": n} for d, n in days], "reactions": list(totals),
            "running": STATE.lock_for(c).locked()}


@route("POST", "/autopost/targets/{cid}/schedule", ApScheduleIn, audit="ap.schedule")
async def set_schedule(ctx, req, session, body, cid):
    chat = await _chat(cid)
    c = chat["chat_id"]
    fields = body.model_dump(exclude_none=True)
    jitter = fields.pop("jitter_minutes", None)
    if not fields and jitter is None:
        raise err(422, "validation_error", "Nothing to change")
    if fields:
        await _db().update_schedule(c, **fields)
    if jitter is not None:
        await _db().set_options(c, jitter_minutes=jitter)
    return {"target": _summary(await _db().get_chat(c)), "_audit": {"chat_id": c, "fields": list(body.model_dump(exclude_none=True))}}


@route("POST", "/autopost/targets/{cid}/options", ApOptionsIn, audit="ap.options")
async def set_options(ctx, req, session, body, cid):
    chat = await _chat(cid)
    c = chat["chat_id"]
    f = body.model_dump(exclude_unset=True, exclude_none=True)
    if "footer" in f:
        footer = f["footer"].strip()
        if footer and visible_len(footer) > MAX_FOOTER_LEN:
            raise err(422, "validation_error", f"footer: too long (max {MAX_FOOTER_LEN} visible characters)")
        f["footer"] = footer or None
    if not f:
        raise err(422, "validation_error", "Nothing to change")
    await _db().set_options(c, **f)
    return {"target": _summary(await _db().get_chat(c)), "_audit": {"chat_id": c, "fields": list(f)}}


@route("POST", "/autopost/targets/{cid}/active", ApActiveIn, audit="ap.active")
async def set_active(ctx, req, session, body, cid):
    chat = await _chat(cid)
    c = chat["chat_id"]
    await _db().set_active(c, body.active, reason=None if body.active else "Paused by admin.")
    return {"target": _summary(await _db().get_chat(c)), "_audit": {"chat_id": c, "active": body.active}}


@route("POST", "/autopost/targets/{cid}/reshuffle", NoBody, audit="ap.reshuffle")
async def reshuffle(ctx, req, session, body, cid):
    chat = await _chat(cid)
    await rebuild_and_ensure_schedule(chat["chat_id"])
    return {"_audit": {"chat_id": chat["chat_id"]}}


@route("POST", "/autopost/targets/{cid}/run", NoBody, audit="ap.run_now")
async def run_now(ctx, req, session, body, cid):
    chat = await _chat(cid)
    c = chat["chat_id"]
    if not chat.get("is_active", True):
        raise err(409, "paused", "This target is paused - resume it first")
    if STATE.lock_for(c).locked():
        raise err(409, "busy", "A batch is already being published")
    bot = need_main_bot(ctx)

    async def run(job):
        job.progress = {"sent": 0}
        sent = await process_chat_batch(bot, c, manual=True)
        job.progress = {"sent": sent}
        return {"sent": sent}

    try:
        job = ctx.jobs.start("autopost-run", f"Run batch for {chat.get('title', c)}", run, group=f"autopost-{c}")
    except JobBusy as e:
        raise err(409, "busy", str(e)) from None
    return {"job_id": job.id, "_audit": {"chat_id": c}}


@route("POST", "/autopost/targets/{cid}/promo", ApPromoIn, audit="ap.promo")
async def promo(ctx, req, session, body, cid):
    chat = await _chat(cid)
    bot = need_main_bot(ctx)
    try:
        await actions.publish_promo(bot, chat, lambda: bot.send_message(chat["chat_id"], body.text, parse_mode="HTML"))
    except TelegramError as e:
        raise tg_error(e, "Could not send/pin") from None
    return {"_audit": {"chat_id": chat["chat_id"]}}


@route("POST", "/autopost/targets/{cid}/force-sub", ApForceSubIn, audit="ap.force_sub")
async def force_sub(ctx, req, session, body, cid):
    chat = await _chat(cid)
    try:
        value = await actions.set_force_sub(need_main_bot(ctx), chat["chat_id"], body.ref)
    except actions.ActionError as e:
        raise err(400, "action_failed", redact(e)[:300]) from None
    return {"force_sub_channel": value, "_audit": {"chat_id": chat["chat_id"]}}


# ---- posts ---------------------------------------------------------------------------------------------------
@route("GET", "/autopost/targets/{cid}/posts")
async def posts_page(ctx, req, session, body, cid):
    chat = await _chat(cid)
    rows, pages = await _db().page_posts(chat["chat_id"], q_int(req, "page", 0, 0, 100000), q_int(req, "size", 10, 1, 50))
    return {"posts": [_post_view(p) for p in rows], "pages": pages, "total": await _db().count_posts(chat["chat_id"])}


async def _post(chat, post_id):
    p = await _db().get_post(chat["chat_id"], post_id)
    if not p:
        raise err(404, "not_found", "That post no longer exists")
    return p


@route("POST", "/autopost/targets/{cid}/posts/{pid}/preview", NoBody, audit="ap.post_preview")
async def post_preview(ctx, req, session, body, cid, pid):
    chat = await _chat(cid)
    try:
        await send_saved_post(need_main_bot(ctx), OWNER_ID, await _post(chat, pid), chat_opts(chat))
    except TelegramError as e:
        raise tg_error(e, "Preview failed") from None
    return {}


@route("POST", "/autopost/targets/{cid}/posts/{pid}/send", NoBody, audit="ap.post_send")
async def post_send(ctx, req, session, body, cid, pid):
    chat = await _chat(cid)
    try:
        await send_single_now(need_main_bot(ctx), chat["chat_id"], await _post(chat, pid))
    except TelegramError as e:
        raise tg_error(e, "Could not send") from None
    return {"_audit": {"chat_id": chat["chat_id"]}}


@route("POST", "/autopost/targets/{cid}/posts/{pid}/delete", ApDangerIn, audit="ap.post_delete")
async def post_delete(ctx, req, session, body, cid, pid):
    if not body.confirm:
        raise err(400, "confirm_required", "Confirm the delete first")
    chat = await _chat(cid)
    if not await _db().delete_post(chat["chat_id"], pid):
        raise err(404, "not_found", "That post no longer exists")
    return {"_audit": {"chat_id": chat["chat_id"]}}


@route("POST", "/autopost/targets/{cid}/undo", ApDangerIn, audit="ap.undo")
async def undo_last(ctx, req, session, body, cid):
    if not body.confirm:
        raise err(400, "confirm_required", "Confirm first")
    chat = await _chat(cid)
    removed = await _db().undo_last_post(chat["chat_id"])
    if not removed:
        raise err(404, "nothing", "No posts to undo")
    return {"removed": _post_view(removed)}


async def _added(c: int, inserted: int, dups: int, bad: int = 0) -> dict:
    if inserted:
        await after_posts_added(c)
    return {"inserted": inserted, "duplicates": dups, "invalid": bad}


@route("POST", "/autopost/targets/{cid}/posts/text", ApTextPostIn, audit="ap.add_text")
async def add_text(ctx, req, session, body, cid):
    chat = await _chat(cid)
    try:
        payload = text_post_payload(body.text)
    except UnsupportedMessage as e:
        raise err(422, "validation_error", f"text: {e}") from None
    pid = await _db().add_post(chat["chat_id"], payload, chat_opts(chat)["dedupe"])
    return await _added(chat["chat_id"], 1 if pid else 0, 0 if pid else 1)


@route("POST", "/autopost/targets/{cid}/posts/bulk", ApBulkTextIn, audit="ap.bulk_text", max_body=9_000_000)
async def add_bulk(ctx, req, session, body, cid):
    chat = await _chat(cid)
    if not body.filename.lower().endswith((".json", ".jsonl", ".txt")):
        raise err(422, "validation_error", "filename: use .json, .jsonl or .txt")
    try:
        posts, bad = parse_bulk_text(body.filename, body.text)
    except ValueError as e:
        raise err(400, "bad_file", redact(e)[:300]) from None
    inserted, dups = await _db().bulk_add_posts(chat["chat_id"], posts, chat_opts(chat)["dedupe"])
    out = await _added(chat["chat_id"], inserted, dups, bad)
    out["_audit"] = {"chat_id": chat["chat_id"], "inserted": inserted}
    return out


def _descriptor(m, kind: str, fid: str) -> dict:
    obj = (m.photo[-1] if getattr(m, "photo", None) else getattr(m, "video", None) or getattr(m, "animation", None)
           or getattr(m, "document", None) or getattr(m, "audio", None))
    return {"media_type": kind, "file_id": fid, "file_unique_id": getattr(obj, "file_unique_id", None),
            "file_name": getattr(obj, "file_name", None)}


async def _stage(ctx, req) -> dict:
    """Upload bytes to the owner's chat just to obtain a reusable Telegram file_id."""
    data = await req.read_body(min(ctx.settings.upload_max_bytes, MAX_MEDIA))
    if not data:
        raise err(400, "empty_body", "Send the file bytes as the request body")
    name = re.sub(r"[^\w.\- ]", "_", req.query.get("filename", "file"))[:120] or "file"
    try:
        m, kind, fid = await files_svc.send_bytes(need_main_bot(ctx), OWNER_ID, data, name, req.headers.get("content-type", ""), "")
    except TelegramError as e:
        raise tg_error(e, "Upload failed") from None
    if not fid:
        raise err(502, "telegram_error", "Telegram did not return a file id")
    return _descriptor(m, kind, fid)


@route("POST", "/autopost/stage-media", raw=True, max_body=MAX_MEDIA + 4096, audit="ap.stage_media")
async def stage_media(ctx, req, session, body):
    return {"media": await _stage(ctx, req)}


@route("POST", "/autopost/targets/{cid}/posts/media", raw=True, max_body=MAX_MEDIA + 4096, audit="ap.add_media")
async def add_media(ctx, req, session, body, cid):
    chat = await _chat(cid)
    d = await _stage(ctx, req)
    body_html, flags = parse_post_markup(req.query.get("caption", "")[:1024])
    payload = {**d, "parse_mode": "HTML", **flags, "caption": body_html or None,
               "fingerprint": media_fingerprint(d["media_type"], d["file_unique_id"]) or f"f:{d['media_type']}:{d['file_id']}"}
    pid = await _db().add_post(chat["chat_id"], payload, chat_opts(chat)["dedupe"])
    return await _added(chat["chat_id"], 1 if pid else 0, 0 if pid else 1)


@route("POST", "/autopost/targets/{cid}/posts/album", ApAlbumIn, audit="ap.add_album")
async def add_album(ctx, req, session, body, cid):
    chat = await _chat(cid)
    items = [i.model_dump(exclude_none=True) for i in body.items]
    cap, flags = parse_post_markup(body.caption or "")
    payload = album_post_payload(items, cap or None, flags)
    pid = await _db().add_post(chat["chat_id"], payload, chat_opts(chat)["dedupe"])
    return await _added(chat["chat_id"], 1 if pid else 0, 0 if pid else 1)


@route("POST", "/autopost/targets/{cid}/import", ApImportIn, audit="ap.import")
async def import_posts(ctx, req, session, body, cid):
    chat = await _chat(cid)
    bot = need_main_bot(ctx)
    try:
        source, first_id = await actions.check_import_source(bot, body.first_link)
        res = await actions.import_range(bot, chat["chat_id"], source.id, first_id, body.last_link)
    except actions.ActionError as e:
        raise err(400, "action_failed", redact(e)[:300]) from None
    return {"inserted": res.inserted, "duplicates": res.dups, "vault": res.vault, "_audit": {"chat_id": chat["chat_id"], "inserted": res.inserted}}


# ---- destructive (ADMIN_PASSWORD gated, like the bot) -------------------------------------------------------
@route("POST", "/autopost/targets/{cid}/wipe", ApDangerIn, audit="ap.wipe_chat")
async def wipe_chat(ctx, req, session, body, cid):
    _gate(ctx, session, body)
    chat = await _chat(cid)
    await actions.wipe_chat(chat["chat_id"])
    return {"_audit": {"chat_id": chat["chat_id"]}}


@route("POST", "/autopost/targets/{cid}/remove", ApDangerIn, audit="ap.remove_chat")
async def remove_chat(ctx, req, session, body, cid):
    _gate(ctx, session, body)
    chat = await _chat(cid)
    await actions.remove_chat(chat["chat_id"])
    return {"_audit": {"chat_id": chat["chat_id"]}}


@route("POST", "/autopost/wipe-all", ApDangerIn, audit="ap.wipe_all")
async def wipe_all(ctx, req, session, body):
    _gate(ctx, session, body)
    _db()
    await actions.wipe_all()
    return {}


# ---- backup / restore --------------------------------------------------------------------------------------
@route("GET", "/autopost/backup", audit="ap.backup")
async def backup(ctx, req, session, body):
    blob, filename = build_backup_file(await _db().backup_payload())
    ctype = "application/gzip" if filename.endswith(".gz") else "application/json"
    return Resp(200, blob, ctype, {"Content-Disposition": f'attachment; filename="{filename}"'})


@route("POST", "/autopost/restore", raw=True, max_body=MAX_RESTORE, audit="ap.restore")
async def restore(ctx, req, session, body):
    if req.query.get("confirm") != "1":
        raise err(400, "confirm_required", "Restore replaces posts of the targets in the file - confirm first")
    _gate(ctx, session, ApDangerIn(confirm=True, admin_password=req.headers.get("x-admin-password")))
    blob = await req.read_body(MAX_RESTORE)
    try:
        if blob[:2] == b"\x1f\x8b":
            blob = gzip.decompress(blob)
        chats, posts = await actions.restore_backup(blob.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError, OSError, KeyError, TypeError) as e:
        raise err(400, "bad_file", redact(e)[:300]) from None
    return {"chats": chats, "posts": posts, "_audit": {"chats": chats, "posts": posts}}
