"""Test scaffolding for the web admin panel.

The sandbox these tests were developed in has no network, so telegram / telethon / motor / pymongo / fastapi / httpx
may be missing. `install()` puts permissive stand-ins into sys.modules ONLY for packages that fail to import, plus
small in-memory fakes for the project's own heavy modules (db, userbot, autoposter, ads, monetag_store, bulk).
Everything in webadmin/ (auth, sessions, CSRF, validation, routing, error shape, jobs) and services/ runs for real."""
import asyncio
import base64
import os
import re
import sys
import time
import types
from datetime import datetime, timezone

ENV = {
    "MAIN_BOT_TOKEN": "111111:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "OWNER_ID": "42", "MONGO_URI": "mongodb://secret-user:secret-pw@h/x",
    "FERNET_KEY": base64.urlsafe_b64encode(b"k" * 32).decode(), "WEBHOOK_SECRET": "whsecret-value-123", "BASE_URL": "https://bot.example.com",
    "PRIMARY_CHANNEL": "-1001", "BACKUP_CHANNEL": "-1002", "LINK_BASE_URL": "https://bot.example.com",
    "POST_CHANNEL": "-1003", "BULK_POST_DELAY": "0", "BULK_SCAN_DELAY": "0", "ADMIN_PASSWORD": "legacy-admin-pw",
}
PANEL_PW = "correct-horse-battery-staple"
TOTP_SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"


class _Perm(types.ModuleType):
    """Module whose unknown attributes are throw-away classes (names ending in Error/Exception become exceptions)."""

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        base = (Exception,) if name.endswith(("Error", "Exception")) or name in ("Forbidden", "BadRequest", "RetryAfter", "TimedOut", "Conflict", "NetworkError") else ()
        cls = type(name, base, {"__init__": (lambda self, *a, **k: None) if not base else Exception.__init__,
                                "__getattr__": lambda self, n: _Inst() if not n.startswith("__") else (_ for _ in ()).throw(AttributeError(n))})
        setattr(self, name, cls)
        return cls


class _Inst:
    def __getattr__(self, n):
        if n.startswith("__"):
            raise AttributeError(n)
        return _Inst()

    def __call__(self, *a, **k):
        return _Inst()

    def __getitem__(self, k):
        return _Inst()


def _perm(name):
    m = _Perm(name)
    m.__path__ = []
    sys.modules[name] = m
    return m


def _missing(name):
    try:
        __import__(name)
        return False
    except ImportError:
        return True


# ----------------------------------------------------------------------------- in-memory collection
def _match(doc, flt):
    for k, cond in (flt or {}).items():
        if k == "$or":
            if not any(_match(doc, c) for c in cond):
                return False
            continue
        v = doc.get(k)
        if isinstance(cond, dict):
            for op, arg in cond.items():
                if op == "$regex":
                    flags = re.I if "i" in cond.get("$options", "") else 0
                    if not re.search(arg, str(v or ""), flags):
                        return False
                elif op == "$ne":
                    if v == arg:
                        return False
                elif op == "$options":
                    continue
                elif op == "$lt":
                    if not (v is not None and v < arg):
                        return False
                else:
                    raise NotImplementedError(op)
        elif v != cond:
            return False
    return True


class Cursor:
    def __init__(self, rows):
        self.rows = list(rows)

    def sort(self, key, direction=1):
        if isinstance(key, list):
            key, direction = key[0]
        self.rows.sort(key=lambda d: (d.get(key) is None, d.get(key)), reverse=direction == -1)
        return self

    def skip(self, n):
        self.rows = self.rows[n:]
        return self

    def limit(self, n):
        self.rows = self.rows[:n] if n else self.rows
        return self

    async def to_list(self, length=None):
        return [dict(r) for r in self.rows[:length]] if length else [dict(r) for r in self.rows]


