"""Attaches the admin panel to the existing FastAPI app.

Starlette-level routes (app.add_route) are used on purpose: the handlers take a plain `Request`, so FastAPI's
annotation resolution (and the `from __future__ import annotations` / 422 hazard) never comes into play.
Everything security-relevant lives in webadmin.core and is unit-tested without FastAPI."""
import asyncio
import logging
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import (routes_ads, routes_autopost, routes_bots, routes_bulk, routes_copy, routes_files, routes_posts,  # noqa: F401
               routes_system)
from . import security
from .config import PanelSettings
from .core import ApiError, Ctx, Req, Resp, handle
from .jobs import JobRegistry

log = logging.getLogger("webadmin")
METHODS = ["GET", "HEAD", "POST", "PUT", "DELETE"]


def client_ip(request: Request, trust_proxy: bool) -> str:
    if trust_proxy:
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[-1].strip()[:64]      # the address the trusted edge proxy saw (earlier entries are client-supplied)
    return (request.client.host if request.client else "")[:64]


def make_reader(request: Request):
    async def read_body(limit: int) -> bytes:
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > limit:
            raise ApiError(413, "too_large", f"Body too large (max {limit // (1024 * 1024) or 1} MB)")
        buf = bytearray()
        async for chunk in request.stream():
            buf += chunk
            if len(buf) > limit:
                raise ApiError(413, "too_large", f"Body too large (max {limit // (1024 * 1024) or 1} MB)")
        return bytes(buf)

    return read_body


def to_req(request: Request, settings) -> Req:
    return Req(
        method=request.method, path=request.url.path, query=dict(request.query_params),
        headers={k.lower(): v for k, v in request.headers.items()}, cookies=dict(request.cookies),
        ip=client_ip(request, settings.trust_proxy), host=request.headers.get("host", ""),
        scheme=request.url.scheme, read_body=make_reader(request),
    )


def to_response(resp: Resp, ctx: Ctx) -> Response:
    if isinstance(resp.body, dict):
        out = JSONResponse(resp.body, status_code=resp.status)
    else:
        body = resp.body if resp.body is not None else b""
        out = Response(body, status_code=resp.status, media_type=resp.content_type)
    for k, v in resp.headers.items():
        out.headers[k] = v
    flags = dict(httponly=True, secure=ctx.settings.secure_cookies, samesite="strict", path="/")
    if resp.set_cookie:
        name, value, max_age = resp.set_cookie
        out.set_cookie(name, value, max_age=max_age, **flags)
    if resp.clear_cookie:
        out.set_cookie(ctx.cookie_name, "", max_age=0, expires=0, **flags)
    return out


def build_ctx(settings: PanelSettings, get_db, manager=None, html: str = "", base_url: str = "") -> Ctx:
    key = settings.password + "|" + __import__("os").environ.get("FERNET_KEY", "")
    return Ctx(
        settings=settings,
        sessions=security.SessionStore(get_db, key, settings.session_hours * 3600),
        audit=security.Audit(get_db),
        guard=security.LoginGuard(settings.max_fails, settings.lockout_seconds, settings.lockout_seconds),
        limiter=security.RateLimiter(240, 60),
        manager=manager, jobs=JobRegistry(), html=html, base_url=base_url,
        pw_guard=security.LoginGuard(5, 900, 900),
        login_limiter=security.RateLimiter(40, 900),
    )


def make_endpoint(ctx: Ctx):
    state = {"ready": False}
    lock = asyncio.Lock()

    async def ensure_indexes():
        if state["ready"]:
            return
        async with lock:
            if state["ready"]:
                return
            try:
                await ctx.sessions.init()
                await ctx.audit.init()
            except Exception:  # noqa: BLE001 - index creation must not take the panel down
                log.exception("admin panel index creation failed")
            state["ready"] = True

    async def endpoint(request: Request) -> Response:
        await ensure_indexes()
        try:
            resp = await handle(ctx, to_req(request, ctx.settings))
        except ApiError as e:           # e.g. 413 raised while streaming a body
            from .core import error_resp

            resp = error_resp(e)
        return to_response(resp, ctx)

    return endpoint


def mount(app, manager=None, *, env=None, get_db=None, base_url: str = "") -> Ctx | None:
    """Register /admin and /admin/api/*. Does nothing (=> 404) unless ADMIN_PANEL_PASSWORD is set and valid."""
    settings = PanelSettings.from_env(env)
    if not settings.enabled:
        log.info("admin panel disabled (%s)", settings.problem or "ADMIN_PANEL_PASSWORD not set")
        return None
    if get_db is None:
        import db

        def get_db():
            return db._db
    html = (Path(__file__).with_name("panel.html")).read_text(encoding="utf-8")
    ctx = build_ctx(settings, get_db, manager, html, base_url)
    endpoint = make_endpoint(ctx)
    app.add_route("/admin", endpoint, methods=METHODS, include_in_schema=False)
    app.add_route("/admin/", endpoint, methods=METHODS, include_in_schema=False)
    app.add_route("/admin/api/{path:path}", endpoint, methods=METHODS, include_in_schema=False)
    log.info("admin panel mounted at /admin (totp=%s)", settings.totp_enabled)
    return ctx
