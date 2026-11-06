"""HTTP surface. Handlers are plain async functions (unit-tested); register() wraps them in FastAPI routes.

IMPORTANT: do NOT add `from __future__ import annotations` to this module. register() imports FastAPI's Request
locally, and FastAPI resolves route-handler annotations from the module globals. With postponed evaluation the
annotation `request: Request` stays an unresolvable string, FastAPI treats `request` as a required *query
parameter*, and every call fails with HTTP 422 {"loc": ["query", "request"], "msg": "Field required"}.
(tests/test_routes_wiring.py guards this.)
"""
import hmac
import json
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import engine, report, service, settings, slog, store
from .ratelimit import RateLimiter
from .settings import NETWORKS

HERE = Path(__file__).parent
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
USERID_RE = re.compile(r"^\d{1,15}$")
MAX_REVENUE_USD = 1_000_000.0


@dataclass
class Ctx:
    auth: Callable                      # async (body) -> (session, application, err)
    identify: Callable                  # (init_data) -> (application, info)
    send_file: Callable
    db: Any
    token_re: Any
    telegram_errors: tuple = ()
    forbidden: tuple = ()
    ads_required: int = 1
    admin_token: str = ""
    reward_secret: str = ""
    manager: Any = None
    rl_user: RateLimiter = None
    rl_ip: RateLimiter = None
    rl_admin: RateLimiter = None
    rl_hits: RateLimiter = None
    admin_fail: RateLimiter = None

    def __post_init__(self):
        self.rl_user = self.rl_user or RateLimiter(20, 60)
        self.rl_ip = self.rl_ip or RateLimiter(90, 60)
        self.rl_admin = self.rl_admin or RateLimiter(120, 60)
        self.rl_hits = self.rl_hits or RateLimiter(600, 60)
        self.admin_fail = self.admin_fail or RateLimiter(10, 600)


# ---------------------------------------------------------------- request diagnostics
def summarize_validation(errors) -> list:
    """FastAPI/Pydantic error list -> safe, compact list. Never includes `input` (it may hold Telegram initData)."""
    out = []
    for e in list(errors or [])[:20]:
        loc = ".".join(str(x) for x in (e.get("loc") or ()))
        out.append({"field": loc, "type": str(e.get("type", "")), "msg": str(e.get("msg", ""))[:200]})
    return out


def validation_payload(errors) -> dict:
    items = summarize_validation(errors)
    missing = [i["field"] for i in items if i["type"] == "missing"]
    invalid = [i["field"] for i in items if i["type"] != "missing"]
    parts = []
    if missing:
        parts.append("missing: " + ", ".join(missing))
    if invalid:
        parts.append("invalid: " + ", ".join(invalid))
    return {"ok": False, "code": "validation_error",
            "error": "The request was rejected (" + "; ".join(parts or ["invalid request"]) + ").", "details": items}


async def read_json_body(request) -> tuple:
    """-> (dict, None) or ({}, error_payload). Unlike a silent `{}` fallback, a malformed body is reported as such."""
    ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    try:
        raw = await request.body()
    except Exception:  # noqa: BLE001
        raw = b""
    if not raw:
        return {}, None                              # empty body is allowed: sid/init_data checks give the precise error
    if ctype != "application/json":
        slog.event("bad_request", level="warning", path=str(request.url.path), reason="content_type", content_type=ctype or None)
        return {}, {"ok": False, "code": "bad_content_type",
                    "error": "Requests must be sent as application/json (got %s)." % (ctype or "no content-type")}
    try:
        b = json.loads(raw)
    except ValueError:
        slog.event("bad_request", level="warning", path=str(request.url.path), reason="invalid_json", bytes=len(raw))
        return {}, {"ok": False, "code": "invalid_json", "error": "The request body is not valid JSON."}
    if not isinstance(b, dict):
        slog.event("bad_request", level="warning", path=str(request.url.path), reason="not_object")
        return {}, {"ok": False, "code": "invalid_json", "error": "The request body must be a JSON object."}
    return b, None


def _session_label(body: dict) -> str:
    sid = body.get("sid")
    return "none" if not (isinstance(sid, str) and sid) else "present"


