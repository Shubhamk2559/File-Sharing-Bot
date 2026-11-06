"""Files page: list/search/detail/upload/caption/delete (single, bulk, everywhere vs record only)."""
import re

import db
from services import files as files_svc
from telegram.error import TelegramError

from .core import err, need_main_bot, require_confirm, route
from .models import CaptionIn, DeleteFilesIn
from .util import q_int, tg_error

TOKEN_RX = re.compile(r"^[A-Za-z0-9_-]{4,40}$")
MAX_UPLOAD = 50 * 1024 * 1024


async def _get(token: str) -> dict:
    if not TOKEN_RX.match(token):
        raise err(404, "not_found", "No such file")
    f = await db.get_file(token)
    if not f:
        raise err(404, "not_found", "No such file")
    return f


@route("GET", "/files")
async def files_list(ctx, req, session, body):
    kind = req.query.get("kind", "")
    if kind not in ("", "post", "file"):
        raise err(422, "validation_error", "kind must be post or file")
    return await files_svc.list_files(q_int(req, "page", 0, 0, 100000), q_int(req, "size", 10, 1, 100), req.query.get("q", ""), kind)


@route("GET", "/files/{token}")
async def file_detail(ctx, req, session, body, token):
    f = await _get(token)
    d = files_svc.describe(f)
    d["items_detail"] = [{"caption": (it.get("caption") or "")[:200], "copies": len(it.get("locations") or [])} for it in (f.get("items") or [])]
    return {"file": d}


@route("POST", "/files/upload", raw=True, max_body=MAX_UPLOAD + 4096, audit="file.upload")
async def file_upload(ctx, req, session, body):
    bot = need_main_bot(ctx)
    limit = min(ctx.settings.upload_max_bytes, MAX_UPLOAD)
    data = await req.read_body(limit)
    if not data:
        raise err(400, "empty_body", "Send the file bytes as the request body")
    filename = re.sub(r"[^\w.\- ]", "_", req.query.get("filename", "file"))[:120] or "file"
    caption = req.query.get("caption", "")[:files_svc.MAX_FILE_CAPTION]
    ctype = req.headers.get("content-type", "application/octet-stream")
    try:
        res = await files_svc.save_uploaded_file(bot, data, filename, ctype, caption)
    except TelegramError as e:
        raise tg_error(e, "Upload failed (is the bot admin in both channels?)") from None
    res["_audit"] = {"file_token": res["token"], "size": len(data)}
    return res


@route("POST", "/files/{token}/caption", CaptionIn, audit="file.caption")
async def file_caption(ctx, req, session, body, token):
    await _get(token)
    await files_svc.set_caption(token, body.caption)
    return {"_audit": {"file_token": token}}


@route("POST", "/files/delete", DeleteFilesIn, audit="file.delete")
async def files_delete(ctx, req, session, body):
    require_confirm(body)
    bot = need_main_bot(ctx) if body.mode == "everywhere" else None
    for t in body.tokens:
        await _get(t)                       # 404 before touching anything

    async def work(job):
        done = failed = 0
        for i, t in enumerate(body.tokens, 1):
            if job.ctl["stop"]:
                break
            try:
                ok = (await files_svc.delete_file_everywhere(bot, t)) if bot else (await files_svc.delete_file_record(t))
                done += 1 if ok else 0
                failed += 0 if ok else 1
            except TelegramError as e:
                failed += 1
                job.log(f"{t}: {str(e)[:120]}")
            job.progress = {"done": done, "failed": failed, "total": len(body.tokens), "i": i}
        return {"deleted": done, "failed": failed, "mode": body.mode}

    if len(body.tokens) == 1:
        class _J:                            # tiny inline runner, no background task needed
            ctl = {"stop": False}
            progress: dict = {}

            def log(self, *_a):
                pass

        r = await work(_J())
        if r["failed"]:
            raise err(502, "delete_failed", "Could not delete the file (see logs)")
        return {"deleted": 1, "mode": body.mode, "_audit": {"file_token": body.tokens[0], "mode": body.mode}}
    from .jobs import JobBusy

    try:
        job = ctx.jobs.start("files-delete", f"Delete {len(body.tokens)} files ({body.mode})", work, group="files-delete")
    except JobBusy as e:
        raise err(409, "busy", str(e)) from None
    return {"job_id": job.id, "_audit": {"count": len(body.tokens), "mode": body.mode}}
