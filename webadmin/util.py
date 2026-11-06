"""Small helpers shared by the route modules."""
import re
from datetime import datetime

from .core import err

_TOKEN_RX = re.compile(r"\d{5,}:[A-Za-z0-9_-]{20,}")


def redact(text: str) -> str:
    """Strip anything that looks like a bot token from text before it goes to the browser or a log."""
    return _TOKEN_RX.sub("[redacted]", str(text))


def tg_error(e: Exception, what: str = "Telegram error"):
    return err(502, "telegram_error", f"{what}: {redact(e)[:200]}")


def jsonable(d):
    if isinstance(d, dict):
        return {str(k): jsonable(v) for k, v in d.items() if k != "_id"}
    if isinstance(d, (list, tuple, set)):
        return [jsonable(v) for v in d]
    if isinstance(d, datetime):
        return d.isoformat()
    if isinstance(d, bytes):
        return None
    return d


def q_int(req, name: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(req.query.get(name, default))
    except (TypeError, ValueError):
        raise err(422, "validation_error", f"{name} must be an integer") from None
    return max(lo, min(hi, v))
