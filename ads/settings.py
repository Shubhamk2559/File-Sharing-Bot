"""Environment + runtime (DB-stored, admin-editable) configuration. No I/O, no import of config.py."""
from __future__ import annotations

import copy
import os
import re

NETWORKS = ("monetag", "adsgram", "tads")
NET_LABEL = {"monetag": "Monetag", "adsgram": "AdsGram", "tads": "Tads.me"}
CLIENT_ATTESTED, SERVER_REWARD_URL, SERVER_POSTBACK = "client_attested", "server_reward_url", "server_postback"
TRUST_RANK = {CLIENT_ATTESTED: 1, SERVER_REWARD_URL: 2, SERVER_POSTBACK: 3}


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except ValueError:
        return default


SMART_ADS_ENABLED = _bool("SMART_ADS_ENABLED", False)
ADS_ADMIN_TOKEN = os.getenv("ADS_ADMIN_TOKEN", "").strip() or os.getenv("MONETAG_ADMIN_TOKEN", "").strip()
ADS_OPT_INTERVAL = max(60, _int("ADS_OPT_INTERVAL", 300))
ADS_UNSHOWN_LOCK_SECONDS = _int("ADS_UNSHOWN_LOCK_SECONDS", 25)    # attempt that never displayed frees the user slot
ADS_SHOWN_LOCK_SECONDS = _int("ADS_SHOWN_LOCK_SECONDS", 180)       # displayed ad (video + verification wait)
STATS_TZ_OFFSET_MIN = _int("STATS_TZ_OFFSET_MIN", 330)

ADSGRAM_BLOCK_ID = os.getenv("ADSGRAM_BLOCK_ID", "").strip()
ADSGRAM_SDK_URL = os.getenv("ADSGRAM_SDK_URL", "https://sad.adsgram.ai/js/sad.min.js").strip()
ADSGRAM_REWARD_SECRET = os.getenv("ADSGRAM_REWARD_SECRET", "").strip()
ADSGRAM_DEBUG = _bool("ADSGRAM_DEBUG", False)

TADS_WIDGET_ID = os.getenv("TADS_WIDGET_ID", "").strip()
TADS_SDK_URL = os.getenv("TADS_SDK_URL", "https://w.tads.me/widget.js").strip()
TADS_DEBUG = _bool("TADS_DEBUG", False)
TADS_ALLOW_CLIENT_REWARD = _bool("TADS_ALLOW_CLIENT_REWARD", False)

# Filled by routes.register() from the real config.py (kept out of here so tests need no env).
RUNTIME = {"monetag_postback": False, "adsgram_secret": bool(ADSGRAM_REWARD_SECRET)}

_ID_RE = {"adsgram": re.compile(r"^\d{1,12}$"), "tads": re.compile(r"^[A-Za-z0-9_-]{1,64}$")}

DEFAULT_CONFIG = {
    "mode": "auto",                     # auto = performance-based | priority = static order | manual = forced first pick
    "manual_network": None,
    "priority": list(NETWORKS),
    "fallback_enabled": True,
    "max_attempts": 3,                  # attempts per user request (each only if the previous never displayed an ad)
    "networks": {
        "monetag": {"enabled": True, "allow_client_reward": True, "unit_id": None},     # legacy behaviour preserved
        "adsgram": {"enabled": False, "allow_client_reward": False, "unit_id": None},
        "tads": {"enabled": False, "allow_client_reward": TADS_ALLOW_CLIENT_REWARD, "unit_id": None},
    },
    "optimization": {
        "window_days": 7, "recent_hours": 24, "min_requests": 100, "min_revenue_impressions": 100,
        "explore_share": 0.15, "cooldown_minutes": 30, "fail_threshold": 0.7, "fail_min_attempts": 15,
        "weights": {"revenue": 0.55, "fill": 0.15, "completion": 0.15, "errors": 0.05, "recent": 0.10},
    },
}

_OPT_RANGES = {
    "window_days": (1, 90, int), "recent_hours": (1, 48, int), "min_requests": (10, 100000, int),
    "min_revenue_impressions": (10, 1000000, int), "explore_share": (0.0, 0.5, float),
    "cooldown_minutes": (1, 1440, int), "fail_threshold": (0.3, 1.0, float), "fail_min_attempts": (5, 10000, int),
}


def merged(stored: dict | None) -> dict:
    """Defaults <- stored overrides (deep for the known nested dicts)."""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    s = stored or {}
    for k in ("mode", "manual_network", "fallback_enabled", "max_attempts"):
        if k in s:
            cfg[k] = s[k]
    if isinstance(s.get("priority"), list) and sorted(s["priority"]) == sorted(NETWORKS):
        cfg["priority"] = list(s["priority"])
    for n in NETWORKS:
        cfg["networks"][n].update({k: v for k, v in (s.get("networks", {}).get(n) or {}).items() if k in cfg["networks"][n]})
    opt = s.get("optimization") or {}
    cfg["optimization"].update({k: v for k, v in opt.items() if k in cfg["optimization"] and k != "weights"})
    if isinstance(opt.get("weights"), dict):
        cfg["optimization"]["weights"].update({k: v for k, v in opt["weights"].items() if k in cfg["optimization"]["weights"]})
    return cfg