class Col:
    def __init__(self):
        self.docs = []
        self.indexes = []

    async def create_index(self, *a, **k):
        self.indexes.append((a, k))

    async def insert_one(self, doc):
        doc = dict(doc)
        if "_id" in doc and any(d.get("_id") == doc["_id"] for d in self.docs):
            raise RuntimeError("duplicate key")
        self.docs.append(doc)

    def find(self, flt=None, proj=None):
        return Cursor([d for d in self.docs if _match(d, flt)])

    async def find_one(self, flt=None, *a, **k):
        for d in self.docs:
            if _match(d, flt):
                return dict(d)
        return None

    async def count_documents(self, flt=None):
        return sum(1 for d in self.docs if _match(d, flt))

    async def update_one(self, flt, upd, upsert=False):
        for d in self.docs:
            if _match(d, flt):
                d.update(upd.get("$set", {}))
                for k, n in upd.get("$inc", {}).items():
                    d[k] = d.get(k, 0) + n
                return
        if upsert:
            self.docs.append({**flt, **upd.get("$set", {})})

    async def delete_one(self, flt):
        for i, d in enumerate(self.docs):
            if _match(d, flt):
                del self.docs[i]
                return

    async def delete_many(self, flt=None):
        self.docs = [d for d in self.docs if not _match(d, flt)]


class FakeMongo:
    """get_db() target for SessionStore / Audit: db[name] -> Col."""

    def __init__(self):
        self.cols = {}

    def __getitem__(self, name):
        return self.cols.setdefault(name, Col())


# ----------------------------------------------------------------------------- fake project modules
class FakeDB(types.ModuleType):
    def __init__(self):
        super().__init__("db")
        self.files, self.sessions, self.batches, self.bots_c, self.userbots, self.logs = Col(), Col(), Col(), Col(), Col(), Col()
        self.views = {"2026-11-06": 7, "2026-11-05": 3}
        self.progress = {}
        self._db = FakeMongo()
        self.saved_posts = []

    def reset(self):
        for c in (self.files, self.sessions, self.batches, self.bots_c, self.userbots):
            c.docs.clear()
        self.saved_posts.clear()
        self.progress.clear()
        self.deleted = []

    async def count_files(self):
        return len(self.files.docs)

    async def views_last_days(self, n):
        return [("2026-11-06", self.views["2026-11-06"]), ("2026-11-05", self.views["2026-11-05"])]

    async def views_total(self):
        return 10

    async def list_files(self, skip, size):
        return (await self.files.find({}).sort("created_at", -1).skip(skip).limit(size).to_list(size))

    async def get_file(self, token):
        return await self.files.find_one({"token": token})

    async def delete_file(self, token):
        await self.files.delete_one({"token": token})

    async def save_file(self, token, locations, caption):
        await self.files.insert_one({"token": token, "locations": locations, "caption": caption, "views": 0, "created_at": time.time()})

    async def save_post(self, token, items, caption, photo, thumb_type="photo", src_chat=None, src_head=None):
        self.saved_posts.append(token)
        await self.files.insert_one({"token": token, "kind": "post", "items": items, "post_caption": caption, "photo": photo,
                                     "thumb_type": thumb_type, "views": 0, "created_at": time.time()})

    async def add_channel_post(self, token, channel, msg_id):
        pass

    async def list_bots(self):
        return list(self.bots_c.docs)

    async def get_bot(self, bot_id):
        return await self.bots_c.find_one({"bot_id": bot_id})

    async def set_status(self, bot_id, status):
        await self.bots_c.update_one({"bot_id": bot_id}, {"$set": {"status": status}})

    async def delete_bot(self, bot_id):
        await self.bots_c.delete_one({"bot_id": bot_id})

    async def list_bulk_tokens(self):
        return [d["token"] for d in self.files.docs if d.get("src_chat")]

    async def get_batch(self, bid):
        return await self.batches.find_one({"id": bid})

    async def list_userbots(self):
        return list(self.userbots.docs)

    async def get_userbot(self, uid):
        return await self.userbots.find_one({"uid": uid})

    async def get_ub_progress(self, acc, src, dest):
        return self.progress.get((acc, src, dest), 0)

    async def find_post_by_source(self, *a):
        return None

    async def save_batch(self, bid, tokens):
        await self.batches.insert_one({"id": bid, "tokens": tokens, "posted": [], "created_at": time.time()})

    async def mark_batch_posted(self, bid, token):
        pass


class FakeManager:
    def __init__(self, db):
        self.db = db
        self.apps = {"b1": object()}
        self.main = types.SimpleNamespace(bot=FakeBot())
        self.checked = 0

    async def add_bot(self, token):
        return "❌ Invalid token" if token.endswith("BAD") else "✅ Bot added (pending)"

    async def release_bot(self, b):
        pass

    async def refresh(self):
        pass

    async def check_all(self):
        self.checked += 1

    async def check_admin(self, bot):
        return True, []

    async def active_usernames(self):
        return []


