"""Bulk Posts: scan a channel range -> posts, preview / post a batch, resume, delete all. Long work = background jobs."""
import db
from config import OWNER_ID, POST_CHANNEL
from services import bulkjobs
from telegram.error import TelegramError

from .core import err, need_main_bot, require_confirm, route
from .jobs import JobBusy
from .models import BulkScanIn, ConfirmIn, PublishBatchIn
from .util import jsonable, redact, tg_error

GROUP = "bulk"


def _start(ctx, kind, title, runner):
    try:
        return ctx.jobs.start(kind, title, runner, group=GROUP)
    except JobBusy as e:
        raise err(409, "busy", str(e)) from None


def _progress_for(job):
    async def progress(stage, force=False, **d):
        job.progress = {"stage": stage, **{k: v for k, v in d.items() if isinstance(v, (int, float, str, bool))}}
    return progress


@route("GET", "/bulk")
async def bulk_overview(ctx, req, session, body):
    rows = await db.batches.find({}).sort("created_at", -1).limit(20).to_list(length=20)
    return {"batches": [{"id": b["id"], "total": len(b.get("tokens", [])), "posted": len(b.get("posted", [])),
                         "created_at": b.get("created_at")} for b in rows],
            "post_channel": POST_CHANNEL is not None, "bulk_posts": len(await db.list_bulk_tokens())}


@route("POST", "/bulk/scan", BulkScanIn, audit="bulk.scan")
async def bulk_scan(ctx, req, session, body):
    bot = need_main_bot(ctx)
    try:
        params = await bulkjobs.validate_links(bot, body.start_link, body.end_link, OWNER_ID)
    except bulkjobs.BulkInputError as e:
        raise err(400, "bad_range", redact(e)[:300]) from None
    except TelegramError as e:
        raise tg_error(e, "Could not read the channel") from None
    params.mode = body.mode

    async def run(job):
        res = await bulkjobs.run_bulk(bot, params, OWNER_ID, job.ctl, _progress_for(job))
        s = res.scan
        job.log(f"outcome={res.outcome}")
        return {"outcome": res.outcome, "made": res.made, "videos": res.videos, "duplicates": res.dup, "empty": res.empty,
                "failed": res.failed, "backup_warnings": res.warns, "batch_id": res.batch_id,
                "orphans": getattr(s, "orphans", 0), "missing": getattr(s, "missing", 0),
                "scan_errors": getattr(s, "errors", 0), "aborted": redact(getattr(s, "aborted", "") or ""),
                "last_error": redact(res.last_err)[:200]}

    job = _start(ctx, "bulk-scan", f"Bulk {params.start}-{params.end}", run)
    return {"job_id": job.id, "range": [params.start, params.end], "_audit": {"start": params.start, "end": params.end}}


@route("POST", "/bulk/batches/{batch_id}/preview", PublishBatchIn, audit="bulk.preview")
async def bulk_preview(ctx, req, session, body, batch_id):
    return await _publish(ctx, batch_id, False)


@route("POST", "/bulk/batches/{batch_id}/post", PublishBatchIn, audit="bulk.post_all")
async def bulk_post(ctx, req, session, body, batch_id):
    if POST_CHANNEL is None:
        raise err(400, "no_post_channel", "POST_CHANNEL env is not set")
    return await _publish(ctx, batch_id, True)        # resume = call again: already posted tokens are skipped


async def _publish(ctx, batch_id, to_channel):
    bot = need_main_bot(ctx)
    batch = await db.get_batch(batch_id)
    if not batch:
        raise err(404, "not_found", "No such batch")
    dest = POST_CHANNEL if to_channel else OWNER_ID

    async def run(job):
        res = await bulkjobs.publish_batch(bot, batch, dest, to_channel, job.ctl, _progress_for(job), ctx.manager)
        return {"sent": res.sent, "todo": res.todo, "left": res.todo - res.sent, "last_error": redact(res.last_err)[:200]}

    job = _start(ctx, "bulk-publish", f"{'Post' if to_channel else 'Preview'} batch {batch_id}", run)
    return {"job_id": job.id, "_audit": {"batch": batch_id, "to_channel": to_channel}}


@route("POST", "/bulk/delete-all", ConfirmIn, audit="bulk.delete_all")
async def bulk_delete_all(ctx, req, session, body):
    require_confirm(body)
    bot = need_main_bot(ctx)

    async def run(job):
        deleted, total = await bulkjobs.delete_all_bulk(bot, job.ctl, _progress_for(job))
        return {"deleted": deleted, "total": total, "left": total - deleted}

    job = _start(ctx, "bulk-delete", "Delete all bulk posts", run)
    return {"job_id": job.id}
