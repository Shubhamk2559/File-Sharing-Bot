"""Copy Chats (Telethon userbot): login (phone / OTP / 2FA), chat picker, start/stop, progress.
Session strings stay Fernet-encrypted in MongoDB exactly as before and are never sent to the browser."""
import db
import userbot

from .core import err, require_confirm, route
from .jobs import JobBusy
from .models import CodeIn, CopyStartIn, NoBody, PasswordIn, PhoneIn, RemoveAccountIn, ResolveIn
from .util import q_int, redact


def _mask(phone: str) -> str:
    p = phone or ""
    return (p[:3] + "•" * max(0, len(p) - 6) + p[-3:]) if len(p) > 6 else "•••"


def _account(d: dict) -> dict:
    return {"uid": d["uid"], "name": d.get("name", ""), "phone": _mask(d.get("phone", "")), "added_at": d.get("added_at")}


def _ub_err(e: Exception):
    return err(400, "userbot_error", redact(e)[:300])


async def _client(uid: int):
    if not await db.get_userbot(uid):
        raise err(404, "not_found", "Unknown account")
    c = await userbot.get_client(uid)
    if not c:
        raise err(409, "session_expired", "This account's session has expired. Remove it and log in again.")
    return c


@route("GET", "/copy/accounts")
async def accounts(ctx, req, session, body):
    return {"accounts": [_account(d) for d in await db.list_userbots()], "login_pending": userbot.has_pending()}


@route("POST", "/copy/login/phone", PhoneIn, audit="copy.login_phone")
async def login_phone(ctx, req, session, body):
    try:
        await userbot.login_send_code(body.phone)           # phone number is deliberately not written to the audit log
    except userbot.UbError as e:
        raise _ub_err(e) from None
    return {"step": "code"}


async def _finish():
    info = await userbot.login_finish()
    return {"step": "done", "account": {"uid": info["uid"], "name": info["name"]}}


@route("POST", "/copy/login/code", CodeIn, audit="copy.login_code")
async def login_code(ctx, req, session, body):
    try:
        if await userbot.login_code(body.code) == "2fa":
            return {"step": "password"}
        return await _finish()
    except userbot.UbError as e:
        raise _ub_err(e) from None


@route("POST", "/copy/login/password", PasswordIn, audit="copy.login_password")
async def login_password(ctx, req, session, body):
    try:
        await userbot.login_password(body.password)
        return await _finish()
    except userbot.UbError as e:
        raise _ub_err(e) from None


@route("POST", "/copy/login/cancel", NoBody)
async def login_cancel(ctx, req, session, body):
    await userbot.login_cancel()
    return {}


@route("POST", "/copy/accounts/{uid}/remove", RemoveAccountIn, audit="copy.remove_account")
async def remove_account(ctx, req, session, body, uid):
    require_confirm(body)
    if not uid.lstrip("-").isdigit() or not await db.get_userbot(int(uid)):
        raise err(404, "not_found", "Unknown account")
    await userbot.remove_account(int(uid))
    return {"_audit": {"uid": int(uid)}}


@route("GET", "/copy/accounts/{uid}/chats")
async def chats(ctx, req, session, body, uid):
    if not uid.lstrip("-").isdigit():
        raise err(404, "not_found", "Unknown account")
    c = await _client(int(uid))
    try:
        rows = await userbot.list_dialogs(c)
    except Exception as e:  # noqa: BLE001
        raise err(502, "telegram_error", f"Could not load chats: {redact(e)[:160]}") from None
    q = req.query.get("q", "").strip().lower()[:100]
    if q:
        rows = [r for r in rows if q in r["title"].lower()]
    return {"chats": rows[:q_int(req, "limit", 150, 1, 400)], "total": len(rows)}


@route("POST", "/copy/resolve", ResolveIn)
async def resolve(ctx, req, session, body):
    d = await userbot.resolve(await _client(body.uid), body.text)
    if not d:
        raise err(404, "not_found", "Could not find that chat")
    return {"chat": d}


@route("GET", "/copy/progress")
async def saved_progress(ctx, req, session, body):
    try:
        uid, src, dest = (int(req.query[k]) for k in ("uid", "src", "dest"))
    except (KeyError, ValueError):
        raise err(422, "validation_error", "uid, src and dest are required integers") from None
    return {"resume_from": await db.get_ub_progress(uid, src, dest)}


@route("POST", "/copy/start", CopyStartIn, audit="copy.start")
async def start(ctx, req, session, body):
    if body.src == body.dest:
        raise err(400, "same_chat", "Source and destination must be different")
    client = await _client(body.uid)
    min_id = await db.get_ub_progress(body.uid, body.src, body.dest) if body.resume else 0

    async def run(job):
        async def progress(r, force=False):
            job.progress = {k: r.get(k) for k in ("scanned", "copied", "skipped", "errors", "total", "last_id")}

        job.log(f"start src={body.src} dest={body.dest} resume_from={min_id}")
        r = await userbot.copy_chat(client, body.src, body.dest, set(body.kinds), body.caption_only, min_id, job.ctl,
                                    progress, acc=body.uid)
        job.progress = {k: r.get(k) for k in ("scanned", "copied", "skipped", "errors", "total", "last_id")}
        if r.get("aborted"):
            job.log(redact(r["aborted"]))
        if r.get("last_error"):
            job.log("last error: " + redact(r["last_error"]))
        return {"copied": r["copied"], "scanned": r["scanned"], "skipped": r["skipped"], "errors": r["errors"],
                "stopped": r["stopped"], "aborted": redact(r.get("aborted", "")), "last_id": r.get("last_id")}

    try:
        job = ctx.jobs.start("copy", f"Copy {body.src} -> {body.dest}", run, group="copy")
    except JobBusy as e:
        raise err(409, "busy", str(e)) from None
    return {"job_id": job.id, "resume_from": min_id, "_audit": {"uid": body.uid, "src": body.src, "dest": body.dest}}
