"""Idempotent migration 001: creates the smart-ads collections' indexes and seeds the default config.
Only NEW collections are touched; existing data (files, sessions, bots, stats, monetag_*) is never modified.

Run automatically on startup, or manually:   python -m ads.migrate
"""
from __future__ import annotations

import asyncio
import logging
import os

log = logging.getLogger("ads.migrate")
VERSION = 1

# (collection, keys, kwargs)
INDEXES = [
    ("ad_requests", "created", {"expireAfterSeconds": 90 * 86400}),
    ("ad_requests", [("sid", 1)], {}),
    ("ad_requests", [("user_id", 1), ("t", -1)], {}),
    ("ad_requests", [("network", 1), ("t", -1)], {}),
    ("ad_requests", [("outcome", 1), ("t", 1)], {}),
    ("ad_events", "created", {"expireAfterSeconds": 30 * 86400}),
    ("ad_events", [("ad_id", 1)], {}),
    ("ad_rewards", [("sid", 1)], {}),
    ("ad_rewards", [("user_id", 1), ("t", -1)], {}),
    ("ad_rewards", [("network", 1), ("t", -1)], {}),
    ("ad_stats_daily", [("day", 1)], {}),
    ("ad_buckets", [("bucket", 1)], {}),
    ("ad_buckets", "expire_at", {"expireAfterSeconds": 0}),
    ("ad_revenue", [("day", 1)], {}),
    ("ad_reward_hits", [("user_id", 1), ("consumed_by", 1), ("t", 1)], {}),
    ("ad_reward_hits", "created", {"expireAfterSeconds": 2 * 86400}),
    ("ad_plan_history", "created", {"expireAfterSeconds": 180 * 86400}),
]


async def run(c) -> None:
    """c(name) -> collection."""
    for coll, keys, kw in INDEXES:
        await c(coll).create_index(keys, **kw)
    await c("ad_config").update_one({"_id": "migration"}, {"$set": {"version": VERSION}}, upsert=True)
    log.info("smart-ads migration %s applied", VERSION)


def main() -> None:  # pragma: no cover - needs real MongoDB
    from motor.motor_asyncio import AsyncIOMotorClient
    db = AsyncIOMotorClient(os.environ["MONGO_URI"])[os.getenv("DB_NAME", "filebot")]
    asyncio.run(run(lambda name: db[name]))
    print("smart-ads migration applied")


if __name__ == "__main__":
    main()
