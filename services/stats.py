"""Stats shared by the Telegram 'Stats' screen and the web dashboard."""
import time

import db
from config import SESSION_TTL


async def collect() -> dict:
    """Same numbers the bot's 📊 Stats screen shows."""
    days = await db.views_last_days(7)
    return {
        "files": await db.count_files(),
        "views_today": days[0][1],
        "views_total": await db.views_total(),
        "last_days": [{"day": d, "views": v} for d, v in days],
    }


async def sessions_overview() -> dict:
    """Verification sessions live in a TTL collection (SESSION_TTL seconds) - this is the only per-user / per-bot
    data the project keeps, so 'users' here means distinct users seen in that window, not all-time users."""
    rows = await db.sessions.find({}, {"user_id": 1, "bot_id": 1, "delivered": 1}).to_list(length=5000)
    users = {r.get("user_id") for r in rows}
    per_bot: dict = {}
    for r in rows:
        b = per_bot.setdefault(str(r.get("bot_id")), {"sessions": 0, "delivered": 0, "users": set()})
        b["sessions"] += 1
        b["delivered"] += 1 if r.get("delivered") else 0
        b["users"].add(r.get("user_id"))
    return {
        "window_seconds": SESSION_TTL,
        "sessions": len(rows),
        "users": len(users),
        "delivered": sum(1 for r in rows if r.get("delivered")),
        "per_bot": {k: {"sessions": v["sessions"], "delivered": v["delivered"], "users": len(v["users"])} for k, v in per_bot.items()},
        "as_of": time.time(),
    }
