"""Login/logout/session, dashboard, settings (env set/unset), activity log, jobs."""
import os

from . import security
from .core import Resp, err, json_resp, route
from .models import LoginIn, NoBody


# ------------------------------------------------------------------ auth
@route("POST", "/login", LoginIn, public=True, csrf=False)
async def login(ctx, req, session, body):
    s = ctx.settings
    if ctx.login_limiter is not None and not ctx.login_limiter.allow("*"):
        await ctx.audit.log("login.throttled", ip=req.ip, ok=False)
        raise err(429, "rate_limited", "Too many login attempts overall. Try again shortly.")
    wait = ctx.guard.locked_for(req.ip)
    if wait:
        await ctx.audit.log("login.locked", ip=req.ip, ok=False)
        raise err(429, "locked", f"Too many failed attempts. Try again in {wait // 60 + 1} min.", retry_after=wait)

    pw_ok = security.secure_eq(body.password, s.password)
    totp_ok, counter = True, None
    if s.totp_enabled:
        counter = security.totp_verify(s.totp_secret, body.totp or "", last_counter=ctx.last_totp)
        totp_ok = counter is not None
    if not (pw_ok and totp_ok):                      # one generic message: do not reveal which factor failed
        lock = ctx.guard.fail(req.ip)
        await ctx.audit.log("login.fail", ip=req.ip, ok=False)
        if lock:
            raise err(429, "locked", "Too many failed attempts. Locked for a while.", retry_after=lock)
        raise err(401, "bad_credentials", "Invalid credentials")
    if counter is not None:
        ctx.last_totp = counter
    ctx.guard.success(req.ip)
    cookie, csrf, expires = await ctx.sessions.create(req.ip, req.headers.get("user-agent", ""))
    await ctx.audit.log("login.ok", ip=req.ip, ok=True)
    return Resp(200, {"ok": True, "csrf": csrf, "expires": expires.isoformat()}, set_cookie=(ctx.cookie_name, cookie, ctx.settings.session_hours * 3600))


@route("POST", "/logout", NoBody)
async def logout(ctx, req, session, body):
    await ctx.sessions.destroy(req.cookies.get(ctx.cookie_name))
    await ctx.audit.log("logout", ip=req.ip)
    return Resp(200, {"ok": True}, clear_cookie=True)


@route("POST", "/logout-all", NoBody, audit="sessions.revoke_all")
async def logout_all(ctx, req, session, body):
    await ctx.sessions.destroy_all()
    return Resp(200, {"ok": True}, clear_cookie=True)


@route("GET", "/session")
async def whoami(ctx, req, session, body):
    return {"csrf": session["csrf"], "totp": ctx.settings.totp_enabled, "role": os.getenv("ROLE", "both"),
            "expires": session["expires"].isoformat() if hasattr(session["expires"], "isoformat") else str(session["expires"])}


# ------------------------------------------------------------------ dashboard
@route("GET", "/dashboard")
async def dashboard(ctx, req, session, body):
    from services import bots as bots_svc
    from services import stats as stats_svc

    out = {"stats": await stats_svc.collect(), "sessions": await stats_svc.sessions_overview(),
           "role": os.getenv("ROLE", "both"), "running_jobs": [j.view(False) for j in ctx.jobs.running()]}
    try:
        out["bots"] = await bots_svc.list_bots(ctx.manager)
    except Exception:  # noqa: BLE001
        out["bots"] = []
    try:
        from autoposter.state import STATE

        out["autoposter"] = _jsonable(await STATE.db.global_stats()) if STATE.db else None
    except Exception:  # noqa: BLE001
        out["autoposter"] = None
    try:
        from ads import settings as ads_settings

        out["smart_ads_enabled"] = bool(ads_settings.SMART_ADS_ENABLED)
    except Exception:  # noqa: BLE001
        out["smart_ads_enabled"] = False
    return out


def _jsonable(d):
    from datetime import datetime

    if isinstance(d, dict):
        return {k: _jsonable(v) for k, v in d.items()}
    if isinstance(d, (list, tuple)):
        return [_jsonable(v) for v in d]
    if isinstance(d, datetime):
        return d.isoformat()
    return d