class FakeMsg:
    def __init__(self, mid=1, kind="video"):
        self.message_id = mid
        fid = types.SimpleNamespace(file_id="FID" + str(mid), file_unique_id="U" + str(mid), file_name="f.mp4")
        self.photo = [fid] if kind == "photo" else []
        self.video = fid if kind == "video" else None
        self.animation = fid if kind == "animation" else None
        self.document = fid if kind == "document" else None
        self.audio = None


class FakeBot:
    def __init__(self):
        self.sent, self.deleted, self.n = [], [], 100

    def _m(self, kind):
        self.n += 1
        return FakeMsg(self.n, kind)

    async def send_photo(self, chat, data, **k):
        self.sent.append(("photo", chat)); return self._m("photo")

    async def send_video(self, chat, data, **k):
        self.sent.append(("video", chat)); return self._m("video")

    async def send_animation(self, chat, data, **k):
        self.sent.append(("animation", chat)); return self._m("animation")

    async def send_document(self, chat, data, **k):
        self.sent.append(("document", chat)); return self._m("document")

    async def send_message(self, chat, text, **k):
        self.sent.append(("message", chat)); return self._m("video")

    async def copy_message(self, chat_id=None, **k):
        self.sent.append(("copy", chat_id)); return self._m("video")

    async def delete_message(self, chat, mid):
        self.deleted.append((chat, mid))

    async def get_me(self):
        return types.SimpleNamespace(username="main_bot")

    async def get_webhook_info(self):
        return types.SimpleNamespace(url="", pending_update_count=0, last_error_message=None)


_SNAP: dict = {}
_PREFIX = ("webadmin", "services", "autoposter", "telegram", "telethon", "motor", "pymongo", "bson")
_EXACT = ("db", "bulk", "userbot", "monetag_store", "config", "webadmin_support_fakes")


def begin():
    """Remember sys.modules so end() can remove every fake / project module this test module pulled in."""
    _SNAP["modules"] = dict(sys.modules)
    _SNAP["env"] = dict(os.environ)


def end():
    before = _SNAP.get("modules", {})
    for k in list(sys.modules):
        if (k in _EXACT or k.split(".")[0] in _PREFIX) and k not in before:
            del sys.modules[k]
    for k in _EXACT:
        if k in before:
            sys.modules[k] = before[k]
    if "ads" in _SNAP:
        from ads import routes as ads_routes
        from ads import settings as ads_settings

        for k, v in _SNAP.pop("ads").items():
            setattr(ads_routes, k, v)
        ads_settings.SMART_ADS_ENABLED = _SNAP.pop("smart")
    for k in list(os.environ):
        if k not in _SNAP.get("env", {}):
            del os.environ[k]


