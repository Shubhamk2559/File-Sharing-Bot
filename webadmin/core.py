"""Framework-independent request handling for the admin panel.

mount.py turns a Starlette request into `Req`, calls `handle()`, and turns the `Resp` back. Everything security
relevant (auth, CSRF, origin check, validation, error shape) happens here so it can be unit-tested without FastAPI."""
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from pydantic import BaseModel, ValidationError

from . import security

log = logging.getLogger("webadmin")

SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
MAX_JSON_BYTES = 1 * 1024 * 1024


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra


def err(status: int, code: str, message: str, **extra) -> ApiError:
    return ApiError(status, code, message, **extra)


@dataclass
class Req:
    method: str
    path: str
    query: dict = field(default_factory=dict)
    headers: dict = field(default_factory=dict)       # lower-case keys
    cookies: dict = field(default_factory=dict)
    ip: str = ""
    host: str = ""
    scheme: str = "https"
    read_body: Optional[Callable[[int], Awaitable[bytes]]] = None   # async (limit) -> bytes, raises ApiError 413


@dataclass
class Resp:
    status: int = 200
    body: Any = None                 # dict -> JSON, bytes/str as-is
    content_type: str = "application/json"
    headers: dict = field(default_factory=dict)
    set_cookie: Optional[tuple] = None   # (name, value, max_age)
    clear_cookie: bool = False


@dataclass
class Route:
    method: str
    pattern: "re.Pattern"
    handler: Callable
    model: Optional[type] = None
    public: bool = False
    csrf: bool = True
    raw: bool = False                # handler reads the body itself (uploads)
    max_body: int = MAX_JSON_BYTES
    audit: Optional[str] = None
    path: str = ""


ROUTES: list = []


def route(method: str, path: str, model: Optional[type] = None, *, public=False, csrf=True, raw=False,
          max_body=MAX_JSON_BYTES, audit: Optional[str] = None):
    """Register `async def handler(ctx, req, session, body, **path_params) -> dict | Resp`.
    Path params look like {token} and match one path segment."""
    rx = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", path) + "$")

    def deco(fn):
        ROUTES.append(Route(method.upper(), rx, fn, model, public, csrf, raw, max_body, audit, path))
        return fn

    return deco


def match(method: str, sub: str):
    """-> (route|None, params, path_exists_for_other_method)"""
    other = False
    for r in ROUTES:
        m = r.pattern.match(sub)
        if m:
            if r.method == method or (method == "HEAD" and r.method == "GET"):
                return r, m.groupdict(), False
            other = True
    return None, {}, other


@dataclass
class Ctx:
    settings: Any
    sessions: Any
    audit: Any
    guard: Any
    limiter: Any
    manager: Any = None
    jobs: Any = None
    html: str = ""
    base_url: str = ""
    last_totp: int = -1
    pw_guard: Any = None     # lockout for the Auto-Posting ADMIN_PASSWORD gate
    login_limiter: Any = None  # global cap on login attempts (defends against distributed guessing)

    @property
    def cookie_name(self) -> str:
        return "__Host-hb_admin" if self.settings.secure_cookies else "hb_admin"


def json_resp(status: int, data: dict, **headers) -> Resp:
    return Resp(status, data, "application/json", headers)


def error_resp(e: ApiError) -> Resp:
    body = {"ok": False, "code": e.code, "error": e.message}
    body.update(e.extra)
    return json_resp(e.status, body)


def _validation_body(ve: ValidationError) -> dict:
    details = []
    for d in ve.errors()[:10]:
        loc = ".".join(str(x) for x in d.get("loc", ()))
        details.append({"field": loc, "msg": str(d.get("msg", "invalid"))[:160]})    # never echo the input value
    first = details[0] if details else {"field": "", "msg": "invalid"}
    return {"ok": False, "code": "validation_error", "error": f"{first['field'] or 'body'}: {first['msg']}", "details": details}


def _origin_ok(ctx: Ctx, req: Req) -> bool:
    origin = req.headers.get("origin")
    if origin is None:
        site = req.headers.get("sec-fetch-site")
        return site in (None, "same-origin", "none")
    if origin == "null":
        return False
    host = origin.split("://", 1)[-1]
    allowed = {req.host}
    if ctx.base_url:
        allowed.add(ctx.base_url.split("://", 1)[-1].rstrip("/"))
    return host in allowed


