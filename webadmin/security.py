"""Authentication primitives for the admin panel. Pure Python, no web framework, no Telegram."""
import base64
import hashlib
import hmac
import secrets
import struct
import time
from collections import deque
from datetime import datetime, timedelta, timezone

SESSION_COLLECTION = "admin_sessions"
AUDIT_COLLECTION = "admin_audit"


# ---------------------------------------------------------------- comparisons
def secure_eq(a: str, b: str) -> bool:
    """Constant-time string comparison that also hides the length (compares fixed-size digests)."""
    da = hashlib.sha256((a or "").encode()).digest()
    db = hashlib.sha256((b or "").encode()).digest()
    return hmac.compare_digest(da, db)


# ---------------------------------------------------------------- TOTP (RFC 6238, SHA-1, 6 digits, 30 s)
def _key(secret: str) -> bytes:
    s = secret.strip().replace(" ", "").upper()
    return base64.b32decode(s + "=" * (-len(s) % 8), casefold=True)


def totp_at(secret: str, counter: int, digits: int = 6) -> str:
    mac = hmac.new(_key(secret), struct.pack(">Q", counter), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def totp_verify(secret: str, code: str, now: float | None = None, window: int = 1, last_counter: int = -1):
    """-> matched counter (int) or None. A counter <= last_counter is rejected (one-time use)."""
    code = (code or "").strip().replace(" ", "")
    if not (code.isdigit() and len(code) == 6):
        return None
    base = int((time.time() if now is None else now) // 30)
    matched = None
    for c in range(base - window, base + window + 1):       # no early exit: constant work
        if hmac.compare_digest(totp_at(secret, c), code) and c > last_counter:
            matched = c if matched is None else matched
    return matched


# ---------------------------------------------------------------- login lockout / rate limit
class LoginGuard:
    """Per-IP failed-login tracking. After `max_fails` failures inside `window` the IP is locked for `lockout` seconds."""

    def __init__(self, max_fails: int = 5, lockout: int = 900, window: int = 900, clock=time.monotonic):
        self.max_fails, self.lockout, self.window, self.clock = max_fails, lockout, window, clock
        self._fails: dict = {}
        self._locked: dict = {}

    def locked_for(self, key: str) -> int:
        until = self._locked.get(key)
        if until is None:
            return 0
        left = until - self.clock()
        if left <= 0:
            self._locked.pop(key, None)
            self._fails.pop(key, None)
            return 0
        return int(left) + 1

    def fail(self, key: str) -> int:
        """Record a failure; returns lockout seconds if this failure triggered/extended a lock, else 0."""
        now = self.clock()
        q = self._fails.setdefault(key, deque())
        while q and now - q[0] > self.window:
            q.popleft()
        q.append(now)
        if len(self._fails) > 5000:
            for k in [k for k, v in self._fails.items() if not v or now - v[-1] > self.window][:1000]:
                self._fails.pop(k, None)
        if len(q) >= self.max_fails:
            self._locked[key] = now + self.lockout
            return self.lockout
        return 0

    def success(self, key: str) -> None:
        self._fails.pop(key, None)
        self._locked.pop(key, None)


class RateLimiter:
    """Sliding window limiter (per process)."""

    def __init__(self, max_events: int, window: float, clock=time.monotonic):
        self.max, self.window, self.clock = max_events, window, clock
        self._hits: dict = {}

    def allow(self, key) -> bool:
        now = self.clock()
        q = self._hits.setdefault(key, deque())
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.max:
            return False
        q.append(now)
        if len(self._hits) > 5000:
            for k in [k for k, v in self._hits.items() if not v or now - v[-1] > self.window][:1000]:
                self._hits.pop(k, None)
        return True


# ---------------------------------------------------------------- sessions
def _sign(key: bytes, sid: str) -> str:
    return base64.urlsafe_b64encode(hmac.new(key, sid.encode(), hashlib.sha256).digest()[:18]).decode().rstrip("=")


def _hash_sid(sid: str) -> str:
    return hashlib.sha256(sid.encode()).hexdigest()


class SessionStore:
    """Random 256-bit session id, HMAC-signed in the cookie, only its SHA-256 stored in MongoDB (TTL index on `expires`)."""

    def __init__(self, get_db, signing_secret: str, ttl_seconds: int, now=lambda: datetime.now(timezone.utc)):
        self._get_db = get_db
        self._key = hashlib.sha256(b"hb-admin-session:" + signing_secret.encode()).digest()
        self.ttl = ttl_seconds
        self.now = now

    def _col(self):
        return self._get_db()[SESSION_COLLECTION]

    async def init(self) -> None:
        await self._col().create_index("expires", expireAfterSeconds=0)

    async def create(self, ip: str, ua: str):
        """-> (cookie_value, csrf_token, expires_datetime)"""
        sid = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        now = self.now()
        expires = now + timedelta(seconds=self.ttl)
        await self._col().insert_one(
            {"_id": _hash_sid(sid), "csrf": csrf, "created": now, "expires": expires, "last_seen": now,
             "ip": (ip or "")[:64], "ua": (ua or "")[:160]}
        )
        return f"{sid}.{_sign(self._key, sid)}", csrf, expires

    def _unsign(self, cookie: str | None):
        if not cookie or "." not in cookie or len(cookie) > 200:
            return None
        sid, _, sig = cookie.partition(".")
        if not hmac.compare_digest(sig, _sign(self._key, sid)):
            return None
        return sid

    async def get(self, cookie: str | None):
        sid = self._unsign(cookie)
        if not sid:
            return None
        doc = await self._col().find_one({"_id": _hash_sid(sid)})
        if not doc:
            return None
        exp = doc["expires"]
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp <= self.now():
            await self._col().delete_one({"_id": doc["_id"]})
            return None
        return doc

    async def destroy(self, cookie: str | None) -> None:
        sid = self._unsign(cookie)
        if sid:
            await self._col().delete_one({"_id": _hash_sid(sid)})

    async def destroy_all(self) -> None:
        await self._col().delete_many({})


# ---------------------------------------------------------------- audit log
_DENY_KEYS = ("token", "password", "secret", "session", "code", "hash", "key", "csrf", "cookie")


def sanitize(detail: dict) -> dict:
    """Keep audit details small, flat and secret-free."""
    out = {}
    for k, v in (detail or {}).items():
        if any(bad in str(k).lower() for bad in _DENY_KEYS) and k not in ("file_token",):
            continue
        if isinstance(v, (int, float, bool)) or v is None:
            out[str(k)] = v
        elif isinstance(v, str):
            out[str(k)] = v[:200]
        elif isinstance(v, (list, tuple)):
            out[str(k)] = [x if isinstance(x, (int, float, bool)) else str(x)[:80] for x in list(v)[:20]]
        else:
            out[str(k)] = str(v)[:200]
    return out


class Audit:
    def __init__(self, get_db, now=lambda: datetime.now(timezone.utc)):
        self._get_db = get_db
        self.now = now

    def _col(self):
        return self._get_db()[AUDIT_COLLECTION]

    async def init(self) -> None:
        await self._col().create_index("ts")
        await self._col().create_index("expire_at", expireAfterSeconds=0)

    async def log(self, action: str, ip: str = "", ok: bool = True, **detail) -> None:
        try:
            now = self.now()
            await self._col().insert_one(
                {"ts": now, "expire_at": now + timedelta(days=90), "action": action, "ok": bool(ok),
                 "ip": (ip or "")[:64], "detail": sanitize(detail)}
            )
        except Exception:  # noqa: BLE001 - auditing must never break the request
            import logging

            logging.getLogger("webadmin").exception("audit write failed")

    async def recent(self, limit: int = 50, skip: int = 0) -> list:
        rows = await self._col().find({}).sort("ts", -1).skip(skip).limit(limit).to_list(length=limit)
        return [{"ts": r["ts"].isoformat() if hasattr(r["ts"], "isoformat") else r["ts"], "action": r["action"],
                 "ok": r.get("ok", True), "ip": r.get("ip", ""), "detail": r.get("detail", {})} for r in rows]