def install():
    """Returns the shared fake DB."""
    for k, v in ENV.items():
        os.environ.setdefault(k, v)
    if "webadmin_support_fakes" in sys.modules:
        return sys.modules["db"]
    if _missing("telegram"):
        for n in ("telegram", "telegram.error", "telegram.ext", "telegram.constants", "telegram.helpers"):
            _perm(n)
    if _missing("telethon"):
        for n in ("telethon", "telethon.errors", "telethon.types", "telethon.utils", "telethon.sessions", "telethon.tl", "telethon.tl.types", "telethon.tl.functions"):
            _perm(n)
    if _missing("motor"):
        _perm("motor"); _perm("motor.motor_asyncio")
    if _missing("pymongo"):
        _perm("pymongo"); _perm("pymongo.errors")
    if _missing("bson"):
        _perm("bson"); _perm("bson.json_util")
    db = FakeDB()
    sys.modules["db"] = db

    # --- bulk (channel scanner) ---
    bulk = types.ModuleType("bulk")
    rx = re.compile(r"https://t\.me/c/(\d+)/(\d+)")

    def parse_link(text):
        m = rx.search(text or "")
        return (int("-100" + m.group(1)), int(m.group(2))) if m else None

    async def probe(bot, chat, mid, scratch):
        return (False, "bot is not admin there") if mid == 999 else (True, "")

    async def with_retry(fn, *a, **k):
        return await fn()

    class ScanResult:
        def __init__(self):
            self.groups, self.aborted, self.stopped, self.orphans, self.missing, self.errors, self.last_error = [], "", False, 0, 0, 0, ""

    async def scan(bot, chat, start, end, scratch, job, on_scan, delay):
        await on_scan(end - start + 1, end - start + 1, 0)
        r = ScanResult()
        r.stopped = bool(job["stop"])
        return r

    bulk.parse_link, bulk.probe, bulk.with_retry, bulk.scan, bulk.ScanResult = parse_link, probe, with_retry, scan, ScanResult
    sys.modules["bulk"] = bulk

    # --- userbot ---
    ub = types.ModuleType("userbot")

    class UbError(Exception):
        pass

    state = {"pending": False, "ready": True}
    ub.UbError, ub._state = UbError, state

    async def login_send_code(phone):
        if phone.endswith("0000"):
            raise UbError("Invalid phone number")
        state["pending"] = True

    async def login_code(code):
        if code == "000000":
            raise UbError("Wrong code")
        return "2fa" if code == "222222" else "ok"

    async def login_password(pwd):
        if pwd == "wrong":
            raise UbError("Wrong password")

    async def login_finish():
        state["pending"] = False
        await db.userbots.insert_one({"uid": 777, "name": "Test Acc", "phone": "+919876543210", "session_enc": "SESSION-ENC-SECRET", "added_at": time.time()})
        return {"uid": 777, "name": "Test Acc"}

    async def login_cancel():
        state["pending"] = False

    async def get_client(uid):
        return object() if state["ready"] else None

    async def remove_account(uid):
        await db.userbots.delete_one({"uid": uid})

    async def list_dialogs(c, limit=400):
        return [{"id": 1, "title": "Saved Messages", "kind": "saved", "post": False, "dest": True},
                {"id": -100500, "title": "News Channel", "kind": "channel", "post": True, "dest": True}]

    async def resolve(c, text):
        return {"id": -100777, "title": "Resolved", "kind": "channel", "post": True, "dest": True} if "found" in text else None

    async def copy_chat(client, src, dest, kinds, cap_only, min_id, job, progress=None, acc=0):
        for i in range(1, 400):
            if job["stop"]:
                return {"scanned": i, "copied": i - 1, "skipped": 0, "errors": 0, "total": 400, "aborted": "", "stopped": True, "last_error": "", "last_id": i}
            if progress:
                await progress({"scanned": i, "copied": i, "skipped": 0, "errors": 0, "total": 400, "last_id": i})
            await asyncio.sleep(0.01)
        return {"scanned": 399, "copied": 399, "skipped": 0, "errors": 0, "total": 400, "aborted": "", "stopped": False, "last_error": "", "last_id": 399}

    for f in (login_send_code, login_code, login_password, login_finish, login_cancel, get_client, remove_account, list_dialogs, resolve, copy_chat):
        setattr(ub, f.__name__, f)
    ub.has_pending = lambda: state["pending"]
    sys.modules["userbot"] = ub

    # --- monetag ---
    ms = types.ModuleType("monetag_store")
    ms.confirmed = []

    async def build_report(days=7):
        return {"days": days, "zones": {"z1": {"impressions": 5}}}

    async def set_confirmed(day, rev, zone="all"):
        ms.confirmed.append((day, rev, zone))

    ms.build_report, ms.set_confirmed = build_report, set_confirmed
    sys.modules["monetag_store"] = ms

    # --- ads: real ads.settings / ads.routes, only the admin handlers are swapped for in-memory ones (restored by end()) ---
    from ads import routes as ads_routes
    from ads import settings as ads_settings

    _SNAP["ads"] = {k: getattr(ads_routes, k) for k in ("h_admin_overview", "h_admin_config", "h_admin_patch_config", "h_admin_revenue", "h_admin_rewards")}
    _SNAP["smart"] = ads_settings.SMART_ADS_ENABLED
    ads_settings.SMART_ADS_ENABLED = True
    cfg = {"mode": "auto"}

    async def h_admin_overview(days):
        return {"days": days, "impressions": 3}

    async def h_admin_config():
        return {"config": dict(cfg), "secrets_set": {"adsgram_reward_secret": True}, "env_ids": {"adsgram_block_id": False}}

    async def h_admin_patch_config(body):
        if body.get("mode") not in (None, "auto", "manual"):
            return 400, {"ok": False, "errors": ["mode must be auto or manual"]}
        cfg.update(body)
        return 200, {"ok": True, "config": dict(cfg)}

    async def h_admin_revenue(body):
        return (400, {"ok": False, "errors": ["day cannot be in the future"]}) if body["day"] > "2026-11-06" else (200, {"ok": True})

    async def h_admin_rewards(limit):
        return {"rewards": [{"user": 1}]}

    ads_routes.h_admin_overview, ads_routes.h_admin_config, ads_routes.h_admin_patch_config = h_admin_overview, h_admin_config, h_admin_patch_config
    ads_routes.h_admin_revenue, ads_routes.h_admin_rewards = h_admin_revenue, h_admin_rewards

    db.apdb = _install_autoposter(db)
    sys.modules["webadmin_support_fakes"] = types.ModuleType("webadmin_support_fakes")
    return db


