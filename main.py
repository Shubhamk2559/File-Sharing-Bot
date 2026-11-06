import asyncio
import hashlib
import hmac
import html
import json
import logging
import math
import random
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qsl

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from telegram import Update
from telegram.error import Forbidden, TelegramError

import db
import userbot
import monetag_engine as engine
import monetag_store as mstore
from ads import engine as ads_engine, routes as ads_routes, settings as ads_settings
from ads.verify import Policy as AdsPolicy
from config import (
    ADS_REQUIRED,
    AD_VERIFY_MODE,
    BLOCK_NON_VALUED,
    DIRECT_MINIAPP,
    MINIAPP_SHORT_NAME,
    MIN_AD_SECONDS,
    MONETAG_ADMIN_TOKEN,
    MONETAG_DEFAULT_ZONE,
    MONETAG_GEO_HEADER,
    MONETAG_POSTBACK_SECRET,
    MONETAG_SDK_URL,
    MONETAG_SHOW_LOG,
    MONETAG_WINDOW_DAYS,
    MONETAG_ZONE_ID,
    MONETAG_ZONES,
    POSTBACK_WAIT_SECONDS,
    ROLE,
)
from delivery import send_file
from manager import BotManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


class _RedactSecrets(logging.Filter):
    """Monetag postback URL me secret query-string me hota hai; access log me na chhape."""

    _RE = re.compile(r"(secret|token)=[^&\s\"]+", re.I)

    def filter(self, record):
        if isinstance(record.args, tuple):
            record.args = tuple(self._RE.sub(r"\1=***", a) if isinstance(a, str) else a for a in record.args)
        return True


logging.getLogger("uvicorn.access").addFilter(_RedactSecrets())

manager = BotManager()
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await manager.start()
    task = asyncio.create_task(manager.background_loop())
    yield
    task.cancel()
    await userbot.shutdown()
    await manager.stop()


app = FastAPI(lifespan=lifespan)


# Web admin panel (/admin). Mounted only when ADMIN_PANEL_PASSWORD is set; otherwise /admin simply 404s.
from webadmin.mount import mount as _mount_admin_panel  # noqa: E402

_mount_admin_panel(app, manager, base_url=BASE_URL)


@app.api_route("/", methods=["GET", "HEAD"])
async def root():
    return PlainTextResponse(f"ok ({ROLE})")


