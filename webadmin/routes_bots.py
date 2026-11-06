"""Bots page: same operations as the Telegram owner menu (services.bots). Tokens never leave the server."""
from services import bots as bots_svc

from .core import err, need_manager, require_confirm, route
from .models import AddBotIn, ConfirmIn, NoBody


@route("GET", "/bots")
async def bots_list(ctx, req, session, body):
    return {"bots": await bots_svc.list_bots(ctx.manager)}


@route("POST", "/bots", AddBotIn, audit="bot.add")
async def bots_add(ctx, req, session, body):
    ok, msg = await bots_svc.add_bot(need_manager(ctx), body.token)      # token is never logged or echoed
    if not ok:
        raise err(400, "add_failed", msg.lstrip("❌ ").strip()[:300])
    return {"message": msg[:300], "bots": await bots_svc.list_bots(ctx.manager)}


@route("POST", "/bots/check-all", NoBody, audit="bot.check_all")
async def bots_check_all(ctx, req, session, body):
    await bots_svc.check_all(need_manager(ctx))
    return {"bots": await bots_svc.list_bots(ctx.manager)}


@route("GET", "/bots/{bot_id}/health")
async def bots_health(ctx, req, session, body, bot_id):
    return {"health": await bots_svc.health(need_manager(ctx), bot_id)}


@route("POST", "/bots/{bot_id}/disable", NoBody, audit="bot.disable")
async def bots_disable(ctx, req, session, body, bot_id):
    if not await bots_svc.disable_bot(need_manager(ctx), bot_id):
        raise err(404, "not_found", "Unknown bot")
    return {"bots": await bots_svc.list_bots(ctx.manager), "_audit": {"bot_id": bot_id}}


@route("POST", "/bots/{bot_id}/enable", NoBody, audit="bot.enable")
async def bots_enable(ctx, req, session, body, bot_id):
    if not await bots_svc.enable_bot(need_manager(ctx), bot_id):
        raise err(404, "not_found", "Unknown bot")
    return {"bots": await bots_svc.list_bots(ctx.manager), "_audit": {"bot_id": bot_id}}


@route("POST", "/bots/{bot_id}/remove", ConfirmIn, audit="bot.remove")
async def bots_remove(ctx, req, session, body, bot_id):
    require_confirm(body)
    if not await bots_svc.remove_bot(need_manager(ctx), bot_id):
        raise err(404, "not_found", "Unknown bot")
    return {"bots": await bots_svc.list_bots(ctx.manager), "_audit": {"bot_id": bot_id}}