# ---------------------------------------------------------------- Mini App
async def _bootstrap(ctx: Ctx, body: dict):
    """Direct launch (no sid): same checks as the legacy /api/ad/start."""
    application, info = ctx.identify(body.get("init_data", ""))
    if not application:
        return None, {"ok": False, "error": "Please open this inside Telegram."}
    token = info.get("start_param") or ""
    if not ctx.token_re.match(token) or not await ctx.db.get_file(token):
        return None, {"ok": False, "error": "This link is invalid."}
    user_id = info["user"]["id"]
    try:
        await application.bot.send_chat_action(user_id, "upload_document")
    except ctx.forbidden:
        return None, {"ok": False, "code": "no_write", "bot": application.bot.username}
    except ctx.telegram_errors:
        pass
    sid = secrets.token_urlsafe(12)
    await ctx.db.create_session(sid=sid, token=token, user_id=user_id, chat_id=user_id,
                                bot_id=application.bot_data["bot_id"], required=ctx.ads_required)
    return await ctx.db.get_session(sid), None


async def h_start(ctx: Ctx, body: dict, ip: str, country=None) -> dict:
    if not ctx.rl_ip.allow(("start", ip)):
        return {"ok": False, "code": "rate_limited", "error": "Too many requests. Please wait a minute."}
    had_sid = _session_label(body)
    if had_sid == "present":
        s, _app, err = await ctx.auth(body)
        slog.event("smart_start", sid=had_sid, init_data_len=len(str(body.get("init_data") or "")),
                   session="ok" if not err else "rejected", reason=err)
        if err:
            return {"ok": False, "code": "session", "error": err}
    else:
        s, early = await _bootstrap(ctx, body)
        slog.event("smart_start", sid=had_sid, init_data_len=len(str(body.get("init_data") or "")),
                   session="created" if not early else "rejected", reason=(early or {}).get("error") or (early or {}).get("code"))
        if early:
            return early
    if s["delivered"]:
        return {"ok": False, "error": "You already received this file."}
    if not ctx.rl_user.allow(("start", s["user_id"])):
        return {"ok": False, "code": "rate_limited", "error": "Too many attempts. Please wait a minute."}
    return await service.start(s, s["user_id"], country)


async def h_report(ctx: Ctx, body: dict, ip: str) -> dict:
    if not ctx.rl_ip.allow(("report", ip)):
        return {"ok": False, "code": "rate_limited"}
    s, _app, err = await ctx.auth(body)
    if err:
        return {"ok": False, "error": err}
    if not ctx.rl_user.allow(("report", s["user_id"])):
        return {"ok": False, "code": "rate_limited"}
    ev = body.get("event")
    if ev not in ("shown", "skip", "fail"):
        return {"ok": False, "error": "Bad event."}
    return await service.report(s, body.get("ad_id"), ev, str(body.get("reason") or "")[:20])


async def h_done(ctx: Ctx, body: dict, ip: str) -> dict:
    if not ctx.rl_ip.allow(("done", ip)):
        return {"ok": False, "code": "rate_limited", "error": "Too many requests. Please wait a minute."}
    s, application, err = await ctx.auth(body)
    if err:
        return {"ok": False, "error": err}
    if s["delivered"]:
        return {"ok": False, "error": "You already received this file."}
    if not ctx.rl_user.allow(("done", s["user_id"])):
        return {"ok": False, "code": "rate_limited", "error": "Too many requests. Please wait a minute."}
    r = await service.done(s, body.get("ad_id"), body.get("completed") is True, body.get("non_valued") is True)
    if not r.get("granted"):
        return r
    s2, db = r["s2"], ctx.db
    base = {"ok": True, "ads_done": s2["ads_done"], "ads_required": s2["ads_required"]}
    if s2["ads_done"] < s2["ads_required"]:
        return dict(base, delivered=False)
    if not await db.claim_delivery(s["sid"]):
        return {"ok": False, "error": "You already received this file."}
    doc = await db.get_file(s2["token"])
    ok = bool(doc) and await ctx.send_file(application.bot, s2["chat_id"], doc, ctx.manager)
    if not ok:
        await db.unclaim_delivery(s["sid"])
        return {"ok": False, "error": "The file is unavailable right now. Please retry shortly."}
    if s2.get("prompt_msg"):
        try:
            await application.bot.delete_message(s2["chat_id"], s2["prompt_msg"])
        except ctx.telegram_errors:
            pass
    return dict(base, delivered=True)


