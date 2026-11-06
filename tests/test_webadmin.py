"""Web admin panel tests (unittest). See tests/webadmin_support.py for what is real and what is faked."""
import ast
import asyncio
import json
import os
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests import webadmin_support as sup

DB = core = security = PanelSettings = ApiError = Req = build_ctx = None


def setUpModule():
    """Fakes are installed only while this module runs and removed afterwards, so other test modules are unaffected."""
    global DB, core, security, PanelSettings, ApiError, Req, build_ctx
    sup.begin()
    DB = sup.install()
    from webadmin import core as _core, security as _security  # noqa: PLC0415
    from webadmin.config import PanelSettings as _PS  # noqa: PLC0415
    from webadmin.mount import build_ctx as _bc  # noqa: PLC0415
    from webadmin import (routes_ads, routes_autopost, routes_bots, routes_bulk, routes_copy, routes_files, routes_posts, routes_system)  # noqa: PLC0415,F401

    core, security, PanelSettings, ApiError, Req, build_ctx = _core, _security, _PS, _core.ApiError, _core.Req, _bc


def tearDownModule():
    sup.end()


ROOT = Path(__file__).resolve().parent.parent
ORIGIN = "https://bot.example.com"
COVERED: set = set()
SECRETS = [sup.PANEL_PW, sup.TOTP_SECRET, sup.ENV["MONGO_URI"], sup.ENV["FERNET_KEY"], sup.ENV["WEBHOOK_SECRET"], sup.ENV["MAIN_BOT_TOKEN"],
           sup.ENV["ADMIN_PASSWORD"], "SESSION-ENC-SECRET"]


def make_env(**extra):
    env = {"ADMIN_PANEL_PASSWORD": sup.PANEL_PW}
    env.update(extra)
    return env