def page_resp(ctx: Ctx) -> Resp:
    nonce = secrets.token_urlsafe(16)
    csp = (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; img-src 'self' data:; "
           "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
    return Resp(200, ctx.html.replace("__NONCE__", nonce), "text/html; charset=utf-8", {"Content-Security-Policy": csp})


async def handle(ctx: Ctx, req: Req) -> Resp:
    resp = await _handle(ctx, req)
    resp.headers.update({
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer", "Cross-Origin-Resource-Policy": "same-origin",
    })
    if ctx.settings.secure_cookies:
        resp.headers["Strict-Transport-Security"] = "max-age=31536000"
    return resp


async def _handle(ctx: Ctx, req: Req) -> Resp:
    if not ctx.settings.enabled:
        return Resp(404, "Not Found", "text/plain")           # panel does not exist unless configured
    path = req.path
    if path in ("/admin", "/admin/"):
        if req.method not in ("GET", "HEAD"):
            return Resp(405, "Method Not Allowed", "text/plain")
        return page_resp(ctx)
    if not path.startswith("/admin/api/"):
        return Resp(404, "Not Found", "text/plain")
    sub = path[len("/admin/api"):]
    rt, params, other = match(req.method, sub)

    session = None
    if not (rt and rt.public):
        session = await ctx.sessions.get(req.cookies.get(ctx.cookie_name))
        if not session:
            return json_resp(401, {"ok": False, "code": "unauthenticated", "error": "Login required",
                                   "totp_required": ctx.settings.totp_enabled})
        if not ctx.limiter.allow(session["_id"]):
            return error_resp(err(429, "rate_limited", "Too many requests, slow down"))
    if rt is None:
        return error_resp(err(405, "method_not_allowed", "Method not allowed") if other else err(404, "not_found", "Unknown API route"))

    if req.method not in SAFE_METHODS:
        if not _origin_ok(ctx, req):
            return error_resp(err(403, "bad_origin", "Cross-origin request refused"))
        if rt.csrf:
            sent = req.headers.get("x-csrf-token", "")
            if not session or not security.secure_eq(sent, session.get("csrf", "")):
                return error_resp(err(403, "csrf", "Missing or invalid CSRF token"))

    try:
        body = None
        if rt.model is not None:
            raw = await req.read_body(rt.max_body) if req.read_body else b""
            ctype = req.headers.get("content-type", "").split(";")[0].strip().lower()
            if raw and ctype != "application/json":
                raise err(415, "bad_content_type", "Content-Type must be application/json")
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            except (ValueError, UnicodeDecodeError):
                raise err(400, "bad_json", "Body is not valid JSON") from None
            if not isinstance(payload, dict):
                raise err(400, "bad_json", "Body must be a JSON object")
            body = rt.model.model_validate(payload)
        result = await rt.handler(ctx, req, session, body, **params)
        if isinstance(result, Resp):
            return result
        result = dict(result or {})
        audit_detail = result.pop("_audit", None)
        result.setdefault("ok", True)
        if rt.audit:
            await ctx.audit.log(rt.audit, ip=req.ip, ok=True, **(audit_detail or {}))
        return Resp(200, result)
    except ValidationError as ve:
        return Resp(422, _validation_body(ve))
    except ApiError as e:
        if rt.audit and e.status >= 400 and e.code not in ("validation_error",):
            await ctx.audit.log(rt.audit, ip=req.ip, ok=False, code=e.code)
        return error_resp(e)
    except Exception:  # noqa: BLE001
        log.exception("admin api error on %s %s", req.method, sub)      # message only; no request bodies/secrets logged
        return error_resp(err(500, "internal", "Internal error - see server logs"))


# helpers shared by route modules --------------------------------------------------------------
def need_manager(ctx: Ctx):
    if ctx.manager is None:
        raise err(503, "no_manager", "Bot manager is not running in this process")
    return ctx.manager


def need_main_bot(ctx: Ctx):
    m = need_manager(ctx)
    if getattr(m, "main", None) is None:
        raise err(503, "no_main_bot", "The main bot is not running in this ROLE")
    return m.main.bot


def require_confirm(body) -> None:
    if not getattr(body, "confirm", False):
        raise err(400, "confirm_required", "This action is destructive - send confirm=true after confirming in the UI")


def now_ts() -> float:
    return time.time()