def unit_id(cfg: dict, net: str) -> str | None:
    """Public identifier (AdsGram blockId / Tads widgetId). DB override wins over env. Not a secret."""
    if net == "monetag":
        return "zone"
    v = cfg["networks"][net].get("unit_id") or (ADSGRAM_BLOCK_ID if net == "adsgram" else TADS_WIDGET_ID)
    return v or None


def is_configured(cfg: dict, net: str) -> bool:
    return bool(unit_id(cfg, net))


def trust_for(net: str) -> str:
    if net == "monetag":
        return SERVER_POSTBACK if RUNTIME["monetag_postback"] else CLIENT_ATTESTED
    if net == "adsgram":
        return SERVER_REWARD_URL if RUNTIME["adsgram_secret"] else CLIENT_ATTESTED
    return CLIENT_ATTESTED          # tads: only client callbacks are documented


def trust_allowed(cfg: dict, net: str) -> bool:
    return trust_for(net) != CLIENT_ATTESTED or bool(cfg["networks"][net]["allow_client_reward"])


def validate_patch(patch: dict, current: dict) -> tuple[dict, list]:
    """Returns (new full config, errors). Pure; nothing is applied if errors is non-empty."""
    errors: list = []
    new = copy.deepcopy(current)
    if not isinstance(patch, dict):
        return current, ["body must be a JSON object"]
    allowed = {"mode", "manual_network", "priority", "fallback_enabled", "max_attempts", "networks", "optimization"}
    for k in patch:
        if k not in allowed:
            errors.append(f"unknown key: {k}")
    if "mode" in patch:
        if patch["mode"] in ("auto", "priority", "manual"):
            new["mode"] = patch["mode"]
        else:
            errors.append("mode must be auto|priority|manual")
    if "manual_network" in patch:
        if patch["manual_network"] is None or patch["manual_network"] in NETWORKS:
            new["manual_network"] = patch["manual_network"]
        else:
            errors.append("manual_network must be null or a known network")
    if "priority" in patch:
        p = patch["priority"]
        if isinstance(p, list) and sorted(p) == sorted(NETWORKS):
            new["priority"] = list(p)
        else:
            errors.append("priority must list each network exactly once")
    if "fallback_enabled" in patch:
        if isinstance(patch["fallback_enabled"], bool):
            new["fallback_enabled"] = patch["fallback_enabled"]
        else:
            errors.append("fallback_enabled must be boolean")
    if "max_attempts" in patch:
        v = patch["max_attempts"]
        if isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 3:
            new["max_attempts"] = v
        else:
            errors.append("max_attempts must be 1..3")
    for n, nv in (patch.get("networks") or {}).items():
        if n not in NETWORKS or not isinstance(nv, dict):
            errors.append(f"unknown network: {n}")
            continue
        for k, v in nv.items():
            if k in ("enabled", "allow_client_reward") and isinstance(v, bool):
                new["networks"][n][k] = v
            elif k == "unit_id" and n in _ID_RE and (v is None or (isinstance(v, str) and (v == "" or _ID_RE[n].match(v)))):
                new["networks"][n][k] = v or None
            else:
                errors.append(f"networks.{n}.{k}: invalid")
    opt = patch.get("optimization")
    if opt is not None:
        if not isinstance(opt, dict):
            errors.append("optimization must be an object")
        else:
            for k, v in opt.items():
                if k == "weights":
                    w = v if isinstance(v, dict) else {}
                    if set(w) - set(DEFAULT_CONFIG["optimization"]["weights"]) or not w:
                        errors.append("weights: unknown/empty")
                        continue
                    if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not 0 <= x <= 1 for x in w.values()):
                        errors.append("weights must be numbers in 0..1")
                        continue
                    merged_w = dict(new["optimization"]["weights"], **{a: float(b) for a, b in w.items()})
                    if sum(merged_w.values()) <= 0:
                        errors.append("weights must not all be zero")
                        continue
                    new["optimization"]["weights"] = merged_w
                elif k in _OPT_RANGES:
                    lo, hi, typ = _OPT_RANGES[k]
                    if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
                        errors.append(f"optimization.{k} must be {lo}..{hi}")
                    else:
                        new["optimization"][k] = typ(v)
                else:
                    errors.append(f"optimization.{k}: unknown")
    # cross-field checks on the resulting config
    for n in ("adsgram", "tads"):
        if new["networks"][n]["enabled"] and not is_configured(new, n):
            errors.append(f"{NET_LABEL[n]} cannot be enabled: set its {'block' if n == 'adsgram' else 'widget'} ID first")
    if new["mode"] == "manual" and not new["manual_network"]:
        errors.append("manual mode needs manual_network")
    return (current if errors else new), errors