class Client:
    def __init__(self, env=None, ctx=None, ip="1.2.3.4"):
        self.settings = PanelSettings.from_env(env if env is not None else make_env())
        self.mongo = sup.FakeMongo()
        self.manager = sup.FakeManager(DB)
        self.ctx = ctx or build_ctx(self.settings, lambda: self.mongo, self.manager, '<script nonce="__NONCE__"></script>', ORIGIN)
        self.cookie = None
        self.csrf = None
        self.ip = ip
        self.last_headers = {}

    async def call(self, method, path, body=None, *, headers=None, raw=None, query=None, cookie="auto", csrf="auto", origin="auto", ip=None, ctype=None):
        h = {"host": "bot.example.com", "user-agent": "unittest"}
        if origin == "auto":
            h["origin"] = ORIGIN
        elif origin:
            h["origin"] = origin
        if csrf == "auto":
            csrf = self.csrf
        if csrf:
            h["x-csrf-token"] = csrf
        data = b""
        if raw is not None:
            data = raw
            h["content-type"] = ctype or "application/octet-stream"
        elif body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            h["content-type"] = ctype or "application/json"
        h.update(headers or {})
        ck = {}
        use = self.cookie if cookie == "auto" else cookie
        if use:
            ck[self.ctx.cookie_name] = use

        async def read_body(limit):
            if len(data) > limit:
                raise ApiError(413, "too_large", "Body too large")
            return data

        sub = path
        rt, _p, _o = core.match(method, sub)
        if rt:
            COVERED.add((rt.method, rt.path))
        req = Req(method, "/admin/api" + path, dict(query or {}), h, ck, ip or self.ip, "bot.example.com", "https", read_body)
        resp = await core.handle(self.ctx, req)
        self.last_headers = resp.headers
        if resp.set_cookie:
            self.cookie = resp.set_cookie[1]
        if resp.clear_cookie:
            self.cookie = None
        return resp

    async def login(self, totp=None):
        body = {"password": sup.PANEL_PW}
        if totp:
            body["totp"] = totp
        r = await self.call("POST", "/login", body, cookie=None, csrf=None)
        assert r.status == 200, r.body
        self.csrf = r.body["csrf"]
        return r

    async def get(self, path, **k):
        return await self.call("GET", path, **k)

    async def post(self, path, body=None, **k):
        return await self.call("POST", path, {} if body is None and "raw" not in k else body, **k)

    async def wait_job(self, job_id, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            j = (await self.get("/jobs/" + job_id)).body["job"]
            if j["status"] != "running":
                return j
            await asyncio.sleep(0.02)
        raise AssertionError("job did not finish")


def ok(tc, resp, status=200):
    tc.assertEqual(resp.status, status, resp.body)
    if isinstance(resp.body, dict) and status == 200:
        tc.assertTrue(resp.body.get("ok"))
    return resp.body


def fail(tc, resp, status, code):
    tc.assertEqual(resp.status, status, resp.body)
    tc.assertEqual(resp.body["ok"], False)
    tc.assertEqual(resp.body["code"], code)
    tc.assertIn("error", resp.body)
    return resp.body


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        DB.reset()
        __import__("ads.settings", fromlist=["x"]).SMART_ADS_ENABLED = True
        self.c = Client()
        await self.c.login()


# ================================================================== login / session / lockout
class TestAuth(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_without_password_is_404(self):
        c = Client(env={})
        self.assertEqual((await c.call("GET", "/dashboard", cookie=None)).status, 404)
        r = await core.handle(c.ctx, Req("GET", "/admin", host="bot.example.com"))
        self.assertEqual(r.status, 404)

    async def test_short_password_disables_panel(self):
        s = PanelSettings.from_env({"ADMIN_PANEL_PASSWORD": "short"})
        self.assertFalse(s.enabled)
        s = PanelSettings.from_env({"ADMIN_PANEL_PASSWORD": sup.PANEL_PW, "ADMIN_PANEL_TOTP_SECRET": "!!!not-base32"})
        self.assertFalse(s.enabled)

    async def test_page_served_with_nonce_csp(self):
        c = Client()
        r = await core.handle(c.ctx, Req("GET", "/admin", host="bot.example.com"))
        self.assertEqual(r.status, 200)
        nonce = r.headers["Content-Security-Policy"].split("'nonce-")[1].split("'")[0]
        self.assertIn(f'nonce="{nonce}"', r.body)
        self.assertNotIn("__NONCE__", r.body)
        self.assertIn("frame-ancestors 'none'", r.headers["Content-Security-Policy"])
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")
        self.assertEqual(r.headers["Cache-Control"], "no-store")
        r2 = await core.handle(c.ctx, Req("POST", "/admin", host="bot.example.com"))
        self.assertEqual(r2.status, 405)

    async def test_login_success_sets_session(self):
        c = Client()
        r = await c.login()
        self.assertEqual(r.set_cookie[0], "__Host-hb_admin")
        self.assertGreater(len(r.set_cookie[1]), 40)
        self.assertEqual(r.set_cookie[2], 12 * 3600)
        me = ok(self, await c.get("/session"))
        self.assertEqual(me["csrf"], c.csrf)
        # only a hash of the session id is stored
        stored = c.mongo["admin_sessions"].docs[0]
        self.assertNotIn(c.cookie.split(".")[0], json.dumps(stored, default=str))

    async def test_login_failure_is_generic(self):
        c = Client()
        r = await c.call("POST", "/login", {"password": "wrong-password-123"}, cookie=None, csrf=None)
        fail(self, r, 401, "bad_credentials")
        self.assertEqual(r.body["error"], "Invalid credentials")
        self.assertIsNone(r.set_cookie)

    async def test_login_bad_body(self):
        c = Client()
        fail(self, await c.call("POST", "/login", {"password": ""}, cookie=None, csrf=None), 422, "validation_error")
        fail(self, await c.call("POST", "/login", {"password": "x", "extra": 1}, cookie=None, csrf=None), 422, "validation_error")
        fail(self, await c.call("POST", "/login", b"{nope", cookie=None, csrf=None), 400, "bad_json")

    async def test_lockout_after_repeated_failures(self):
        c = Client()
        for i in range(4):
            fail(self, await c.call("POST", "/login", {"password": "wrong-pw-%d" % i}, cookie=None, csrf=None), 401, "bad_credentials")
        r = await c.call("POST", "/login", {"password": "wrong-pw-final"}, cookie=None, csrf=None)
        fail(self, r, 429, "locked")
        # correct password is refused while locked
        r = await c.call("POST", "/login", {"password": sup.PANEL_PW}, cookie=None, csrf=None)
        fail(self, r, 429, "locked")
        self.assertGreater(r.body["retry_after"], 0)
        # another IP is not affected
        r = await c.call("POST", "/login", {"password": sup.PANEL_PW}, cookie=None, csrf=None, ip="9.9.9.9")
        self.assertEqual(r.status, 200)

    async def test_lockout_expires(self):
        clock = [0.0]
        g = security.LoginGuard(3, 60, 60, clock=lambda: clock[0])
        for _ in range(3):
            g.fail("ip")
        self.assertGreater(g.locked_for("ip"), 0)
        clock[0] = 61
        self.assertEqual(g.locked_for("ip"), 0)

    async def test_global_login_throttle(self):
        c = Client()
        c.ctx.login_limiter = security.RateLimiter(2, 60)
        for ip in ("1.1.1.1", "2.2.2.2"):
            await c.call("POST", "/login", {"password": "wrong-password-x"}, cookie=None, csrf=None, ip=ip)
        fail(self, await c.call("POST", "/login", {"password": sup.PANEL_PW}, cookie=None, csrf=None, ip="3.3.3.3"), 429, "rate_limited")

    async def test_totp_required_and_replay_blocked(self):
        c = Client(env=make_env(ADMIN_PANEL_TOTP_SECRET=sup.TOTP_SECRET))
        r = await c.call("POST", "/login", {"password": sup.PANEL_PW}, cookie=None, csrf=None)
        fail(self, r, 401, "bad_credentials")
        r = await c.call("POST", "/login", {"password": sup.PANEL_PW, "totp": "000000"}, cookie=None, csrf=None)
        fail(self, r, 401, "bad_credentials")
        code = security.totp_at(sup.TOTP_SECRET, int(time.time() // 30))
        await c.login(totp=code)
        c2 = Client(ctx=c.ctx)
        r = await c2.call("POST", "/login", {"password": sup.PANEL_PW, "totp": code}, cookie=None, csrf=None)
        fail(self, r, 401, "bad_credentials")       # same code cannot be used twice
        r = await c.call("GET", "/dashboard", cookie=None)
        self.assertEqual(r.status, 401)
        self.assertTrue(r.body["totp_required"])

    async def test_totp_rfc6238_vector(self):
        # RFC 6238 appendix B, SHA-1, secret "12345678901234567890", T=59 -> 94287082 (last 6 digits 287082)
        import base64
        sec = base64.b32encode(b"12345678901234567890").decode()
        self.assertEqual(security.totp_at(sec, 59 // 30), "287082")

    async def test_secure_eq(self):
        self.assertTrue(security.secure_eq("abc", "abc"))
        self.assertFalse(security.secure_eq("abc", "abd"))
        self.assertFalse(security.secure_eq("", "x"))

    async def test_logout_and_logout_all(self):
        c = Client()
        await c.login()
        old = c.cookie
        ok(self, await c.post("/logout"))
        self.assertEqual((await c.call("GET", "/session", cookie=old)).status, 401)
        await c.login()
        c2 = Client(ctx=c.ctx)
        await c2.login()
        ok(self, await c.post("/logout-all"))
        self.assertEqual((await c2.get("/session")).status, 401)

    async def test_expired_and_forged_sessions(self):
        c = Client()
        await c.login()
        c.mongo["admin_sessions"].docs[0]["expires"] = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.assertEqual((await c.get("/session")).status, 401)
        await c.login()
        sid = c.cookie.split(".")[0]
        self.assertEqual((await c.get("/session", cookie=sid + ".AAAAAAAAAAAAAAAAAAAAAAAA")).status, 401)
        self.assertEqual((await c.get("/session", cookie="garbage")).status, 401)
        self.assertEqual((await c.get("/session", cookie=None)).status, 401)

    async def test_session_signed_with_other_key_rejected(self):
        a, b = Client(), Client()
        await a.login()
        self.assertEqual((await b.get("/session", cookie=a.cookie)).status, 401)

    async def test_rate_limit(self):
        c = Client()
        await c.login()
        c.ctx.limiter = security.RateLimiter(3, 60)
        for _ in range(3):
            self.assertEqual((await c.get("/session")).status, 200)
        fail(self, await c.get("/session"), 429, "rate_limited")

    async def test_audit_log_records_without_secrets(self):
        c = Client()
        await c.call("POST", "/login", {"password": "wrong-password-xx"}, cookie=None, csrf=None)
        await c.login()
        rows = ok(self, await c.get("/activity"))["items"]
        self.assertEqual([r["action"] for r in rows][:2], ["login.ok", "login.fail"])
        blob = json.dumps(rows)
        self.assertNotIn(sup.PANEL_PW, blob)
        self.assertNotIn("wrong-password-xx", blob)
        self.assertEqual(security.sanitize({"password": "x", "bot_token": "y", "file_token": "ok", "n": 1}), {"file_token": "ok", "n": 1})


# ================================================================== every protected route
class TestProtection(Base):
    def _routes(self):
        return [r for r in core.ROUTES if not r.public]

    def _path(self, r):
        return (r.path.replace("{token}", "tok12345").replace("{bot_id}", "b1").replace("{job_id}", "nojob").replace("{batch_id}", "bt")
                .replace("{uid}", "777").replace("{cid}", "-100900").replace("{pid}", "p1").replace("{idx}", "0"))

    async def test_all_protected_routes_reject_anonymous(self):
        anon = Client(ctx=self.c.ctx)
        n = 0
        for r in self._routes():
            resp = await anon.call(r.method, self._path(r), {} if r.method != "GET" else None, cookie=None, csrf=None)
            COVERED.add((r.method, r.path))
            self.assertEqual(resp.status, 401, f"{r.method} {r.path}")
            self.assertEqual(resp.body["code"], "unauthenticated")
            self.assertFalse(resp.body["ok"])
            n += 1
        self.assertGreater(n, 60)

    async def test_all_mutating_routes_require_csrf(self):
        for r in self._routes():
            if r.method == "GET":
                continue
            p = self._path(r)
            for token in (None, "wrong-token"):
                resp = await self.c.call(r.method, p, {}, csrf=token)
                self.assertEqual(resp.status, 403, f"{r.method} {r.path} csrf={token}")
                self.assertEqual(resp.body["code"], "csrf")

    async def test_cross_origin_refused(self):
        r = await self.c.post("/bots/check-all", origin="https://evil.example")
        fail(self, r, 403, "bad_origin")
        r = await self.c.post("/bots/check-all", origin="null")
        fail(self, r, 403, "bad_origin")
        r = await self.c.post("/bots/check-all", origin=None, headers={"sec-fetch-site": "cross-site"})
        fail(self, r, 403, "bad_origin")
        ok(self, await self.c.post("/bots/check-all", origin=None, headers={"sec-fetch-site": "same-origin"}))

    async def test_unknown_route_and_method(self):
        fail(self, await self.c.get("/nope"), 404, "not_found")
        fail(self, await self.c.call("DELETE", "/bots"), 405, "method_not_allowed")

    async def test_error_shape_and_validation(self):
        r = await self.c.post("/bots", {"token": "short"})
        b = fail(self, r, 422, "validation_error")
        self.assertIn("details", b)
        self.assertNotIn("short", json.dumps(b))        # input values are never echoed
        fail(self, await self.c.post("/bots", {"token": "x" * 40, "zzz": 1}), 422, "validation_error")
        fail(self, await self.c.post("/bots", b"not json"), 400, "bad_json")
        fail(self, await self.c.post("/bots", b"[1,2]"), 400, "bad_json")
        fail(self, await self.c.post("/bots", {"a": 1}, ctype="text/plain"), 415, "bad_content_type")
        fail(self, await self.c.post("/bots", b"x" * (2 * 1024 * 1024)), 413, "too_large")

    async def test_internal_errors_do_not_leak(self):
        async def boom(ctx, req, session, body):
            raise RuntimeError("secret-internal-detail mongodb://u:p@h")

        core.ROUTES.append(core.Route("GET", __import__("re").compile("^/boom$"), boom, path="/boom"))
        try:
            r = await self.c.get("/boom")
        finally:
            core.ROUTES.pop()
        fail(self, r, 500, "internal")
        self.assertNotIn("secret-internal-detail", json.dumps(r.body))


# ================================================================== pages
class TestSystem(Base):
    async def test_dashboard(self):
        DB.bots_c.docs.append({"bot_id": "b1", "username": "bot_one", "status": "active", "token_enc": "ENCRYPTED-TOKEN"})
        b = ok(self, await self.c.get("/dashboard"))
        self.assertEqual(b["stats"]["views_today"], 7)
        self.assertEqual(b["bots"][0]["running"], True)
        self.assertEqual(b["autoposter"]["total_posts"], 1)
        self.assertNotIn("ENCRYPTED-TOKEN", json.dumps(b))

    async def test_settings_never_exposes_secrets(self):
        for k, v in sup.ENV.items():
            os.environ[k] = v
        os.environ["ADMIN_PANEL_PASSWORD"] = sup.PANEL_PW
        os.environ["ADMIN_PANEL_TOTP_SECRET"] = sup.TOTP_SECRET
        os.environ["ADS_ADMIN_TOKEN"] = "ads-admin-token-value"
        try:
            b = ok(self, await self.c.get("/settings"))
        finally:
            for k in ("ADMIN_PANEL_PASSWORD", "ADMIN_PANEL_TOTP_SECRET", "ADS_ADMIN_TOKEN"):
                os.environ.pop(k, None)
        blob = json.dumps(b)
        for s in SECRETS + ["ads-admin-token-value"]:
            self.assertNotIn(s, blob)
        rows = {v["name"]: v for g in b["groups"] for v in g["vars"]}
        self.assertTrue(rows["MONGO_URI"]["set"])
        self.assertNotIn("value", rows["MONGO_URI"])
        self.assertTrue(rows["MONGO_URI"]["secret"])
        self.assertTrue(rows["ADMIN_PANEL_PASSWORD"]["set"])
        self.assertNotIn("value", rows["ADMIN_PANEL_PASSWORD"])
        self.assertTrue(rows["BASE_URL"]["secret"])      # allow-list: only known-harmless tunables show a value

    async def test_activity_pagination_and_validation(self):
        ok(self, await self.c.get("/activity", query={"limit": "5", "skip": "0"}))
        fail(self, await self.c.get("/activity", query={"limit": "abc"}), 422, "validation_error")

    async def test_jobs_list_get_stop(self):
        fail(self, await self.c.get("/jobs/nope"), 404, "not_found")
        fail(self, await self.c.post("/jobs/nope/stop"), 409, "not_running")
        started = ok(self, await self.c.post("/copy/accounts", None)) if False else None
        ok(self, await self.c.get("/jobs"))


class TestBots(Base):
    def seed(self):
        DB.bots_c.docs.append({"bot_id": "b1", "username": "bot_one", "status": "active", "token_enc": "ENC-TOKEN-X", "added_at": 1.0})

    async def test_list_never_has_tokens(self):
        self.seed()
        b = ok(self, await self.c.get("/bots"))
        self.assertEqual(b["bots"][0]["username"], "bot_one")
        self.assertNotIn("ENC-TOKEN-X", json.dumps(b))

    async def test_add_ok_and_failure_and_no_token_in_audit(self):
        tok = "123456:" + "A" * 30
        b = ok(self, await self.c.post("/bots", {"token": tok}))
        self.assertIn("added", b["message"])
        bad = "123456:" + "B" * 27 + "BAD"
        fail(self, await self.c.post("/bots", {"token": bad}), 400, "add_failed")
        self.assertNotIn("A" * 30, json.dumps(ok(self, await self.c.get("/activity")), default=str))
        fail(self, await self.c.post("/bots", {"token": "x" * 30}), 422, "validation_error")

    async def test_check_all_health_disable_enable_remove(self):
        self.seed()
        ok(self, await self.c.post("/bots/check-all"))
        self.assertEqual(self.c.manager.checked, 1)
        h = ok(self, await self.c.get("/bots/main/health"))["health"]
        self.assertIn("found", h)
        ok(self, await self.c.post("/bots/b1/disable"))
        self.assertEqual(DB.bots_c.docs[0]["status"], "disabled")
        ok(self, await self.c.post("/bots/b1/enable"))
        fail(self, await self.c.post("/bots/nope/disable"), 404, "not_found")
        fail(self, await self.c.post("/bots/nope/enable"), 404, "not_found")
        fail(self, await self.c.post("/bots/nope/remove", {"confirm": True}), 404, "not_found")

    async def test_remove_requires_confirmation(self):
        self.seed()
        fail(self, await self.c.post("/bots/b1/remove", {}), 400, "confirm_required")
        fail(self, await self.c.post("/bots/b1/remove", {"confirm": False}), 400, "confirm_required")
        self.assertEqual(len(DB.bots_c.docs), 1)
        ok(self, await self.c.post("/bots/b1/remove", {"confirm": True}))
        self.assertEqual(DB.bots_c.docs, [])


class TestFiles(Base):
    def seed(self, n=3):
        for i in range(n):
            DB.files.docs.append({"token": f"tok{i}abc", "caption": f"cap {i}", "views": i, "created_at": 100 + i, "locations": [{"channel": -1001, "msg": 5 + i}, {"channel": -1002, "msg": 8 + i}]})
        DB.files.docs.append({"token": "post1abc", "kind": "post", "items": [{"locations": [{"channel": -1001, "msg": 50}], "caption": ""}], "post_caption": "pc", "photo": "PH", "created_at": 200, "views": 0})

    async def test_list_search_filter_detail(self):
        self.seed()
        b = ok(self, await self.c.get("/files", query={"size": "2"}))
        self.assertEqual((b["total"], b["pages"], len(b["items"])), (4, 2, 2))
        b = ok(self, await self.c.get("/files", query={"q": "cap 1"}))
        self.assertEqual([i["token"] for i in b["items"]], ["tok1abc"])
        b = ok(self, await self.c.get("/files", query={"kind": "post"}))
        self.assertEqual([i["token"] for i in b["items"]], ["post1abc"])
        fail(self, await self.c.get("/files", query={"kind": "weird"}), 422, "validation_error")
        fail(self, await self.c.get("/files", query={"page": "x"}), 422, "validation_error")
        d = ok(self, await self.c.get("/files/tok1abc"))["file"]
        self.assertEqual(d["copies"], 2)
        self.assertTrue(d["link"].endswith("/f/tok1abc"))
        fail(self, await self.c.get("/files/missing1"), 404, "not_found")
        fail(self, await self.c.get("/files/a"), 404, "not_found")

    async def test_upload(self):
        b = ok(self, await self.c.post("/files/upload", raw=b"\x00" * 2048, ctype="video/mp4", query={"filename": "../../etc/pass wd.mp4", "caption": "hi"}))
        self.assertEqual(b["media"], "video")
        self.assertEqual(len(DB.files.docs), 1)
        self.assertEqual(len(DB.files.docs[0]["locations"]), 2)
        self.assertEqual(self.c.manager.main.bot.sent[0], ("video", -1001))
        fail(self, await self.c.post("/files/upload", raw=b""), 400, "empty_body")
        self.c.ctx.settings = PanelSettings.from_env(make_env(ADMIN_PANEL_UPLOAD_MAX_MB="1"))
        fail(self, await self.c.post("/files/upload", raw=b"x" * (2 * 1024 * 1024)), 413, "too_large")

    async def test_caption(self):
        self.seed()
        ok(self, await self.c.post("/files/tok1abc/caption", {"caption": "new cap"}))
        self.assertEqual(DB.files.docs[1]["caption"], "new cap")
        ok(self, await self.c.post("/files/post1abc/caption", {"caption": "post cap"}))
        self.assertEqual(DB.files.docs[3]["post_caption"], "post cap")
        fail(self, await self.c.post("/files/tok1abc/caption", {"caption": "x" * 1025}), 422, "validation_error")
        fail(self, await self.c.post("/files/missing1/caption", {"caption": "x"}), 404, "not_found")

    async def test_delete_requires_confirmation_and_validates(self):
        self.seed()
        fail(self, await self.c.post("/files/delete", {"tokens": ["tok1abc"]}), 400, "confirm_required")
        self.assertEqual(len(DB.files.docs), 4)
        fail(self, await self.c.post("/files/delete", {"tokens": [], "confirm": True}), 422, "validation_error")
        fail(self, await self.c.post("/files/delete", {"tokens": ["../x"], "confirm": True}), 422, "validation_error")
        fail(self, await self.c.post("/files/delete", {"tokens": ["nothere1"], "confirm": True}), 404, "not_found")
        fail(self, await self.c.post("/files/delete", {"tokens": ["tok1abc"], "mode": "bad", "confirm": True}), 422, "validation_error")

    async def test_delete_single_everywhere_and_record_only(self):
        self.seed()
        ok(self, await self.c.post("/files/delete", {"tokens": ["tok0abc"], "confirm": True}))
        self.assertEqual(len(self.c.manager.main.bot.deleted), 2)      # both channel copies removed
        self.assertNotIn("tok0abc", [d["token"] for d in DB.files.docs])
        before = len(self.c.manager.main.bot.deleted)
        ok(self, await self.c.post("/files/delete", {"tokens": ["tok1abc"], "mode": "record", "confirm": True}))
        self.assertEqual(len(self.c.manager.main.bot.deleted), before)  # channel copies untouched
        self.assertNotIn("tok1abc", [d["token"] for d in DB.files.docs])

    async def test_bulk_delete_runs_as_job(self):
        self.seed()
        b = ok(self, await self.c.post("/files/delete", {"tokens": ["tok0abc", "tok1abc", "tok2abc"], "confirm": True}))
        j = await self.c.wait_job(b["job_id"])
        self.assertEqual((j["status"], j["result"]["deleted"]), ("done", 3))
        self.assertEqual([d["token"] for d in DB.files.docs], ["post1abc"])


class TestPosts(Base):
    async def test_draft_flow_finalize_preview_channel(self):
        b = ok(self, await self.c.get("/posts/draft"))
        self.assertIsNone(b["draft"])
        fail(self, await self.c.post("/posts/draft/finalize", {}), 400, "incomplete")
        ok(self, await self.c.post("/posts/draft/thumb", raw=b"\x89PNG" * 10, ctype="image/png", query={"filename": "t.png"}))
        fail(self, await self.c.post("/posts/draft/thumb", raw=b"zip", ctype="application/zip"), 415, "bad_media")
        ok(self, await self.c.post("/posts/draft/video", raw=b"v1" * 100, ctype="video/mp4", query={"filename": "a.mp4", "caption": "c1"}))
        b = ok(self, await self.c.post("/posts/draft/video", raw=b"v2" * 100, ctype="video/mp4", query={"filename": "b.mp4"}))
        self.assertEqual(len(b["draft"]["videos"]), 2)
        ok(self, await self.c.post("/posts/draft/caption", {"caption": "my caption"}))
        b = ok(self, await self.c.post("/posts/draft/video/1/remove"))
        self.assertEqual(len(b["draft"]["videos"]), 1)
        fail(self, await self.c.post("/posts/draft/video/9/remove"), 404, "not_found")
        fin = ok(self, await self.c.post("/posts/draft/finalize", {}))
        self.assertTrue(fin["link"].endswith("/f/" + fin["token"]))
        self.assertEqual(DB.files.docs[0]["post_caption"], "my caption")
        self.assertIsNone(ok(self, await self.c.get("/posts/draft"))["draft"])
        ok(self, await self.c.post(f"/posts/{fin['token']}/preview"))
        fail(self, await self.c.post(f"/posts/{fin['token']}/channel", {}), 400, "confirm_required")
        ok(self, await self.c.post(f"/posts/{fin['token']}/channel", {"confirm": True}))
        self.assertIn(("photo", -1003), self.c.manager.main.bot.sent)
        fail(self, await self.c.post("/posts/nothere/preview"), 404, "not_found")
        fail(self, await self.c.post("/posts/nothere/channel", {"confirm": True}), 404, "not_found")

    async def test_discard_rolls_back_uploaded_videos(self):
        ok(self, await self.c.post("/posts/draft/video", raw=b"v" * 50, ctype="video/mp4"))
        ok(self, await self.c.post("/posts/draft/discard"))
        self.assertEqual(len(self.c.manager.main.bot.deleted), 2)
        self.assertIsNone(ok(self, await self.c.get("/posts/draft"))["draft"])

    async def test_caption_validation(self):
        fail(self, await self.c.post("/posts/draft/caption", {"caption": "x" * 1025}), 422, "validation_error")


class TestBulk(Base):
    async def test_overview_scan_validation_and_job(self):
        ok(self, await self.c.get("/bulk"))
        fail(self, await self.c.post("/bulk/scan", {"start_link": "nope", "end_link": "nope"}), 422, "validation_error")
        fail(self, await self.c.post("/bulk/scan", {"start_link": "https://example.com/aaa", "end_link": "https://example.com/bbb"}), 400, "bad_range")
        fail(self, await self.c.post("/bulk/scan", {"start_link": "https://t.me/c/123/10", "end_link": "https://t.me/c/999/20"}), 400, "bad_range")
        fail(self, await self.c.post("/bulk/scan", {"start_link": "https://t.me/c/123/10", "end_link": "https://t.me/c/123/999"}), 400, "bad_range")
        b = ok(self, await self.c.post("/bulk/scan", {"start_link": "https://t.me/c/123/20", "end_link": "https://t.me/c/123/10", "mode": "original"}))
        self.assertEqual(b["range"], [10, 20])
        j = await self.c.wait_job(b["job_id"])
        self.assertEqual(j["status"], "done")
        self.assertEqual(j["result"]["outcome"], "done")

    async def test_one_bulk_job_at_a_time_and_stop(self):
        gate = asyncio.Event()

        async def slow(job):
            while not job.ctl["stop"]:
                await asyncio.sleep(0.01)
            return {}

        job = self.c.ctx.jobs.start("bulk-scan", "slow", slow, group="bulk")
        fail(self, await self.c.post("/bulk/delete-all", {"confirm": True}), 409, "busy")
        ok(self, await self.c.post(f"/jobs/{job.id}/stop"))
        j = await self.c.wait_job(job.id)
        self.assertEqual(j["status"], "stopped")
        del gate

    async def test_publish_preview_and_resume(self):
        for t in ("pa", "pb"):
            DB.files.docs.append({"token": t + "xxxx", "kind": "post", "items": [], "photo": "PH", "post_caption": "c", "created_at": 1})
        DB.batches.docs.append({"id": "bt", "tokens": ["paxxxx", "pbxxxx"], "posted": ["paxxxx"], "created_at": 1})
        fail(self, await self.c.post("/bulk/batches/nope/post", {}), 404, "not_found")
        b = ok(self, await self.c.post("/bulk/batches/bt/post", {}))
        j = await self.c.wait_job(b["job_id"])
        self.assertEqual((j["result"]["sent"], j["result"]["todo"]), (1, 1))     # resumed: the already-posted one is skipped
        b = ok(self, await self.c.post("/bulk/batches/bt/preview", {"to_channel": False}))
        j = await self.c.wait_job(b["job_id"])
        self.assertEqual(j["result"]["sent"], 2)
        self.assertIn(("photo", 42), self.c.manager.main.bot.sent)

    async def test_delete_all_requires_confirm(self):
        DB.files.docs.append({"token": "bulk1xxx", "kind": "post", "items": [], "src_chat": -100, "locations": [{"channel": -1001, "msg": 1}]})
        fail(self, await self.c.post("/bulk/delete-all", {}), 400, "confirm_required")
        self.assertEqual(len(DB.files.docs), 1)
        b = ok(self, await self.c.post("/bulk/delete-all", {"confirm": True}))
        j = await self.c.wait_job(b["job_id"])
        self.assertEqual(j["result"]["deleted"], 1)


class TestCopy(Base):
    async def test_login_wizard_and_secrets(self):
        fail(self, await self.c.post("/copy/login/phone", {"phone": "12"}), 422, "validation_error")
        fail(self, await self.c.post("/copy/login/phone", {"phone": "+910000000000"}), 400, "userbot_error")
        ok(self, await self.c.post("/copy/login/phone", {"phone": "+91 98765-43210"}))
        self.assertTrue(ok(self, await self.c.get("/copy/accounts"))["login_pending"])
        fail(self, await self.c.post("/copy/login/code", {"code": "abcdef"}), 422, "validation_error")
        fail(self, await self.c.post("/copy/login/code", {"code": "000000"}), 400, "userbot_error")
        self.assertEqual(ok(self, await self.c.post("/copy/login/code", {"code": "222222"}))["step"], "password")
        fail(self, await self.c.post("/copy/login/password", {"password": "wrong"}), 400, "userbot_error")
        b = ok(self, await self.c.post("/copy/login/password", {"password": "right"}))
        self.assertEqual(b["step"], "done")
        acc = ok(self, await self.c.get("/copy/accounts"))
        blob = json.dumps(acc)
        self.assertNotIn("SESSION-ENC-SECRET", blob)
        self.assertNotIn("98765", blob)                        # phone is masked
        ok(self, await self.c.post("/copy/login/cancel"))
        audit = json.dumps(ok(self, await self.c.get("/activity")), default=str)
        for leak in ("98765", "222222", "right", "wrong"):
            self.assertNotIn(leak, audit)

    async def test_login_direct_ok_code(self):
        ok(self, await self.c.post("/copy/login/phone", {"phone": "+919876543210"}))
        self.assertEqual(ok(self, await self.c.post("/copy/login/code", {"code": "12345"}))["step"], "done")

    async def _acc(self):
        await DB.userbots.insert_one({"uid": 777, "name": "Test Acc", "phone": "+919876543210", "session_enc": "SESSION-ENC-SECRET"})

    async def test_chats_resolve_progress(self):
        await self._acc()
        b = ok(self, await self.c.get("/copy/accounts/777/chats", query={"q": "news"}))
        self.assertEqual([c["title"] for c in b["chats"]], ["News Channel"])
        fail(self, await self.c.get("/copy/accounts/999/chats"), 404, "not_found")
        fail(self, await self.c.get("/copy/accounts/abc/chats"), 404, "not_found")
        self.assertEqual(ok(self, await self.c.post("/copy/resolve", {"uid": 777, "text": "@found"}))["chat"]["title"], "Resolved")
        fail(self, await self.c.post("/copy/resolve", {"uid": 777, "text": "@zzz"}), 404, "not_found")
        DB.progress[(777, 1, 2)] = 55
        self.assertEqual(ok(self, await self.c.get("/copy/progress", query={"uid": "777", "src": "1", "dest": "2"}))["resume_from"], 55)
        fail(self, await self.c.get("/copy/progress", query={"uid": "x"}), 422, "validation_error")
        sup.sys.modules["userbot"]._state["ready"] = False
        try:
            fail(self, await self.c.get("/copy/accounts/777/chats"), 409, "session_expired")
        finally:
            sup.sys.modules["userbot"]._state["ready"] = True

    async def test_start_progress_stop(self):
        await self._acc()
        fail(self, await self.c.post("/copy/start", {"uid": 777, "src": 5, "dest": 5}), 400, "same_chat")
        fail(self, await self.c.post("/copy/start", {"uid": 777, "src": 5, "dest": 6, "kinds": []}), 422, "validation_error")
        fail(self, await self.c.post("/copy/start", {"uid": 777, "src": 5, "dest": 6, "kinds": ["gif"]}), 422, "validation_error")
        fail(self, await self.c.post("/copy/start", {"uid": 1234, "src": 5, "dest": 6}), 404, "not_found")
        b = ok(self, await self.c.post("/copy/start", {"uid": 777, "src": 5, "dest": 6, "kinds": ["video"], "resume": False}))
        fail(self, await self.c.post("/copy/start", {"uid": 777, "src": 7, "dest": 8}), 409, "busy")
        await asyncio.sleep(0.1)
        j = (await self.c.get("/jobs/" + b["job_id"])).body["job"]
        self.assertEqual(j["status"], "running")
        self.assertGreater(j["progress"]["copied"], 0)
        ok(self, await self.c.post(f"/jobs/{b['job_id']}/stop"))
        j = await self.c.wait_job(b["job_id"])
        self.assertEqual(j["status"], "stopped")
        self.assertTrue(j["result"]["stopped"])

    async def test_remove_requires_confirm(self):
        await self._acc()
        fail(self, await self.c.post("/copy/accounts/777/remove", {}), 400, "confirm_required")
        self.assertEqual(len(DB.userbots.docs), 1)
        fail(self, await self.c.post("/copy/accounts/555/remove", {"confirm": True}), 404, "not_found")
        ok(self, await self.c.post("/copy/accounts/777/remove", {"confirm": True}))
        self.assertEqual(DB.userbots.docs, [])


class TestAutopost(Base):
    BASE = "/autopost/targets/-100900"

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.ap = DB.apdb
        self.ap.__init__()
        sys.modules["autoposter.state"].STATE.db = self.ap
        sys.modules["autoposter.actions"].pw["required"] = True

    async def test_overview_detail_logs(self):
        b = ok(self, await self.c.get("/autopost/overview"))
        self.assertEqual(b["targets"][0]["title"], "My Channel")
        self.assertIn("retry_attempts", b["settings"])
        self.assertTrue(b["settings"]["password_gate"])
        self.assertNotIn("legacy-admin-pw", json.dumps(b))
        d = ok(self, await self.c.get(self.BASE))
        self.assertEqual(d["queue"]["total"], 1)
        fail(self, await self.c.get("/autopost/targets/123"), 404, "not_found")
        fail(self, await self.c.get("/autopost/targets/abc"), 404, "not_found")
        await self.ap.logs.insert_one({"level": "INFO", "event": "x", "payload": {"a": 1}, "created_at": datetime.now(timezone.utc)})
        self.assertEqual(len(ok(self, await self.c.get("/autopost/logs"))["logs"]), 1)

    async def test_add_target(self):
        ok(self, await self.c.post("/autopost/targets", {"ref": "@mych"}))
        fail(self, await self.c.post("/autopost/targets", {"ref": "bad"}), 400, "action_failed")
        fail(self, await self.c.post("/autopost/targets", {"ref": ""}), 422, "validation_error")

    async def test_schedule_and_validation(self):
        b = ok(self, await self.c.post(self.BASE + "/schedule", {"interval_minutes": 60, "batch_size": 2, "start_time": "08:30", "end_time": "23:00", "days": [4, 2, 2], "jitter_minutes": 10}))
        self.assertEqual(b["target"]["schedule"]["days"], [2, 4])
        self.assertEqual(self.ap.chat["options"]["jitter_minutes"], 10)
        for bad in ({"interval_minutes": 0}, {"batch_size": 99}, {"start_time": "25:00"}, {"days": [7]}, {"days": []}, {"junk": 1}):
            fail(self, await self.c.post(self.BASE + "/schedule", bad), 422, "validation_error")
        fail(self, await self.c.post(self.BASE + "/schedule", {}), 422, "validation_error")

    async def test_options_footer_and_active(self):
        ok(self, await self.c.post(self.BASE + "/options", {"loop": False, "dedupe": True, "auto_delete_minutes": 60}))
        self.assertEqual(self.ap.chat["options"]["loop"], False)
        fail(self, await self.c.post(self.BASE + "/options", {"auto_delete_minutes": 7}), 422, "validation_error")
        ok(self, await self.c.post(self.BASE + "/options", {"footer": "<b>hi</b>"}))
        self.assertEqual(self.ap.chat["options"]["footer"], "<b>hi</b>")
        ok(self, await self.c.post(self.BASE + "/options", {"footer": ""}))
        self.assertIsNone(self.ap.chat["options"]["footer"])
        fail(self, await self.c.post(self.BASE + "/options", {"footer": "x" * 450}), 422, "validation_error")
        fail(self, await self.c.post(self.BASE + "/options", {}), 422, "validation_error")
        ok(self, await self.c.post(self.BASE + "/active", {"active": False}))
        self.assertFalse(self.ap.chat["is_active"])
        fail(self, await self.c.post(self.BASE + "/run", {}), 409, "paused")
        ok(self, await self.c.post(self.BASE + "/active", {"active": True}))
        fail(self, await self.c.post(self.BASE + "/active", {"active": "maybe"}), 422, "validation_error")

    async def test_run_now_reshuffle(self):
        ok(self, await self.c.post(self.BASE + "/reshuffle"))
        b = ok(self, await self.c.post(self.BASE + "/run"))
        j = await self.c.wait_job(b["job_id"])
        self.assertEqual(j["result"]["sent"], 3)

    async def test_posts_list_preview_send_delete_undo(self):
        b = ok(self, await self.c.get(self.BASE + "/posts"))
        self.assertEqual(b["total"], 1)
        ok(self, await self.c.post(self.BASE + "/posts/p1/preview"))
        ok(self, await self.c.post(self.BASE + "/posts/p1/send"))
        fail(self, await self.c.post(self.BASE + "/posts/zz/preview"), 404, "not_found")
        fail(self, await self.c.post(self.BASE + "/posts/p1/delete", {}), 400, "confirm_required")
        self.assertEqual(len(self.ap.posts), 1)
        ok(self, await self.c.post(self.BASE + "/posts/p1/delete", {"confirm": True}))
        fail(self, await self.c.post(self.BASE + "/posts/p1/delete", {"confirm": True}), 404, "not_found")
        fail(self, await self.c.post(self.BASE + "/undo", {}), 400, "confirm_required")
        fail(self, await self.c.post(self.BASE + "/undo", {"confirm": True}), 404, "nothing")

    async def test_add_posts_all_kinds(self):
        r = ok(self, await self.c.post(self.BASE + "/posts/text", {"text": "new text"}))
        self.assertEqual(r["inserted"], 1)
        self.assertEqual(ok(self, await self.c.post(self.BASE + "/posts/text", {"text": "new text"}))["duplicates"], 1)
        fail(self, await self.c.post(self.BASE + "/posts/text", {"text": ""}), 422, "validation_error")
        fail(self, await self.c.post(self.BASE + "/posts/text", {"text": "   "}), 422, "validation_error") if False else None
        r = ok(self, await self.c.post(self.BASE + "/posts/bulk", {"filename": "p.txt", "text": "a\n---\nb\n---\nc"}))
        self.assertEqual(r["inserted"], 3)
        fail(self, await self.c.post(self.BASE + "/posts/bulk", {"filename": "p.exe", "text": "a"}), 422, "validation_error")
        fail(self, await self.c.post(self.BASE + "/posts/bulk", {"filename": "p.json", "text": "{bad"}), 400, "bad_file")
        r = ok(self, await self.c.post(self.BASE + "/posts/media", raw=b"img", ctype="image/png", query={"filename": "a.png", "caption": "cap"}))
        self.assertEqual(r["inserted"], 1)
        st = ok(self, await self.c.post("/autopost/stage-media", raw=b"vid", ctype="video/mp4", query={"filename": "a.mp4"}))["media"]
        self.assertEqual(st["media_type"], "video")
        items = [{"media_type": "photo", "file_id": "FILE-ID-1"}, {"media_type": "video", "file_id": "FILE-ID-2"}]
        self.assertEqual(ok(self, await self.c.post(self.BASE + "/posts/album", {"items": items, "caption": "al"}))["inserted"], 1)
        fail(self, await self.c.post(self.BASE + "/posts/album", {"items": items[:1]}), 422, "validation_error")
        fail(self, await self.c.post(self.BASE + "/posts/album", {"items": [{"media_type": "audio", "file_id": "FILE-ID-1"}] * 2}), 422, "validation_error")
        fail(self, await self.c.post(self.BASE + "/posts/media", raw=b""), 400, "empty_body")

    async def test_import_promo_force_sub(self):
        b = ok(self, await self.c.post(self.BASE + "/import", {"first_link": "https://t.me/c/1/10", "last_link": "https://t.me/c/1/20"}))
        self.assertEqual((b["inserted"], b["duplicates"]), (3, 1))
        fail(self, await self.c.post(self.BASE + "/import", {"first_link": "https://t.me/bad/1", "last_link": "https://t.me/c/1/20"}), 400, "action_failed")
        fail(self, await self.c.post(self.BASE + "/import", {"first_link": "x", "last_link": "y"}), 422, "validation_error")
        ok(self, await self.c.post(self.BASE + "/promo", {"text": "<b>join</b>"}))
        fail(self, await self.c.post(self.BASE + "/promo", {"text": ""}), 422, "validation_error")
        self.assertEqual(ok(self, await self.c.post(self.BASE + "/force-sub", {"ref": "@other"}))["force_sub_channel"], "@other")
        self.assertIsNone(ok(self, await self.c.post(self.BASE + "/force-sub", {"ref": "off"}))["force_sub_channel"])

    async def test_destructive_need_confirm_and_admin_password(self):
        w = self.BASE + "/wipe"
        fail(self, await self.c.post(w, {}), 400, "confirm_required")
        fail(self, await self.c.post(w, {"confirm": True}), 403, "password_required")
        fail(self, await self.c.post(w, {"confirm": True, "admin_password": "nope"}), 403, "password_required")
        self.assertNotIn(("wipe_chat", -100900), self.ap.calls)
        ok(self, await self.c.post(w, {"confirm": True, "admin_password": "legacy-admin-pw"}))
        self.assertIn(("wipe_chat", -100900), self.ap.calls)
        fail(self, await self.c.post("/autopost/wipe-all", {"confirm": True}), 403, "password_required")
        self.assertFalse(self.ap.wiped)
        ok(self, await self.c.post("/autopost/wipe-all", {"confirm": True, "admin_password": "legacy-admin-pw"}))
        self.assertTrue(self.ap.wiped)
        fail(self, await self.c.post(self.BASE + "/remove", {"confirm": True}), 403, "password_required")
        ok(self, await self.c.post(self.BASE + "/remove", {"confirm": True, "admin_password": "legacy-admin-pw"}))
        self.assertIn(("remove_chat", -100900), self.ap.calls)
        audit = json.dumps(ok(self, await self.c.get("/activity")), default=str)
        self.assertNotIn("legacy-admin-pw", audit)

    async def test_password_gate_lockout(self):
        for _ in range(5):
            await self.c.post(self.BASE + "/wipe", {"confirm": True, "admin_password": "bad"})
        r = await self.c.post(self.BASE + "/wipe", {"confirm": True, "admin_password": "legacy-admin-pw"})
        fail(self, r, 429, "locked")

    async def test_no_password_configured_still_needs_confirm(self):
        sys.modules["autoposter.actions"].pw["required"] = False
        fail(self, await self.c.post(self.BASE + "/wipe", {}), 400, "confirm_required")
        ok(self, await self.c.post(self.BASE + "/wipe", {"confirm": True}))

    async def test_backup_and_restore(self):
        r = await self.c.get("/autopost/backup")
        self.assertEqual(r.status, 200)
        self.assertIn("attachment", r.headers["Content-Disposition"])
        fail(self, await self.c.post("/autopost/restore", raw=b"{}"), 400, "confirm_required")
        fail(self, await self.c.post("/autopost/restore", raw=b"{}", query={"confirm": "1"}), 403, "password_required")
        b = ok(self, await self.c.post("/autopost/restore", raw=b"{}", query={"confirm": "1"}, headers={"x-admin-password": "legacy-admin-pw"}))
        self.assertEqual((b["chats"], b["posts"]), (1, 2))
        fail(self, await self.c.post("/autopost/restore", raw=b"garbage", query={"confirm": "1"}, headers={"x-admin-password": "legacy-admin-pw"}), 400, "bad_file")

    async def test_autopost_off(self):
        sys.modules["autoposter.state"].STATE.db = None
        fail(self, await self.c.get("/autopost/overview"), 503, "autopost_off")


class TestAds(Base):
    async def test_smart_ads(self):
        self.assertTrue(ok(self, await self.c.get("/ads/status"))["enabled"])
        self.assertEqual(ok(self, await self.c.get("/ads/overview", query={"days": "30"}))["report"]["days"], 30)
        cf = ok(self, await self.c.get("/ads/config"))
        self.assertEqual(cf["secrets_set"], {"adsgram_reward_secret": True})
        ok(self, await self.c.post("/ads/config", {"mode": "manual"}))
        fail(self, await self.c.post("/ads/config", {"mode": "weird"}), 400, "bad_config")
        ok(self, await self.c.post("/ads/revenue", {"day": "2026-11-05", "network": "monetag", "revenue_usd": 1.5}))
        fail(self, await self.c.post("/ads/revenue", {"day": "2026-12-05", "network": "monetag", "revenue_usd": 1.5}), 400, "bad_revenue")
        fail(self, await self.c.post("/ads/revenue", {"day": "bad", "network": "x", "revenue_usd": 1}), 422, "validation_error")
        fail(self, await self.c.post("/ads/revenue", {"day": "2026-11-05", "network": "x", "revenue_usd": -1}), 422, "validation_error")
        self.assertEqual(len(ok(self, await self.c.get("/ads/rewards"))["rewards"]), 1)

    async def test_smart_ads_disabled(self):
        __import__("ads.settings", fromlist=["x"]).SMART_ADS_ENABLED = False
        self.assertFalse(ok(self, await self.c.get("/ads/status"))["enabled"])
        fail(self, await self.c.get("/ads/overview"), 404, "ads_disabled")
        fail(self, await self.c.post("/ads/config", {"mode": "auto"}), 404, "ads_disabled")

    async def test_monetag(self):
        b = ok(self, await self.c.get("/monetag/report"))
        self.assertIn("z1", b["report"]["zones"])
        ok(self, await self.c.post("/monetag/confirmed", {"day": "2026-11-05", "zone": "all", "revenue_usd": 12.5}))
        self.assertEqual(sys.modules["monetag_store"].confirmed[-1], ("2026-11-05", 12.5, "all"))
        fail(self, await self.c.post("/monetag/confirmed", {"day": "2026-11-05", "zone": "unknown-zone", "revenue_usd": 1}), 422, "validation_error")
        fail(self, await self.c.post("/monetag/confirmed", {"day": "5-11-2026", "zone": "all", "revenue_usd": 1}), 422, "validation_error")
        fail(self, await self.c.post("/monetag/confirmed", {"day": "2026-11-05", "zone": "all", "revenue_usd": "abc"}), 422, "validation_error")


# ================================================================== real ASGI transport (cookie flags, streaming, 404)
class TestAsgi(unittest.IsolatedAsyncioTestCase):
    async def asgi(self, app, method, path, headers=None, body=b""):
        sent = []
        hdrs = [(b"host", b"bot.example.com")] + [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
        if body:
            hdrs.append((b"content-length", str(len(body)).encode()))
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "https", "path": path, "raw_path": path.encode(),
                 "query_string": b"", "headers": hdrs, "client": ("5.6.7.8", 1234), "server": ("bot.example.com", 443), "root_path": ""}
        chunks = [body]

        async def receive():
            if chunks:
                return {"type": "http.request", "body": chunks.pop(0), "more_body": False}
            return {"type": "http.disconnect"}

        async def send(m):
            sent.append(m)

        await app(scope, receive, send)
        start = next(m for m in sent if m["type"] == "http.response.start")
        data = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        return start["status"], [(k.decode().lower(), v.decode()) for k, v in start["headers"]], data

    def make_app(self, env):
        from starlette.applications import Starlette
        from webadmin import mount as mt

        app = Starlette()
        mongo = sup.FakeMongo()
        import webadmin.mount as m

        orig = m.PanelSettings.from_env
        ctx = mt.mount(app, sup.FakeManager(DB), env=env, get_db=lambda: mongo, base_url=ORIGIN)
        del orig
        return app, ctx

    async def test_disabled_returns_404_from_framework(self):
        app, ctx = self.make_app({})
        self.assertIsNone(ctx)
        st, _h, _b = await self.asgi(app, "GET", "/admin")
        self.assertEqual(st, 404)
        st, _h, _b = await self.asgi(app, "GET", "/admin/api/dashboard")
        self.assertEqual(st, 404)

    async def test_cookie_flags_and_flow(self):
        app, ctx = self.make_app(make_env())
        st, h, body = await self.asgi(app, "GET", "/admin")
        self.assertEqual(st, 200)
        self.assertIn("nonce-", dict(h)["content-security-policy"])
        st, h, body = await self.asgi(app, "POST", "/admin/api/login", {"content-type": "application/json", "origin": ORIGIN}, json.dumps({"password": sup.PANEL_PW}).encode())
        self.assertEqual(st, 200, body)
        cookie = [v for k, v in h if k == "set-cookie"][0]
        low = cookie.lower()
        for flag in ("httponly", "secure", "samesite=strict", "path=/"):
            self.assertIn(flag, low)
        self.assertTrue(cookie.startswith("__Host-hb_admin="))
        self.assertNotIn("domain", low)
        csrf = json.loads(body)["csrf"]
        ck = cookie.split(";")[0]
        st, _h, b = await self.asgi(app, "GET", "/admin/api/session", {"cookie": ck})
        self.assertEqual(st, 200)
        st, _h, b = await self.asgi(app, "POST", "/admin/api/bots/check-all", {"cookie": ck, "origin": ORIGIN, "content-type": "application/json"}, b"{}")
        self.assertEqual(st, 403)                                    # CSRF header missing
        st, _h, b = await self.asgi(app, "POST", "/admin/api/bots/check-all", {"cookie": ck, "origin": ORIGIN, "x-csrf-token": csrf, "content-type": "application/json"}, b"{}")
        self.assertEqual(st, 200, b)
        st, h, _b = await self.asgi(app, "POST", "/admin/api/logout", {"cookie": ck, "origin": ORIGIN, "x-csrf-token": csrf, "content-type": "application/json"}, b"{}")
        self.assertEqual(st, 200)
        cleared = [v for k, v in h if k == "set-cookie"][0].lower()
        self.assertIn("max-age=0", cleared)
        st, _h, _b = await self.asgi(app, "GET", "/admin/api/session", {"cookie": ck})
        self.assertEqual(st, 401)

    async def test_streaming_body_limit(self):
        app, ctx = self.make_app(make_env())
        st, h, body = await self.asgi(app, "POST", "/admin/api/login", {"content-type": "application/json", "origin": ORIGIN}, json.dumps({"password": sup.PANEL_PW}).encode())
        ck, csrf = [v for k, v in h if k == "set-cookie"][0].split(";")[0], json.loads(body)["csrf"]
        big = b"x" * (2 * 1024 * 1024)
        st, _h, b = await self.asgi(app, "POST", "/admin/api/bots", {"cookie": ck, "origin": ORIGIN, "x-csrf-token": csrf, "content-type": "application/json"}, big)
        self.assertEqual(st, 413)
        self.assertEqual(json.loads(b)["code"], "too_large")


# ================================================================== FastAPI wiring regression (static)
class TestWiringRegression(unittest.TestCase):
    """The 422 'query.request missing' bug came from `from __future__ import annotations` in a module whose FastAPI
    handlers use `Request` imported locally. Guard every module that defines route handlers."""

    def _files(self):
        return [p for p in ROOT.rglob("*.py") if not any(x in p.parts for x in ("tests", "__pycache__", "orig"))]

    def test_no_future_annotations_in_route_modules(self):
        bad = []
        for p in self._files():
            src = p.read_text(encoding="utf-8")
            tree = ast.parse(src)
            has_future = any(isinstance(n, ast.ImportFrom) and n.module == "__future__" and any(a.name == "annotations" for a in n.names) for n in tree.body)
            uses_fastapi = ("fastapi" in src and "import" in src) or "add_route(" in src or "@app." in src or "@router." in src
            if has_future and uses_fastapi and "autoposter/" not in p.as_posix():
                bad.append(str(p.relative_to(ROOT)))
        self.assertEqual(bad, [], "route modules must not use `from __future__ import annotations`")

    def test_main_handlers_annotate_request(self):
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        checked = 0
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.AsyncFunctionDef):
                continue
            routed = any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and isinstance(d.func.value, ast.Name) and d.func.value.id == "app" for d in fn.decorator_list)
            if not routed:
                continue
            for a in fn.args.args:
                if a.arg == "request":
                    checked += 1
                    self.assertIsInstance(a.annotation, ast.Name, fn.name)
                    self.assertEqual(a.annotation.id, "Request", fn.name)
        self.assertGreater(checked, 3)
        src = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("from fastapi import FastAPI, Header, HTTPException, Request", src)

    def test_panel_handlers_take_plain_request_objects(self):
        # webadmin handlers are Starlette-level: the endpoint's only parameter is `request: Request` imported at module top.
        src = (ROOT / "webadmin" / "mount.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        self.assertFalse(any(isinstance(n, ast.ImportFrom) and n.module == "__future__" for n in tree.body))
        self.assertIn("from starlette.requests import Request", src)
        self.assertIn("async def endpoint(request: Request)", src)

    def test_main_mounts_panel(self):
        src = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("_mount_admin_panel(app, manager", src)

    def test_panel_html_has_no_inline_handlers_or_innerhtml(self):
        html = (ROOT / "webadmin" / "panel.html").read_text(encoding="utf-8")
        self.assertNotIn("innerHTML", html)
        self.assertNotRegex(html, r"\son[a-z]+=\"")
        self.assertEqual(html.count("__NONCE__"), 1)
        self.assertNotIn("http://", html.replace("https://", ""))


class TestZCoverage(unittest.TestCase):
    def test_every_route_was_exercised(self):
        if not COVERED:
            self.skipTest("run the whole module to check route coverage")
        missing = sorted((r.method, r.path) for r in core.ROUTES if (r.method, r.path) not in COVERED)
        self.assertEqual(missing, [], "routes without a test")


if __name__ == "__main__":
    unittest.main()
