import asyncio
import hashlib
import logging
import re
import time

from cryptography.fernet import Fernet
from telegram import Bot
from telegram.error import BadRequest, Forbidden, InvalidToken, RetryAfter, TelegramError
from telegram.ext import Application

import db
import handlers
import monetag_engine
import monetag_store
from ads import engine as ads_engine, store as ads_store, settings as ads_settings
from autoposter import panel as ap_panel
from config import (
    AUTO_DELETE_SECONDS,
    BASE_URL,
    CHANNELS,
    FERNET_KEY,
    HEALTH_INTERVAL,
    MAIN_BOT_TOKEN,
    OWNER_ID,
    ROLE,
    SYNC_INTERVAL,
    WEBHOOK_SECRET,
)

log = logging.getLogger("manager")
fernet = Fernet(FERNET_KEY.encode() if isinstance(FERNET_KEY, str) else FERNET_KEY)
TOKEN_RE = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{30,}$")


def _dec(token_enc: str) -> str:
    return fernet.decrypt(token_enc.encode()).decode()


class BotManager:
    def __init__(self):
        self.apps: dict[str, Application] = {}
        self.main: Application | None = None
        self._running_tokens: dict[str, str] = {}
        self._cache: tuple[float, list[str]] | None = None
        self._alerted: set[str] = set()
        self._lock = asyncio.Lock()

    def secret_for(self, bot_id: str) -> str:
        return hashlib.sha256(f"{WEBHOOK_SECRET}:{bot_id}".encode()).hexdigest()

    async def _build(self, token: str, register, bot_id: str) -> Application:
        app = (
            Application.builder()
            .token(token)
            .updater(None)
            .concurrent_updates(True)
            .build()
        )
        app.bot_data["manager"] = self
        app.bot_data["bot_id"] = bot_id
        register(app)
        await app.initialize()
        await app.start()
        try:  # koi slash-command menu nahi: sab kuch buttons me
            await app.bot.delete_my_commands()
        except Exception:  # noqa: BLE001
            pass
        return app

    async def notify_owner(self, text: str):
        try:
            if self.main:
                await self.main.bot.send_message(OWNER_ID, text)
            else:
                async with Bot(MAIN_BOT_TOKEN) as b:
                    await b.send_message(OWNER_ID, text)
        except Exception as e:  # noqa: BLE001
            log.warning("owner notify failed: %s", e)

    async def alert_once(self, key: str, text: str):
        if key in self._alerted:
            return
        self._alerted.add(key)
        await self.notify_owner(text)

    async def check_admin(self, bot: Bot):
        missing = []
        for ch in CHANNELS:
            try:
                m = await bot.get_chat_member(ch, bot.id)
                if m.status not in ("administrator", "creator"):
                    missing.append(ch)
            except TelegramError:
                missing.append(ch)
        return (not missing), missing

    # ---------- lifecycle ----------
    async def start(self):
        await db.init()
        try:
            await monetag_store.init()
        except Exception:  # noqa: BLE001 — analytics indexes fail ho to bhi bot chalna chahiye
            log.exception("monetag index init failed")
        if ads_settings.SMART_ADS_ENABLED:
            try:
                await ads_store.init()      # idempotent migration (new collections only)
            except Exception:  # noqa: BLE001
                log.exception("smart-ads migration failed")
        if ROLE in ("main", "both"):
            # Auto Posting (alag Mongo database). Fail ho to bhi HelpBot chalta rahe.
            if not await ap_panel.start():
                log.error("Auto Posting disabled (MongoDB init failed)")
            self.main = await self._build(MAIN_BOT_TOKEN, handlers.register_main_handlers, "main")
            await self.main.bot.set_webhook(
                url=f"{BASE_URL}/webhook/main",
                secret_token=self.secret_for("main"),
                # my_chat_member / chat_join_request = Auto Posting (channel add/remove, force-sub)
                allowed_updates=["message", "callback_query", *ap_panel.EXTRA_ALLOWED_UPDATES],
            )
            self.apps["main"] = self.main
        if ROLE in ("worker", "both"):
            await self.sync_once()

    def background_loop(self):
        if ROLE == "main":
            return self._main_loops()
        if ROLE == "worker":
            return self._worker_loops()
        return self._both_loops()

    async def _autoposter_loops(self):
        if self.main is not None:
            await ap_panel.run_loops(self.main.bot)

    async def _main_loops(self):
        await asyncio.gather(self.health_loop(), self._autoposter_loops())

    async def _worker_loops(self):
        await asyncio.gather(self.sync_loop(), self.cleanup_loop(), monetag_engine.optimizer_loop(), ads_engine.loop())

    async def _both_loops(self):
        await asyncio.gather(
            self.health_loop(), self.sync_loop(), self.cleanup_loop(), monetag_engine.optimizer_loop(), ads_engine.loop(), self._autoposter_loops()
        )

    # ---------- auto-delete delivered files ----------
    async def cleanup_loop(self):
        if AUTO_DELETE_SECONDS <= 0:
            return
        while True:
            try:
                await self.cleanup_once()
            except Exception:  # noqa: BLE001
                log.exception("cleanup error")
            await asyncio.sleep(10)

    async def cleanup_once(self):
        now = time.time()
        for d in await db.due_deletes(now, 100):
            app = next((a for a in list(self.apps.values()) if a.bot.id == d["bot_id"]), None)
            overdue = now - d["delete_at"] > 3600
            if not app:
                if overdue:
                    await db.remove_delete(d["_id"])
                continue
            try:
                await app.bot.delete_message(d["chat_id"], d["message_id"])
            except RetryAfter:
                return
            except (BadRequest, Forbidden):
                pass  # pehle hi delete ho chuka / ab delete nahi ho sakta
            except TelegramError as e:
                log.warning("auto-delete failed: %s", e)
                if not overdue:
                    continue
            await db.remove_delete(d["_id"])

    async def refresh(self):
        """Single-service mode: bot add/enable/disable ke turant baad naye bots live karo."""
        if ROLE == "both":
            try:
                await self.sync_once()
            except Exception:  # noqa: BLE001
                log.exception("refresh error")

    async def stop(self):
        for app in list(self.apps.values()):
            try:
                await app.stop()
                await app.shutdown()
            except Exception:  # noqa: BLE001
                pass
        self.apps.clear()
        await ap_panel.shutdown()

    # ---------- WORKER: secondary bots ----------
    async def launch_secondary(self, b: dict):
        bot_id = b["bot_id"]
        app = await self._build(_dec(b["token_enc"]), handlers.register_delivery_handlers, bot_id)
        await app.bot.set_webhook(
            url=f"{BASE_URL}/webhook/{bot_id}",
            secret_token=self.secret_for(bot_id),
            allowed_updates=["message", "callback_query"],
        )
        self.apps[bot_id] = app
        self._running_tokens[bot_id] = b["token_enc"]
        self._cache = None

    async def stop_secondary(self, bot_id: str, delete_webhook: bool = False):
        app = self.apps.pop(bot_id, None)
        self._running_tokens.pop(bot_id, None)
        self._cache = None
        if not app:
            return
        if delete_webhook:
            try:
                await app.bot.delete_webhook()
            except TelegramError:
                pass
        try:
            await app.stop()
            await app.shutdown()
        except Exception:  # noqa: BLE001
            pass

    async def _safe_launch(self, b: dict):
        try:
            await self.launch_secondary(b)
            log.info("launched @%s", b["username"])
        except InvalidToken:
            await self._mark_dead(b)
        except Exception as e:  # noqa: BLE001
            log.warning("launch failed @%s: %s", b.get("username"), e)

    async def sync_once(self):
        want = {
            b["bot_id"]: b
            for b in await db.list_bots()
            if b["status"] in ("active", "pending")
        }
        for bot_id in list(self.apps):
            if bot_id == "main":
                continue
            b = want.get(bot_id)
            if b is None:
                await self.stop_secondary(bot_id, delete_webhook=True)
            elif self._running_tokens.get(bot_id) != b["token_enc"]:
                await self.stop_secondary(bot_id)
        for bot_id, b in want.items():
            if bot_id not in self.apps:
                await self._safe_launch(b)

    async def sync_loop(self):
        while True:
            try:
                await self.sync_once()
            except Exception:  # noqa: BLE001
                log.exception("sync error")
            await asyncio.sleep(SYNC_INTERVAL)

    # ---------- MAIN: registry + health ----------
    async def _mark_dead(self, b: dict):
        await db.set_status(b["bot_id"], "dead")
        await self.stop_secondary(b["bot_id"])
        left = len(await db.list_bots("active"))
        await self.notify_owner(
            f"🔴💀 @{b['username']} is dead (suspended / deleted / token revoked).\n"
            f"Active bots left: {left}\n"
            "Naya bot add karo: Control Center → ➕ Add Bot"
        )

    async def release_bot(self, b: dict):
        try:
            async with Bot(_dec(b["token_enc"])) as tmp:
                await tmp.delete_webhook()
        except Exception:  # noqa: BLE001
            pass

    async def add_bot(self, token: str) -> str:
        if not TOKEN_RE.match(token):
            return "❌ That token format looks wrong."
        try:
            async with Bot(token) as tmp:
                me = await tmp.get_me()
                ok, missing = await self.check_admin(tmp)
        except InvalidToken:
            return "❌ Token is invalid or revoked."
        except TelegramError as e:
            return f"❌ Telegram error: {e}"

        bot_id = str(me.id)
        status = "active" if ok else "pending"
        async with self._lock:
            await db.upsert_bot(bot_id, me.username, fernet.encrypt(token.encode()).decode(), status)

        await self.refresh()
        if ok:
            return f"✅🎉 @{me.username} saved & live!" if ROLE == "both" else (
                f"✅🎉 @{me.username} saved!\n"
                f"The worker will take it live within {SYNC_INTERVAL}s."
            )
        return (
            f"🟡⏳ @{me.username} saved (pending).\n"
            f"Make it admin in these channels: {', '.join(map(str, missing))}\n"
            f"It activates automatically within {HEALTH_INTERVAL // 60} min after that."
        )

    async def check_all(self):
        for b in await db.list_bots():
            if b["status"] in ("dead", "disabled"):
                continue
            try:
                async with Bot(_dec(b["token_enc"])) as bot:
                    await bot.get_me()
                    if b["status"] == "pending":
                        ok, _ = await self.check_admin(bot)
                        if ok:
                            await db.set_status(b["bot_id"], "active")
                            await self.notify_owner(f"🟢🎉 @{b['username']} is now active!")
            except InvalidToken:
                await self._mark_dead(b)
            except Exception as e:  # noqa: BLE001
                log.warning("health check skipped @%s: %s", b["username"], e)

    async def health_loop(self):
        while True:
            await asyncio.sleep(HEALTH_INTERVAL)
            try:
                await self.check_all()
            except Exception:  # noqa: BLE001
                log.exception("health loop error")

    async def active_usernames(self) -> list[str]:
        if self._cache and time.time() - self._cache[0] < 30:
            return self._cache[1]
        names = [b["username"] for b in await db.list_bots("active")]
        if self.main and self.main.bot.username:  # main bot khud bhi delivery karta hai
            names.append(self.main.bot.username)
        self._cache = (time.time(), names)
        return names
