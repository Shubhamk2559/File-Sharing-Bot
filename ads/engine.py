"""Config cache, eligibility, plan (hot path) and the periodic optimizer (background)."""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field

from . import scoring, settings, slog, store
from .scoring import NetStats

log = logging.getLogger("ads.engine")
CACHE_TTL = 15.0
_cache: dict = {"cfg": (0.0, None), "plan": (0.0, None)}


@dataclass
class Plan:
    order: list = field(default_factory=list)
    weights: dict = field(default_factory=dict)
    reason: str = ""
    mode: str = "auto"


def invalidate() -> None:
    _cache["cfg"] = (0.0, None)
    _cache["plan"] = (0.0, None)


async def get_config(force: bool = False) -> dict:
    t, cfg = _cache["cfg"]
    if not force and cfg is not None and time.time() - t < CACHE_TTL:
        return cfg
    try:
        cfg = settings.merged(await store.get_config_raw())
    except Exception:  # noqa: BLE001 — DB hiccup: keep serving the last good config
        log.exception("config read failed")
        cfg = cfg or settings.merged(None)
    _cache["cfg"] = (time.time(), cfg)
    return cfg


async def update_config(patch: dict) -> tuple[dict, list]:
    cur = await get_config(force=True)
    new, errors = settings.validate_patch(patch, cur)
    if errors:
        return cur, errors
    await store.save_config(new)
    invalidate()
    slog.event("config_updated", keys=sorted(patch))
    return new, []


async def _plan_state() -> dict:
    t, st = _cache["plan"]
    if st is not None and time.time() - t < CACHE_TTL:
        return st
    try:
        st = await store.get_doc("plan")
    except Exception:  # noqa: BLE001
        log.exception("plan read failed")
        st = st or {}
    _cache["plan"] = (time.time(), st)
    return st


def eligible(cfg: dict, benched: dict | None = None, now: float | None = None) -> list:
    """enabled + configured + reward trust allowed + not cooling down."""
    now = now or time.time()
    out, benched = [], benched or {}
    for n in settings.NETWORKS:
        c = cfg["networks"][n]
        if c["enabled"] and settings.is_configured(cfg, n) and settings.trust_allowed(cfg, n):
            out.append((n, benched.get(n, 0) > now))
    live = [n for n, b in out if not b]
    return live or [n for n, _ in out]            # everything benched -> still serve (least bad) instead of nothing


async def plan_request(rng=random) -> Plan:
    cfg = await get_config()
    st = await _plan_state()
    elig = eligible(cfg, st.get("benched"))
    prio = cfg["priority"]
    limit = cfg["max_attempts"] if cfg["fallback_enabled"] else 1
    if not elig:
        return Plan([], {}, "no eligible network", cfg["mode"])
    mode = cfg["mode"]
    if mode == "manual" and cfg["manual_network"] in elig:
        first = cfg["manual_network"]
        rest = [n for n in prio if n in elig and n != first]
        return Plan(([first] + rest)[:limit], {first: 1.0}, f"manual: {first}", mode)
    if mode == "priority" or mode == "manual":      # manual target not eligible -> degrade to priority order
        order = [n for n in prio if n in elig]
        return Plan(order[:limit], {order[0]: 1.0}, "static priority order", mode)
    order, weights, reason = scoring.choose_order(elig, st.get("scores") or {}, prio, cfg["optimization"]["explore_share"], rng)
    return Plan(order[:limit], weights, reason, mode)


# ------------------------------------------------------------------ optimizer
def _fold(rows: list, nets=settings.NETWORKS) -> dict:
    out = {n: NetStats(n) for n in nets}
    for r in rows:
        s = out.get(r.get("network"))
        if not s:
            continue
        s.requests += int(r.get("requests", 0)); s.shown += int(r.get("shown", 0)); s.completed += int(r.get("completed", 0))
        s.verified += int(r.get("verified", 0)); s.denied += int(r.get("denied", 0))
        s.failed_nofill += int(r.get("failed_nofill", 0)); s.failed_error += int(r.get("failed_error", 0))
        s.abandoned += int(r.get("abandoned", 0))
    return out


async def collect_stats(cfg: dict, now: float | None = None):
    """(window stats with confirmed-revenue join, recent stats, per-network (attempts, hard_fails) for the cooldown window)."""
    now = now or time.time()
    opt = cfg["optimization"]
    days = store.window_days(opt["window_days"])
    drows = await store.daily_rows(days)
    window = _fold(drows)
    for r in await store.revenue_rows(days):
        n, d = r.get("network"), r.get("day")
        if n not in window:
            continue
        for dr in (x for x in drows if x.get("network") == n and x.get("day") == d):
            window[n].conf_requests += int(dr.get("requests", 0))
            window[n].conf_verified += int(dr.get("verified", 0))
        window[n].conf_revenue += float(r.get("revenue_usd", 0.0))
    recent = _fold([r for r in await store.bucket_rows(now - opt["recent_hours"] * 3600)])
    cool = {n: [0, 0] for n in settings.NETWORKS}
    for r in await store.bucket_rows(now - opt["cooldown_minutes"] * 60):
        if r.get("network") in cool:
            cool[r["network"]][0] += int(r.get("requests", 0)); cool[r["network"]][1] += int(r.get("hard_fails", 0))
    return window, recent, {n: tuple(v) for n, v in cool.items()}


async def tick(now: float | None = None) -> bool:
    if not await store.try_lease("optimizer", settings.ADS_OPT_INTERVAL * 0.8):
        return False
    now = now or time.time()
    cfg = await get_config(force=True)
    opt = cfg["optimization"]
    window, recent, cool = await collect_stats(cfg, now)
    scores = scoring.compute_scores(window, recent, scoring.Params(opt["min_requests"], opt["min_revenue_impressions"],
                                                                    opt["explore_share"], opt["weights"]))
    prev = await store.get_doc("plan")
    benched = {n: u for n, u in (prev.get("benched") or {}).items() if u > now}
    for n in scoring.find_bench(cool, opt["fail_min_attempts"], opt["fail_threshold"]):
        if n not in benched:
            benched[n] = now + opt["cooldown_minutes"] * 60
            slog.event("network_benched", level="warning", network=n, minutes=opt["cooldown_minutes"])
    ranked = sorted((n for n in scores if scores[n]["measured"]), key=lambda n: -scores[n]["score"])
    leader = ranked[0] if ranked else None
    await store.set_doc("plan", {"scores": scores, "benched": benched, "leader": leader, "updated_at": now})
    if leader != prev.get("leader"):
        await store.add_history({"from": prev.get("leader"), "to": leader, "scores": {n: scores[n]["score"] for n in scores}})
    expired = await store.expire_stale(settings.ADS_SHOWN_LOCK_SECONDS + 60)
    if expired:
        slog.event("expired_attempts", count=expired)
    _cache["plan"] = (0.0, None)
    return True


async def loop() -> None:
    if not settings.SMART_ADS_ENABLED:
        return
    await asyncio.sleep(25)
    while True:
        try:
            await tick()
        except Exception:  # noqa: BLE001
            log.exception("ads optimizer tick failed")
        await asyncio.sleep(settings.ADS_OPT_INTERVAL)