# ---------------------------------------------------------------- AdsGram Reward URL
async def h_adsgram_reward(ctx: Ctx, query: dict, ip: str):
    """GET <BASE_URL>/api/adsgram/reward?secret=<ADSGRAM_REWARD_SECRET>&userid=[userId]
    AdsGram documents only the user id in this callback, so our own secret in the URL authenticates the caller."""
    if not ctx.reward_secret:
        return 404, "not found"
    if not ctx.rl_hits.allow(("hit", ip)):
        return 429, "rate limited"
    if not hmac.compare_digest(str(query.get("secret", "")), ctx.reward_secret):
        slog.event("adsgram_hit_bad_secret", level="warning", ip=ip)
        return 403, "forbidden"
    uid = str(query.get("userid", ""))
    if not USERID_RE.match(uid):
        return 400, "bad userid"
    await store.add_reward_hit(int(uid))
    slog.event("adsgram_reward_hit", user_id=int(uid))
    return 200, "ok"


# ---------------------------------------------------------------- admin
def admin_check(ctx: Ctx, authorization: str | None, ip: str):
    """None if allowed, else (status, detail)."""
    if not ctx.admin_token:
        return 404, "not found"
    if not ctx.rl_admin.allow(ip):
        return 429, "rate limited"
    got = (authorization or "").removeprefix("Bearer ").strip()
    if hmac.compare_digest(got.encode(), ctx.admin_token.encode()):
        return None
    if not ctx.admin_fail.allow(ip):
        slog.event("admin_lockout", level="warning", ip=ip)
        return 429, "too many failed attempts"
    slog.event("admin_auth_failed", level="warning", ip=ip)
    return 403, "forbidden"


async def h_admin_overview(days: int) -> dict:
    return await report.build(days)


async def h_admin_config() -> dict:
    cfg = await engine.get_config(force=True)
    return {"config": cfg, "secrets_set": {"adsgram_reward_secret": settings.RUNTIME["adsgram_secret"],
                                           "monetag_postback": settings.RUNTIME["monetag_postback"]},
            "env_ids": {"adsgram_block_id": bool(settings.ADSGRAM_BLOCK_ID), "tads_widget_id": bool(settings.TADS_WIDGET_ID)}}


async def h_admin_patch_config(body: dict):
    new, errors = await engine.update_config(body)
    return (400, {"ok": False, "errors": errors}) if errors else (200, {"ok": True, "config": new})


async def h_admin_revenue(body: dict):
    day, net = str(body.get("day", "")), body.get("network")
    rev = body.get("revenue_usd")
    errors = []
    if not DAY_RE.match(day):
        errors.append("day must be YYYY-MM-DD")
    elif day > store.day_key():
        errors.append("day cannot be in the future")
    if net not in NETWORKS:
        errors.append("network must be one of " + ",".join(NETWORKS))
    if isinstance(rev, bool) or not isinstance(rev, (int, float)) or not (0 <= rev <= MAX_REVENUE_USD) or rev != rev:
        errors.append("revenue_usd must be a number between 0 and 1,000,000")
    if errors:
        return 400, {"ok": False, "errors": errors}
    await store.set_confirmed_revenue(day, net, float(rev))
    slog.event("confirmed_revenue_set", day=day, network=net, usd=float(rev))
    return 200, {"ok": True}


async def h_admin_rewards(limit: int) -> dict:
    return {"rewards": await store.recent_rewards(max(1, min(limit, 200)))}


