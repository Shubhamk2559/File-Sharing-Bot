"""Create Post (1 thumbnail + N videos -> one link), preview, post to channel.

Web flow: the browser uploads the thumbnail and videos one by one into a server-side *draft* (kept in memory, one per
admin session). Videos are stored into PRIMARY/BACKUP at upload time exactly like the bot does and are rolled back if
the draft is discarded or never finalised-and-saved."""
import secrets

import db
from config import OWNER_ID, POST_CAPTION, POST_CHANNEL, POST_MAX_VIDEOS
from services import files as files_svc
from services import posts as posts_svc
from telegram.error import TelegramError

from .core import err, need_main_bot, require_confirm, route
from .models import FinalizePostIn, NoBody, PostToChannelIn
from .util import tg_error

MAX_BYTES = 50 * 1024 * 1024
DRAFTS: dict = {}          # session id -> draft dict


def _draft(session, create=False) -> dict | None:
    d = DRAFTS.get(session["_id"])
    if d is None and create:
        d = DRAFTS[session["_id"]] = {"token": secrets.token_urlsafe(6), "thumb": None, "videos": [], "caption": ""}
    return d


def _view(d) -> dict:
    if not d:
        return {"draft": None, "max_videos": POST_MAX_VIDEOS, "post_channel": POST_CHANNEL is not None}
    return {"draft": {"thumb": bool(d["thumb"]), "thumb_type": (d["thumb"] or {}).get("type"),
                      "videos": [{"caption": v["caption"], "copies": len(v["locations"])} for v in d["videos"]],
                      "caption": d["caption"], "default_caption": POST_CAPTION},
            "max_videos": POST_MAX_VIDEOS, "post_channel": POST_CHANNEL is not None}


def _name(req) -> str:
    import re

    return re.sub(r"[^\w.\- ]", "_", req.query.get("filename", "file"))[:120] or "file"


@route("GET", "/posts/draft")
async def draft_get(ctx, req, session, body):
    return _view(_draft(session))


@route("POST", "/posts/draft/thumb", raw=True, max_body=MAX_BYTES + 4096, audit="post.draft_thumb")
async def draft_thumb(ctx, req, session, body):
    bot = need_main_bot(ctx)
    data = await req.read_body(min(ctx.settings.upload_max_bytes, MAX_BYTES))
    if not data:
        raise err(400, "empty_body", "Send the image/video/gif bytes as the request body")
    ctype = req.headers.get("content-type", "")
    kind = files_svc._kind_for(ctype, _name(req), len(data))
    if kind == "document":
        raise err(415, "bad_media", "Thumbnail must be an image, GIF or video")
    try:                                          # sent to the owner's chat only to obtain a reusable file_id
        _m, kind, fid = await files_svc.send_bytes(bot, OWNER_ID, data, _name(req), ctype, "")
    except TelegramError as e:
        raise tg_error(e, "Thumbnail upload failed") from None
    if not fid:
        raise err(502, "telegram_error", "Telegram did not return a file id")
    d = _draft(session, create=True)
    d["thumb"] = {"fid": fid, "type": kind}
    return _view(d)


@route("POST", "/posts/draft/video", raw=True, max_body=MAX_BYTES + 4096, audit="post.draft_video")
async def draft_video(ctx, req, session, body):
    bot = need_main_bot(ctx)
    d = _draft(session, create=True)
    if len(d["videos"]) >= POST_MAX_VIDEOS:
        raise err(409, "limit", f"Max {POST_MAX_VIDEOS} videos per post")
    data = await req.read_body(min(ctx.settings.upload_max_bytes, MAX_BYTES))
    if not data:
        raise err(400, "empty_body", "Send the video bytes as the request body")
    caption = req.query.get("caption", "")[:900]
    try:
        locs, _warn, _kind, _fid = await files_svc.upload_to_channels(
            bot, data, _name(req), req.headers.get("content-type", ""), caption, f"{d['token']} #{len(d['videos']) + 1}")
    except TelegramError as e:
        raise tg_error(e, "Upload failed (is the bot admin in both channels?)") from None
    d["videos"].append({"locations": locs, "caption": caption})
    return _view(d)


@route("POST", "/posts/draft/video/{idx}/remove", NoBody, audit="post.draft_video_remove")
async def draft_video_remove(ctx, req, session, body, idx):
    d = _draft(session)
    if not d or not idx.isdigit() or int(idx) >= len(d["videos"]):
        raise err(404, "not_found", "No such video in the draft")
    item = d["videos"].pop(int(idx))
    await posts_svc.rollback_items(need_main_bot(ctx), [item])
    return _view(d)


@route("POST", "/posts/draft/caption", FinalizePostIn)
async def draft_caption(ctx, req, session, body):
    d = _draft(session, create=True)
    d["caption"] = (body.caption or "")[:1024]
    return _view(d)


@route("POST", "/posts/draft/discard", NoBody, audit="post.draft_discard")
async def draft_discard(ctx, req, session, body):
    d = DRAFTS.pop(session["_id"], None)
    if d and d["videos"]:
        await posts_svc.rollback_items(need_main_bot(ctx), d["videos"])
    return _view(None)


@route("POST", "/posts/draft/finalize", FinalizePostIn, audit="post.create")
async def draft_finalize(ctx, req, session, body):
    bot = need_main_bot(ctx)
    d = _draft(session)
    if not d or not d["thumb"] or not d["videos"]:
        raise err(400, "incomplete", "A thumbnail and at least one video are required")
    caption = (body.caption if body.caption is not None else d["caption"]) or POST_CAPTION
    token = d["token"]
    await db.save_post(token, d["videos"], caption, d["thumb"]["fid"], thumb_type=d["thumb"]["type"])
    DRAFTS.pop(session["_id"], None)
    out = {"token": token, "link": files_svc.file_link(token), "videos": len(d["videos"]), "_audit": {"file_token": token}}
    try:                                           # same as the bot: show the owner the exact post
        f = await db.get_file(token)
        await posts_svc.send_thumb_post(bot, OWNER_ID, f, ctx.manager)
        out["preview_sent"] = True
    except TelegramError:
        out["preview_sent"] = False
    return out


@route("POST", "/posts/{token}/preview", NoBody, audit="post.preview")
async def post_preview(ctx, req, session, body, token):
    f = await db.get_file(token)
    if not f or not f.get("photo"):
        raise err(404, "not_found", "No such post")
    try:
        await posts_svc.send_thumb_post(need_main_bot(ctx), OWNER_ID, f, ctx.manager)
    except TelegramError as e:
        raise tg_error(e, "Preview failed") from None
    return {"_audit": {"file_token": token}}


@route("POST", "/posts/{token}/channel", PostToChannelIn, audit="post.to_channel")
async def post_to_channel(ctx, req, session, body, token):
    require_confirm(body)
    if POST_CHANNEL is None:
        raise err(400, "no_post_channel", "POST_CHANNEL env is not set")
    f = await db.get_file(token)
    if not f or not f.get("photo"):
        raise err(404, "not_found", "No such post")
    try:
        m = await posts_svc.send_thumb_post(need_main_bot(ctx), POST_CHANNEL, f, ctx.manager)
        await db.add_channel_post(token, POST_CHANNEL, m.message_id)
    except TelegramError as e:
        raise tg_error(e, "Posting failed (is the bot admin of POST_CHANNEL?)") from None
    return {"_audit": {"file_token": token}}
