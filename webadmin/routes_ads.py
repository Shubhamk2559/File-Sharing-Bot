"""Smart Ads and Monetag pages. They call the same functions the bearer-token endpoints (/admin/ads, /admin/monetag)
and the Telegram Smart Ads panel use - no duplicated logic."""
import monetag_store as mstore
from config import MONETAG_WINDOW_DAYS, MONETAG_ZONES

from .core import err, route
from .models import AdsConfigIn, MonetagConfirmedIn, RevenueIn
from .util import jsonable, q_int


def _ads():
    from ads import routes as ads_routes
    from ads import settings as ads_settings

    if not ads_settings.SMART_ADS_ENABLED:
        raise err(404, "ads_disabled", "Smart Ads is off (set SMART_ADS_ENABLED=true and redeploy)")
    return ads_routes


@route("GET", "/ads/status")
async def ads_status(ctx, req, session, body):
    from ads import settings as ads_settings

    return {"enabled": bool(ads_settings.SMART_ADS_ENABLED), "networks": list(ads_settings.NETWORKS)}


@route("GET", "/ads/overview")
async def ads_overview(ctx, req, session, body):
    return {"report": jsonable(await _ads().h_admin_overview(q_int(req, "days", 7, 1, 90)))}


@route("GET", "/ads/config")
async def ads_config(ctx, req, session, body):
    return jsonable(await _ads().h_admin_config())          # reports secrets only as booleans


@route("POST", "/ads/config", AdsConfigIn, audit="ads.config")
async def ads_patch(ctx, req, session, body):
    status, out = await _ads().h_admin_patch_config(body.model_dump())
    if status != 200:
        raise err(400, "bad_config", "; ".join(out.get("errors", ["invalid"]))[:300])
    return {"config": jsonable(out.get("config")), "_audit": {"keys": list(body.model_dump())}}


@route("POST", "/ads/revenue", RevenueIn, audit="ads.revenue")
async def ads_revenue(ctx, req, session, body):
    status, out = await _ads().h_admin_revenue(body.model_dump())
    if status != 200:
        raise err(400, "bad_revenue", "; ".join(out.get("errors", ["invalid"]))[:300])
    return {"_audit": {"day": body.day, "network": body.network, "usd": body.revenue_usd}}


@route("GET", "/ads/rewards")
async def ads_rewards(ctx, req, session, body):
    return jsonable(await _ads().h_admin_rewards(q_int(req, "limit", 50, 1, 200)))


# ---- Monetag ---------------------------------------------------------------------------------------------------
@route("GET", "/monetag/report")
async def monetag_report(ctx, req, session, body):
    return {"report": jsonable(await mstore.build_report(q_int(req, "days", MONETAG_WINDOW_DAYS, 1, 90))),
            "zones": sorted(MONETAG_ZONES)}


@route("POST", "/monetag/confirmed", MonetagConfirmedIn, audit="monetag.confirmed")
async def monetag_confirmed(ctx, req, session, body):
    if not (body.zone == "all" or body.zone in MONETAG_ZONES):
        raise err(422, "validation_error", "zone: must be 'all' or one of the configured MONETAG_ZONES")
    await mstore.set_confirmed(body.day, body.revenue_usd, body.zone)
    return {"_audit": {"day": body.day, "zone": body.zone, "usd": body.revenue_usd}}