# ---------------------------------------------------------------- FastAPI wiring (thin)
def register(app, *, auth, identify, send_file, db, token_re, telegram_errors, forbidden, policy, manager, ads_required,
             monetag_store=None, select_zone=None, sdk_url=None, country=None, clock=time.time) -> Ctx:
    from fastapi import Header, HTTPException, Request
    from fastapi.exception_handlers import request_validation_exception_handler
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

    settings.RUNTIME["monetag_postback"] = bool(policy.monetag_postback)
    settings.RUNTIME["adsgram_secret"] = bool(settings.ADSGRAM_REWARD_SECRET)
    policy.adsgram_reward_secret = bool(settings.ADSGRAM_REWARD_SECRET)
    service.configure(service.Deps(sessions=db, policy=policy, monetag=monetag_store, select_zone=select_zone,
                                   sdk_url=sdk_url, clock=clock))
    ctx = Ctx(auth=auth, identify=identify, send_file=send_file, db=db, token_re=token_re, telegram_errors=telegram_errors,
              forbidden=forbidden, admin_token=settings.ADS_ADMIN_TOKEN, reward_secret=settings.ADSGRAM_REWARD_SECRET,
              manager=manager, ads_required=ads_required)
    js = (HERE / "adapters.js").read_text(encoding="utf-8")
    admin_html = (HERE / "admin_ads.html").read_text(encoding="utf-8")

    def ip_of(request: Request) -> str:
        fwd = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        return fwd or (request.client.host if request.client else "unknown")

    async def body_of(request: Request) -> dict:
        try:
            b = await request.json()
        except Exception:  # noqa: BLE001
            return {}
        return b if isinstance(b, dict) else {}

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        """Structured, secret-free 422 for /api/*; other paths keep FastAPI's default body."""
        path = request.url.path
        if not path.startswith("/api/"):
            return await request_validation_exception_handler(request, exc)
        payload = validation_payload(exc.errors())
        slog.event("validation_error", level="warning", path=path, method=request.method,
                   content_type=(request.headers.get("content-type") or "")[:60], details=payload["details"])
        return JSONResponse(payload, status_code=422)

    async def checked_body(request: Request):
        body, bad = await read_json_body(request)
        return body, (JSONResponse(bad, status_code=400) if bad else None)

    def guard(request: Request, authorization):
        bad = admin_check(ctx, authorization, ip_of(request))
        if bad:
            raise HTTPException(status_code=bad[0], detail=bad[1])

    @app.get("/ads/adapters.js")
    async def adapters_js():
        return Response(js, media_type="application/javascript", headers={"Cache-Control": "public, max-age=300"})

    @app.post("/api/smart/start")
    async def smart_start(request: Request):
        body, bad = await checked_body(request)
        if bad:
            return bad
        return await h_start(ctx, body, ip_of(request), country(request) if country else None)

    @app.post("/api/smart/report")
    async def smart_report(request: Request):
        body, bad = await checked_body(request)
        if bad:
            return bad
        return await h_report(ctx, body, ip_of(request))

    @app.post("/api/smart/done")
    async def smart_done(request: Request):
        body, bad = await checked_body(request)
        if bad:
            return bad
        return await h_done(ctx, body, ip_of(request))

    if settings.ADSGRAM_REWARD_SECRET:
        @app.get("/api/adsgram/reward")
        async def adsgram_reward(request: Request):
            status, text = await h_adsgram_reward(ctx, dict(request.query_params), ip_of(request))
            return PlainTextResponse(text, status_code=status)

    if ctx.admin_token:
        @app.get("/admin/ads", response_class=HTMLResponse)
        async def admin_page():
            return HTMLResponse(admin_html, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                                                     "X-Frame-Options": "DENY"})

        @app.get("/admin/ads/api/overview")
        async def admin_overview(request: Request, days: int = 7, authorization: str | None = Header(default=None)):
            guard(request, authorization)
            return await h_admin_overview(days)

        @app.get("/admin/ads/api/config")
        async def admin_get_config(request: Request, authorization: str | None = Header(default=None)):
            guard(request, authorization)
            return await h_admin_config()

        @app.post("/admin/ads/api/config")
        async def admin_set_config(request: Request, authorization: str | None = Header(default=None)):
            guard(request, authorization)
            status, out = await h_admin_patch_config(await body_of(request))
            return JSONResponse(out, status_code=status)

        @app.post("/admin/ads/api/revenue")
        async def admin_revenue(request: Request, authorization: str | None = Header(default=None)):
            guard(request, authorization)
            status, out = await h_admin_revenue(await body_of(request))
            return JSONResponse(out, status_code=status)

        @app.get("/admin/ads/api/rewards")
        async def admin_rewards(request: Request, limit: int = 50, authorization: str | None = Header(default=None)):
            guard(request, authorization)
            return await h_admin_rewards(limit)

    return ctx
