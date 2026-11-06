import base64
import binascii
import logging
import os
from dataclasses import dataclass

log = logging.getLogger("webadmin")

MIN_PASSWORD_LEN = 12


def _truthy(v: str) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


def _int(env, name: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int((env.get(name) or "").strip() or default)
    except ValueError:
        v = default
    return max(lo, min(hi, v))


def valid_base32(secret: str) -> bool:
    try:
        s = secret.strip().replace(" ", "").upper()
        base64.b32decode(s + "=" * (-len(s) % 8), casefold=True)
        return len(s) >= 16
    except (binascii.Error, ValueError):
        return False


@dataclass(frozen=True)
class PanelSettings:
    password: str = ""
    totp_secret: str = ""
    session_hours: int = 12
    secure_cookies: bool = True
    max_fails: int = 5
    lockout_seconds: int = 900
    upload_max_bytes: int = 50 * 1024 * 1024
    trust_proxy: bool = True
    problem: str = ""          # why the panel is disabled although a password was given

    @property
    def enabled(self) -> bool:
        return bool(self.password) and not self.problem

    @property
    def totp_enabled(self) -> bool:
        return bool(self.totp_secret)

    @classmethod
    def from_env(cls, env=None) -> "PanelSettings":
        env = os.environ if env is None else env
        password = (env.get("ADMIN_PANEL_PASSWORD") or "").strip()
        totp = (env.get("ADMIN_PANEL_TOTP_SECRET") or "").strip().replace(" ", "")
        problem = ""
        if password and len(password) < MIN_PASSWORD_LEN:
            problem = f"ADMIN_PANEL_PASSWORD must be at least {MIN_PASSWORD_LEN} characters - admin panel disabled"
        if totp and not valid_base32(totp):
            problem = "ADMIN_PANEL_TOTP_SECRET is not valid base32 (min 16 chars) - admin panel disabled"
        if problem:
            log.error(problem)
        return cls(
            password=password,
            totp_secret=totp.upper(),
            session_hours=_int(env, "ADMIN_PANEL_SESSION_HOURS", 12, 1, 168),
            secure_cookies=not _truthy(env.get("ADMIN_PANEL_INSECURE_COOKIES", "")),
            max_fails=_int(env, "ADMIN_PANEL_MAX_FAILS", 5, 3, 20),
            lockout_seconds=_int(env, "ADMIN_PANEL_LOCKOUT_SECONDS", 900, 60, 86400),
            trust_proxy=not _truthy(env.get("ADMIN_PANEL_NO_PROXY", "")),
            upload_max_bytes=_int(env, "ADMIN_PANEL_UPLOAD_MAX_MB", 50, 1, 50) * 1024 * 1024,
            problem=problem,
        )