class FakeApDB:
    def __init__(self):
        self.chat = {"chat_id": -100900, "title": "My Channel", "type": "channel", "username": "mych", "is_active": True,
                     "schedule": {"interval_minutes": 180, "batch_size": 5, "start_time": "09:00", "end_time": "21:00", "days": [0, 1, 2, 3, 4, 5, 6], "enabled": True},
                     "options": {}, "stats": {"total_cycles_completed": 2}, "next_post_time": datetime(2026, 11, 7, tzinfo=timezone.utc)}
        self.posts = [{"_id": "p1", "media_type": "text", "text": "hello", "created_at": datetime(2026, 11, 1, tzinfo=timezone.utc)}]
        self.logs = Col()
        self.calls = []
        self.wiped = False

    async def list_chats(self):
        return [self.chat] if self.chat else []

    async def get_chat(self, cid):
        return self.chat if self.chat and self.chat["chat_id"] == cid else None

    async def global_stats(self):
        return {"total_posts": 1, "remaining_posts": 1, "published_today": 0, "total_cycles_completed": 2, "total_channels": 1, "total_groups": 0, "paused": 0, "next_post_time": None}

    async def ping(self):
        return 3.2

    async def queue_snapshot(self, cid):
        return {"total": 1, "used": 0, "remaining": 1, "cycle": 0, "current_cursor": 0, "next_batch_ids": [], "queue_size": 1, "shuffle_status": "idle", "last_posted_at": None, "next_post_time": None}

    async def history_by_day(self, cid, days=7):
        return [("2026-11-06", 2)]

    async def reaction_summary(self, cid, top=3):
        return [0, 0], []

    async def published_today(self, cid):
        return 2

    async def count_posts(self, cid):
        return len(self.posts)

    async def update_schedule(self, cid, **f):
        self.chat["schedule"].update(f); self.calls.append(("schedule", f)); return self.chat

    async def set_options(self, cid, **f):
        self.chat["options"].update(f); self.calls.append(("options", f)); return self.chat

    async def set_active(self, cid, active, reason=None, error=None):
        self.chat["is_active"] = active; return self.chat

    async def page_posts(self, cid, page, size=6):
        return self.posts, 1

    async def get_post(self, cid, pid):
        return next((p for p in self.posts if p["_id"] == pid), None)

    async def delete_post(self, cid, pid):
        n = len(self.posts); self.posts = [p for p in self.posts if p["_id"] != pid]; return len(self.posts) < n

    async def undo_last_post(self, cid):
        return self.posts.pop() if self.posts else None

    async def add_post(self, cid, payload, dedupe=True):
        if any(p.get("fingerprint") == payload.get("fingerprint") for p in self.posts) and dedupe:
            return None
        self.posts.append({"_id": f"p{len(self.posts) + 1}", **payload}); return self.posts[-1]["_id"]

    async def bulk_add_posts(self, cid, posts, dedupe=True):
        for p in posts:
            self.posts.append({"_id": f"b{len(self.posts)}", **p})
        return len(posts), 0

    async def backup_payload(self):
        return {"version": 2, "chats": [], "posts": {}}

    async def restore_payload(self, payload):
        return 1, 2

    async def wipe_all(self):
        self.wiped = True