@app.post("/webhook/{bot_id}")
async def webhook(
    bot_id: str,
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    application = manager.apps.get(bot_id)
    expected = manager.secret_for(bot_id)
    if not application or not hmac.compare_digest(x_telegram_bot_api_secret_token or "", expected):
        raise HTTPException(status_code=403)

    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.update_queue.put(update)
    return {"ok": True}


_REDIRECT_TPL = Path(__file__).with_name("redirect.html").read_text(encoding="utf-8")


def _redirect_page(target: str = "", message: str = "", status: int = 200) -> HTMLResponse:
    page = (
        _REDIRECT_TPL.replace(
            "__META__",
            f'<meta http-equiv="refresh" content="8;url={html.escape(target, quote=True)}">' if target else "",
        )
        .replace("__TARGET_ATTR__", html.escape(target, quote=True))
        .replace("__TARGET_JSON__", json.dumps(target).replace("</", "<\\/"))
        .replace("__MSG_JSON__", json.dumps(message).replace("</", "<\\/"))
    )
    return HTMLResponse(
        page,
        status_code=status,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


def _telegram_target(username: str, token: str) -> str:
    if DIRECT_MINIAPP:
        # Direct Mini App link: bina button dabaye app khulta hai, token start_param me jata hai
        if MINIAPP_SHORT_NAME:
            return f"https://t.me/{username}/{MINIAPP_SHORT_NAME}?startapp={token}"
        return f"https://t.me/{username}?startapp={token}"
    return f"https://t.me/{username}?start={token}"


@app.get("/f/{token}")
async def permanent_link(token: str):
    if not TOKEN_RE.match(token):
        return _redirect_page(message="This link doesn't look right.", status=404)
    names = await manager.active_usernames()
    if not names:
        return _redirect_page(message="Service is unavailable right now. Please try again shortly.", status=503)
    return _redirect_page(_telegram_target(random.choice(names), token))


# ===================== WORKER only: Mini App =====================
def validate_init_data(init_data: str, bot_token: str, max_age: int = 3600):
    """Signature sahi ho to {"user": {...}, "start_param": str|None} warna None."""
    try:
        parsed = dict(parse_qsl(init_data or "", keep_blank_values=True))
        received = parsed.pop("hash", None)
        if not received:
            return None
        data_check = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, received):
            return None
        if time.time() - int(parsed.get("auth_date", "0")) > max_age:
            return None
        return {"user": json.loads(parsed["user"]), "start_param": parsed.get("start_param")}
    except Exception:  # noqa: BLE001
        return None


if ROLE in ("worker", "both"):
    # Default zone ka SDK tag pehle jaisa static rehta hai (koi extra latency nahi).
    # Doosre zones sirf tab dynamically load hote hain jab optimizer unhe chune.
    _DEFAULT_ZONE = MONETAG_DEFAULT_ZONE
    _CFG = json.dumps(
        {
            "zoneId": _DEFAULT_ZONE,
            "showLog": MONETAG_SHOW_LOG,
            "required": ADS_REQUIRED,
            "blockNonValued": BLOCK_NON_VALUED,
            "smart": ads_settings.SMART_ADS_ENABLED,   # multi-network flow (default off = legacy flow)
        }
    ).replace("</", "<\\/")

    _TAG = (
        f'<script src="{html.escape(engine.sdk_url(_DEFAULT_ZONE), quote=True)}" '
        f'data-zone="{html.escape(_DEFAULT_ZONE, quote=True)}" '
        f'data-sdk="show_{html.escape(_DEFAULT_ZONE, quote=True)}" '
        f'onerror="window.__sdkErr=1"></script>'
    )

    _HTML = (
        Path(__file__)
        .with_name("miniapp.html")
        .read_text(encoding="utf-8")
        .replace("__CFG__", _CFG)
        .replace("<!--AD_TAG-->", _TAG)
    )

    @app.get("/app", response_class=HTMLResponse)
    async def miniapp():
        return HTMLResponse(_HTML, headers={"Cache-Control": "no-store"})

    async def _auth(body: dict):
        """Existing session: sid present (from the bot button or a retry)."""
        sid = body.get("sid")
        s = await db.get_session(sid) if isinstance(sid, str) and sid else None
        if not s:
            return None, None, "Session expired. Please open the link again."
        application = manager.apps.get(s["bot_id"])
        if not application:
            return None, None, "Bot is unavailable right now. Please try again shortly."
        info = validate_init_data(body.get("init_data", ""), application.bot.token)
        if not info or info["user"].get("id") != s["user_id"]:
            return None, None, "Verification failed. Please open the link again."
        return s, application, None

    def _identify(init_data: str):
        """Direct launch: kaunse bot ne Mini App kholi, ye initData ke signature se pata chalta hai."""
        for application in list(manager.apps.values()):
            info = validate_init_data(init_data, application.bot.token)
            if info:
                return application, info
        return None, None

    def _country(request: Request):
        if not MONETAG_GEO_HEADER:
            return None
        c = (request.headers.get(MONETAG_GEO_HEADER) or "").strip().upper()
        return c if re.fullmatch(r"[A-Z]{2}", c) and c not in ("XX", "T1") else None

    # ---- Smart multi-network rotation (Monetag + AdsGram + Tads). Only mounted when SMART_ADS_ENABLED=true ----
    if ads_settings.SMART_ADS_ENABLED:
        ads_routes.register(
            app,
            auth=_auth,
            identify=_identify,
            send_file=send_file,
            db=db,
            token_re=TOKEN_RE,
            telegram_errors=(TelegramError,),
            forbidden=(Forbidden,),
            policy=AdsPolicy(
                monetag_postback=(AD_VERIFY_MODE == "postback"),
                min_seconds=MIN_AD_SECONDS,
                wait_seconds=POSTBACK_WAIT_SECONDS,
                block_non_valued=BLOCK_NON_VALUED,
            ),
            manager=manager,
            ads_required=ADS_REQUIRED,
            monetag_store=mstore,
            select_zone=engine.select_zone,
            sdk_url=engine.sdk_url,
            country=_country,
        )

    @app.post("/api/ad/start")
    async def ad_start(request: Request):
        body = await request.json()

        if isinstance(body.get("sid"), str) and body["sid"]:
            s, _application, err = await _auth(body)
            if err:
                return {"ok": False, "error": err}
        else:
            application, info = _identify(body.get("init_data", ""))
            if not application:
                return {"ok": False, "error": "Please open this inside Telegram."}
            token = info.get("start_param") or ""
            if not TOKEN_RE.match(token) or not await db.get_file(token):
                return {"ok": False, "error": "This link is invalid."}

            user_id = info["user"]["id"]
            # bot user ko message bhej sakta hai ya nahi (pehle check, ad dikhane se pehle)
            try:
                await application.bot.send_chat_action(user_id, "upload_document")
            except Forbidden:
                return {"ok": False, "code": "no_write", "bot": application.bot.username}
            except TelegramError:
                pass

            new_sid = secrets.token_urlsafe(12)
            await db.create_session(
                sid=new_sid,
                token=token,
                user_id=user_id,
                chat_id=user_id,
                bot_id=application.bot_data["bot_id"],
                required=ADS_REQUIRED,
            )
            s = await db.get_session(new_sid)

        if s["delivered"]:
            return {"ok": False, "error": "You already received this file."}

        # Har ad show ka apna unique ymid (Monetag docs: "unique ymid per ad show") + zone choice.
        zone = await engine.select_zone()
        ymid = f"{s['sid']}.{secrets.token_hex(4)}"
        await db.start_ad(s["sid"], ymid, zone)
        try:
            await mstore.record_attempt(ymid, s["sid"], zone, _country(request))
        except Exception:  # noqa: BLE001 — analytics kabhi delivery na roke
            logging.getLogger("monetag").exception("record_attempt failed")
        return {
            "ok": True,
            "sid": s["sid"],
            "ymid": ymid,
            "zone": zone,
            "sdk": engine.sdk_url(zone),
            "ads_done": s["ads_done"],
            "ads_required": s["ads_required"],
        }

    @app.post("/api/ad/fail")
    async def ad_fail(request: Request):
        """Client SDK error report (stats + auto-bench). Reward par koi asar nahi."""
        body = await request.json()
        s, _application, err = await _auth(body)
        if err:
            return {"ok": False}
        ymid = body.get("ymid")
        if isinstance(ymid, str) and ymid == s.get("cur_ymid"):
            try:
                await mstore.record_fail(ymid, s["sid"], str(body.get("reason") or "error")[:20])
            except Exception:  # noqa: BLE001
                logging.getLogger("monetag").exception("record_fail failed")
        return {"ok": True}

    @app.post("/api/ad/done")
    async def ad_done(request: Request):
        body = await request.json()
        s, application, err = await _auth(body)
        if err:
            return {"ok": False, "error": err}
        if s["delivered"]:
            return {"ok": False, "error": "You already received this file."}

        # Yeh wahi ad show hona chahiye jo is session ke liye abhi start hua tha
        ymid = body.get("ymid") if isinstance(body.get("ymid"), str) else None
        expected = s.get("cur_ymid")
        if (expected or AD_VERIFY_MODE == "postback") and ymid != expected:
            return {"ok": False, "error": "This ad session is outdated. Please try again."}

        started = s.get("ad_started_at")
        if not started:
            return {"ok": False, "error": "The ad didn't start. Please try again."}
        elapsed = time.time() - started
        if elapsed < MIN_AD_SECONDS:
            return {"ok": True, "wait": math.ceil(MIN_AD_SECONDS - elapsed)}

        # ---- server-side confirmation by Monetag postback (AD_VERIFY_MODE=postback) ----
        if AD_VERIFY_MODE == "postback":
            ad = await mstore.get_ad(ymid)
            if not ad or ad.get("sid") != s["sid"]:
                return {"ok": False, "error": "Verification failed. Please try again."}
            if not ad.get("pb_seen"):
                first = ad.get("done_first_at") or await mstore.mark_done_first(ymid)
                if time.time() - first < POSTBACK_WAIT_SECONDS:
                    return {"ok": True, "wait": 2}       # Monetag postback abhi aaya nahi, thoda ruko
                await mstore.record_fail(ymid, s["sid"], "unverified")
                return {"ok": False, "error": "We couldn't confirm the ad view. Please try again."}
            if not ad.get("pb_valued"):
                await mstore.record_fail(ymid, s["sid"], "non_valued")
                return {"ok": False, "error": "That ad didn't count. Please try again."}

        s2 = await db.complete_ad(s["sid"], MIN_AD_SECONDS)
        if not s2:
            return {"ok": False, "error": "Duplicate request."}
        if ymid:
            try:
                await mstore.record_completion(ymid)
            except Exception:  # noqa: BLE001
                logging.getLogger("monetag").exception("record_completion failed")
        if s2["ads_done"] < s2["ads_required"]:
            return {
                "ok": True,
                "delivered": False,
                "ads_done": s2["ads_done"],
                "ads_required": s2["ads_required"],
            }

        if not await db.claim_delivery(s["sid"]):
            return {"ok": False, "error": "You already received this file."}

        doc = await db.get_file(s2["token"])
        ok = bool(doc) and await send_file(application.bot, s2["chat_id"], doc, manager)
        if not ok:
            await db.unclaim_delivery(s["sid"])
            return {"ok": False, "error": "The file is unavailable right now. Please retry shortly."}

        # ad verify ho gaya: "Almost there! Verify & Get Video" wala message hata do
        prompt_msg = s2.get("prompt_msg")
        if prompt_msg:
            try:
                await application.bot.delete_message(s2["chat_id"], prompt_msg)
            except TelegramError:
                pass
        return {
            "ok": True,
            "delivered": True,
            "ads_done": s2["ads_done"],
            "ads_required": s2["ads_required"],
        }

    # ---------------- Monetag postback (server-to-server) ----------------
    if MONETAG_POSTBACK_SECRET:

        @app.get("/api/monetag/postback")
        async def monetag_postback(request: Request):
            q = dict(request.query_params)
            if not hmac.compare_digest(q.get("secret", ""), MONETAG_POSTBACK_SECRET):
                raise HTTPException(status_code=403)
            try:
                status = await mstore.record_postback(q)
            except Exception:  # noqa: BLE001
                logging.getLogger("monetag").exception("postback store failed")
                raise HTTPException(status_code=500)   # Monetag baad me retry karega
            return PlainTextResponse(status)

    # ---------------- admin analytics ----------------
    def _admin_ok(authorization: str | None) -> bool:
        if not MONETAG_ADMIN_TOKEN:
            return False
        got = (authorization or "").removeprefix("Bearer ").strip()
        return hmac.compare_digest(got, MONETAG_ADMIN_TOKEN)

    @app.get("/admin/monetag")
    async def admin_monetag(days: int = MONETAG_WINDOW_DAYS, authorization: str | None = Header(default=None)):
        if not MONETAG_ADMIN_TOKEN:
            raise HTTPException(status_code=404)
        if not _admin_ok(authorization):
            raise HTTPException(status_code=403)
        return await mstore.build_report(max(1, min(days, 90)))

    @app.post("/admin/monetag/confirmed")
    async def admin_monetag_confirmed(request: Request, authorization: str | None = Header(default=None)):
        """Monetag dashboard se dekha hua asli revenue yahan likho: {"day":"2026-11-03","revenue_usd":12.34}"""
        if not MONETAG_ADMIN_TOKEN:
            raise HTTPException(status_code=404)
        if not _admin_ok(authorization):
            raise HTTPException(status_code=403)
        b = await request.json()
        day, zone = str(b.get("day", "")), str(b.get("zone", "all"))
        try:
            rev = float(b.get("revenue_usd"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="revenue_usd must be a number")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) or rev < 0 or not (zone == "all" or zone in MONETAG_ZONES):
            raise HTTPException(status_code=400, detail="bad day/zone/revenue")
        await mstore.set_confirmed(day, rev, zone)
        return {"ok": True}
