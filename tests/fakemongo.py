"""Minimal in-memory stand-in for the Motor collections used by ads.store / db sessions (test-only).
Supports: equality, None-matches-missing, $or, $lt/$lte/$gt/$gte/$ne, $expr{$gte}, $set/$inc/$setOnInsert, upsert, sort."""
from __future__ import annotations

import copy
import time

from ads.store import DuplicateKeyError

_MISSING = object()


def _get(doc, path):
    cur = doc
    for p in path.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return _MISSING
        cur = cur[p]
    return cur


def _cmp_ok(val, cond):
    if isinstance(cond, dict) and any(k.startswith("$") for k in cond):
        v = None if val is _MISSING else val
        for op, arg in cond.items():
            if op == "$ne" and not (v != arg):
                return False
            if op in ("$lt", "$lte", "$gt", "$gte"):
                if v is None:
                    return False
                if not {"$lt": v < arg, "$lte": v <= arg, "$gt": v > arg, "$gte": v >= arg}[op]:
                    return False
        return True
    if cond is None:
        return val is _MISSING or val is None
    return val is not _MISSING and val == cond


def matches(doc, flt):
    for k, cond in flt.items():
        if k == "$or":
            if not any(matches(doc, f) for f in cond):
                return False
        elif k == "$expr":
            (op, (a, b)), = cond.items()
            assert op == "$gte"
            va, vb = (doc.get(x[1:]) if isinstance(x, str) else x for x in (a, b))
            if not (va >= vb):
                return False
        elif not _cmp_ok(_get(doc, k), cond):
            return False
    return True


def _apply(doc, upd, inserting=False):
    for k, v in upd.get("$set", {}).items():
        doc[k] = v
    for k, v in upd.get("$inc", {}).items():
        doc[k] = doc.get(k, 0) + v
    if inserting:
        for k, v in upd.get("$setOnInsert", {}).items():
            doc[k] = v


class Cursor:
    def __init__(self, docs): self.docs = docs
    def limit(self, n): self.docs = self.docs[:n]; return self
    async def to_list(self, length=None): return [copy.deepcopy(d) for d in self.docs]


class Collection:
    def __init__(self): self.docs = []

    def _find(self, flt): return [d for d in self.docs if matches(d, flt)]

    async def insert_one(self, doc):
        doc = copy.deepcopy(doc)
        if "_id" in doc and any(d.get("_id") == doc["_id"] for d in self.docs):
            raise DuplicateKeyError(str(doc["_id"]))
        self.docs.append(doc)

    async def find_one(self, flt=None, **_):
        r = self._find(flt or {})
        return copy.deepcopy(r[0]) if r else None

    def find(self, flt=None): return Cursor(self._find(flt or {}))

    async def update_one(self, flt, upd, upsert=False):
        r = self._find(flt)
        if r:
            _apply(r[0], upd)
        elif upsert:
            d = {k: v for k, v in flt.items() if not k.startswith("$") and not isinstance(v, dict)}
            _apply(d, upd, inserting=True); self.docs.append(d)

    async def find_one_and_update(self, flt, upd, upsert=False, return_document=None, sort=None, **_):
        r = self._find(flt)
        if sort:
            for key, direction in reversed(sort):
                r.sort(key=lambda d: d.get(key, 0), reverse=direction < 0)
        if not r:
            return None
        before = copy.deepcopy(r[0])
        _apply(r[0], upd)
        # pymongo ReturnDocument.AFTER == True; default BEFORE
        return copy.deepcopy(r[0]) if return_document else before


class FakeDB:
    def __init__(self): self.c = {}
    def __getitem__(self, name): return self.c.setdefault(name, Collection())


class FakeSessions:
    """Same behaviour as db.py's session helpers (start_ad / complete_ad / get_session / create_session / claim_delivery)."""
    def __init__(self, fdb: FakeDB): self.col = fdb["sessions"]

    async def create_session(self, sid, token, user_id, chat_id, bot_id, required, prompt_msg=None):
        await self.col.insert_one({"sid": sid, "token": token, "user_id": user_id, "chat_id": chat_id, "bot_id": bot_id,
                                   "ads_done": 0, "ads_required": required, "ad_started_at": None, "delivered": False,
                                   "prompt_msg": prompt_msg})

    async def get_session(self, sid): return await self.col.find_one({"sid": sid})

    async def get_file(self, token): return {"token": token}

    async def start_ad(self, sid, ymid=None, zone=None):
        f = {"ad_started_at": time.time()}
        if ymid:
            f.update(cur_ymid=ymid, cur_zone=zone)
        await self.col.update_one({"sid": sid, "delivered": False}, {"$set": f})

    async def complete_ad(self, sid, min_seconds):
        cutoff = time.time() - min_seconds
        return await self.col.find_one_and_update(
            {"sid": sid, "delivered": False, "ad_started_at": {"$ne": None, "$lte": cutoff}},
            {"$inc": {"ads_done": 1}, "$set": {"ad_started_at": None}}, return_document=True)

    async def claim_delivery(self, sid):
        r = await self.col.find_one_and_update(
            {"sid": sid, "delivered": False, "$expr": {"$gte": ["$ads_done", "$ads_required"]}}, {"$set": {"delivered": True}})
        return r is not None

    async def unclaim_delivery(self, sid): await self.col.update_one({"sid": sid}, {"$set": {"delivered": False}})