# ------------------------------------------------------------------ settings (set / unset only)
# Groups of every environment variable the project reads. Secrets are reported only as set/unset.
SECRET_WORDS = ("TOKEN", "SECRET", "KEY", "PASSWORD", "URI", "HASH", "SESSION_STR", "DSN")
SHOW_VALUE = {  # plain, non-sensitive tunables whose value may be displayed
    "ROLE", "DB_NAME", "AP_DB_NAME", "SESSION_TTL", "POST_MAX_VIDEOS", "BULK_MAX_RANGE", "BULK_POST_DELAY", "BULK_SCAN_DELAY",
    "UB_BATCH", "UB_DELAY", "SMART_ADS_ENABLED", "ADS_REQUIRED", "AD_VERIFY_MODE", "BLOCK_NON_VALUED", "AUTO_DELETE_SECONDS",
    "MIN_AD_SECONDS", "POSTBACK_WAIT_SECONDS", "HEALTH_INTERVAL", "TIMEZONE", "DEFAULT_BATCH_SIZE", "DEFAULT_DAYS",
    "DEFAULT_END_TIME", "DEFAULT_START_TIME", "DEFAULT_INTERVAL_MINUTES", "RETRY_ATTEMPTS", "RETRY_BASE_SECONDS",
    "POST_GAP_SECONDS", "SCAN_INTERVAL_SECONDS", "SCHEDULER_LOCK_SECONDS", "LOG_RETENTION_DAYS", "MONETAG_OPTIMIZE",
    "MONETAG_WINDOW_DAYS", "ADMIN_PANEL_SESSION_HOURS", "ADMIN_PANEL_MAX_FAILS", "ADMIN_PANEL_LOCKOUT_SECONDS",
    "ADMIN_PANEL_UPLOAD_MAX_MB", "ADMIN_PANEL_INSECURE_COOKIES", "ADMIN_PANEL_NO_PROXY", "STATS_TZ_OFFSET_MIN", "POST_DIRECT_LINK", "DIRECT_MINIAPP",
}
ENV_GROUPS = {
    "Core": ["MAIN_BOT_TOKEN", "OWNER_ID", "MONGO_URI", "DB_NAME", "FERNET_KEY", "BASE_URL", "LINK_BASE_URL", "WEBHOOK_SECRET",
             "ROLE", "SESSION_TTL", "ADMIN_PASSWORD", "HEALTH_INTERVAL"],
    "Channels": ["PRIMARY_CHANNEL", "VAULT_CHAT_ID", "BACKUP_CHANNEL", "POST_CHANNEL", "POST_MAX_VIDEOS", "BULK_MAX_RANGE",
                 "BULK_POST_DELAY", "BULK_SCAN_DELAY"],
    "Copy chats (userbot)": ["TG_API_ID", "TG_API_HASH", "UB_BATCH", "UB_DELAY"],
    "Auto posting": ["AP_DB_NAME", "AP_EXTRA_ADMIN_IDS", "TIMEZONE", "DEFAULT_BATCH_SIZE", "DEFAULT_DAYS", "DEFAULT_START_TIME",
                     "DEFAULT_END_TIME", "DEFAULT_INTERVAL_MINUTES", "RETRY_ATTEMPTS", "RETRY_BASE_SECONDS", "POST_GAP_SECONDS",
                     "SCAN_INTERVAL_SECONDS", "SCHEDULER_LOCK_SECONDS", "LOG_RETENTION_DAYS"],
    "Ads / Monetag": ["SMART_ADS_ENABLED", "ADS_ADMIN_TOKEN", "MONETAG_ADMIN_TOKEN", "MONETAG_POSTBACK_SECRET", "MONETAG_ZONES",
                      "MONETAG_ZONE_ID", "MONETAG_DEFAULT_ZONE", "MONETAG_OPTIMIZE", "MONETAG_WINDOW_DAYS", "ADSGRAM_BLOCK_ID",
                      "ADSGRAM_REWARD_SECRET", "TADS_WIDGET_ID", "AD_VERIFY_MODE", "ADS_REQUIRED", "BLOCK_NON_VALUED"],
    "Admin panel": ["ADMIN_PANEL_PASSWORD", "ADMIN_PANEL_TOTP_SECRET", "ADMIN_PANEL_SESSION_HOURS", "ADMIN_PANEL_MAX_FAILS",
                    "ADMIN_PANEL_LOCKOUT_SECONDS", "ADMIN_PANEL_UPLOAD_MAX_MB", "ADMIN_PANEL_INSECURE_COOKIES", "ADMIN_PANEL_NO_PROXY"],
}


def _is_secret(name: str) -> bool:
    return name not in SHOW_VALUE          # allow-list: anything not explicitly safe is treated as secret


@route("GET", "/settings")
async def settings_view(ctx, req, session, body):
    groups = []
    for title, names in ENV_GROUPS.items():
        rows = []
        for n in names:
            raw = os.environ.get(n)
            is_set = bool((raw or "").strip())
            row = {"name": n, "set": is_set, "secret": _is_secret(n)}
            if is_set and not row["secret"]:
                row["value"] = raw.strip()[:80]
            rows.append(row)
        groups.append({"group": title, "vars": rows})
    return {"groups": groups, "note": "Secrets are shown only as set/unset. Change values in your host's environment settings and redeploy."}


# ------------------------------------------------------------------ activity log
@route("GET", "/activity")
async def activity(ctx, req, session, body):
    try:
        limit = max(1, min(int(req.query.get("limit", 50)), 200))
        skip = max(0, int(req.query.get("skip", 0)))
    except ValueError:
        raise err(422, "validation_error", "limit/skip must be integers") from None
    return {"items": await ctx.audit.recent(limit, skip)}


# ------------------------------------------------------------------ jobs
@route("GET", "/jobs")
async def jobs_list(ctx, req, session, body):
    return {"jobs": ctx.jobs.list()}


@route("GET", "/jobs/{job_id}")
async def job_get(ctx, req, session, body, job_id):
    j = ctx.jobs.get(job_id)
    if not j:
        raise err(404, "not_found", "No such job")
    return {"job": j.view()}


@route("POST", "/jobs/{job_id}/stop", NoBody, audit="job.stop")
async def job_stop(ctx, req, session, body, job_id):
    if not ctx.jobs.stop(job_id):
        raise err(409, "not_running", "Job is not running")
    return {"_audit": {"job": job_id}}