def _install_autoposter(db):
    apdb = FakeApDB()
    pkg = types.ModuleType("autoposter")
    pkg.__path__ = []
    sys.modules["autoposter"] = pkg
    mods = {}
    for n in ("actions", "config", "jobs", "payloads", "sender", "state", "utils", "ui"):
        mods[n] = types.ModuleType("autoposter." + n)
        sys.modules["autoposter." + n] = mods[n]
        setattr(pkg, n, mods[n])

    class ActionError(Exception):
        pass

    A = mods["actions"]
    A.ActionError = ActionError
    A.parse_chat_ref = lambda raw: raw.strip()
    A.pw = {"required": True}
    A.password_required = lambda: A.pw["required"]
    A.password_ok = lambda e: e == "legacy-admin-pw"

    async def add_target(bot, ref):
        if ref == "bad":
            raise ActionError("❌ Chat access nahi mil raha")
        return apdb.chat

    async def wipe_all():
        await apdb.wipe_all()

    async def wipe_chat(cid):
        apdb.calls.append(("wipe_chat", cid)); return True

    async def remove_chat(cid):
        apdb.calls.append(("remove_chat", cid)); apdb.chat = None; return "My Channel"

    async def restore_backup(text):
        if text == "garbage":
            raise ValueError("This doesn't look like a backup")
        return 1, 2

    async def set_force_sub(bot, cid, raw):
        return None if raw == "off" else raw

    async def publish_promo(bot, chat, send):
        await send(); return 5

    async def check_import_source(bot, link):
        if "bad" in link:
            raise ActionError("Link samajh nahi aaya")
        return types.SimpleNamespace(id=-100111), 10

    async def import_range(bot, target, source, first, last):
        return types.SimpleNamespace(inserted=3, dups=1, vault="no_vault", target=apdb.chat)

    for f in (add_target, wipe_all, wipe_chat, remove_chat, restore_backup, set_force_sub, publish_promo, check_import_source, import_range):
        setattr(A, f.__name__, f)

    mods["config"].SETTINGS = types.SimpleNamespace(timezone="Asia/Kolkata", scan_interval_seconds=30, retry_attempts=3, retry_base_seconds=2.0, post_gap_seconds=1.0,
                                                    default_interval_minutes=180, default_batch_size=5, default_start_time="09:00", default_end_time="21:00",
                                                    log_retention_days=30, vault_chat_id=None, admin_password="legacy-admin-pw")
    mods["jobs"].build_backup_file = lambda payload: (b'{"version":2}', "backup-test.json")
    P = mods["payloads"]

    class UnsupportedMessage(Exception):
        pass

    P.UnsupportedMessage = UnsupportedMessage

    def text_post_payload(raw):
        if not raw.strip():
            raise UnsupportedMessage("Text is empty.")
        return {"media_type": "text", "text": raw, "fingerprint": "t:" + raw}

    P.text_post_payload = text_post_payload
    P.album_post_payload = lambda items, caption, flags: {"media_type": "album", "media_group": items, "caption": caption, "fingerprint": "a:" + str(len(items))}
    P.media_fingerprint = lambda t, u: f"m:{t}:{u}" if u else None

    def parse_bulk_text(name, text):
        if name.endswith(".json") and not text.startswith("["):
            raise ValueError("Invalid JSON")
        return [{"media_type": "text", "text": c} for c in text.split("\n---\n") if c.strip()], 0

    P.parse_bulk_text = parse_bulk_text
    S = mods["sender"]

    async def _noop(*a, **k):
        return None

    async def process_chat_batch(bot, cid, manual=False):
        await asyncio.sleep(0.01); return 3

    S.after_posts_added, S.rebuild_and_ensure_schedule, S.send_saved_post, S.send_single_now = _noop, _noop, _noop, _noop
    S.process_chat_batch = process_chat_batch

    class _State:
        db = apdb
        uptime_seconds = 120
        _locks = {}

        def lock_for(self, cid):
            return self._locks.setdefault(cid, asyncio.Lock())

    mods["state"].STATE = _State()
    U = mods["utils"]
    U.MAX_FOOTER_LEN = 400
    U.chat_opts = lambda chat: {"loop": True, "dedupe": True, "notify_cycle": False, "auto_delete_minutes": 0, "footer": None, "jitter_minutes": 0, **((chat or {}).get("options") or {})}
    U.visible_len = lambda s: len(re.sub(r"<[^>]+>", "", s or ""))
    U.parse_post_markup = lambda raw: ((raw or "").strip(), {"silent": False, "protect_content": False, "spoiler": False, "pin": False, "nofooter": False, "buttons": []})
    mods["ui"].post_label = lambda p: "📝 " + (p.get("text") or p.get("media_type", ""))[:28]
    return apdb
