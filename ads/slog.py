"""One JSON object per log line (grep/ship friendly). Never pass secrets in fields."""
from __future__ import annotations

import json
import logging
import time

_log = logging.getLogger("ads")


def event(name: str, level: str = "info", **fields) -> None:
    rec = {"ev": name, "t": round(time.time(), 3)}
    rec.update({k: v for k, v in fields.items() if v is not None})
    _log.log(getattr(logging, level.upper(), logging.INFO), json.dumps(rec, default=str, separators=(",", ":")))
