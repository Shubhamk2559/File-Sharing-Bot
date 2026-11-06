# HelpBot — file delivery bot with Smart Ads (Monetag + AdsGram + Tads.me)

Users open a link, watch **one rewarded ad** in the Telegram Mini App, and the file is delivered to their chat.
The original Monetag flow is untouched. The smart multi-network layer is **off until `SMART_ADS_ENABLED=true`**.

> Status: implemented and unit-tested with mocks. **Not verified against live AdsGram / Tads / Monetag accounts** (no credentials
> were available). Follow "First live test" below before relying on it. See "Limitations".

## What was inspected / what is reused
* "Reward" in this project = **file delivery** (there are no balances/referrals to protect). Reward state still lives in your
  existing `sessions` collection (`ads_done`, `delivered`); the smart layer reuses `db.start_ad / complete_ad / claim_delivery`.
* Existing Monetag integration (zone, SDK, `ymid`, postback endpoint, zone optimizer, `AD_VERIFY_MODE`) is reused as-is.
  Monetag attempts made through the smart flow still feed `monetag_*` analytics and its postback verification.
* New files: `ads/` package, `tests/`, `migrations/`, `run_tests.sh`. Edited (additive, gated): `main.py`, `manager.py`,
  `handlers.py`, `miniapp.html`, `_.env.example`.

## Setup per network

### Monetag (already working)
Nothing new. For the strongest verification set `AD_VERIFY_MODE=postback` + `MONETAG_POSTBACK_SECRET` (see `KOYEB.md`).
Reward trust: `server_postback` in postback mode, otherwise `client_attested` (the existing behaviour, allowed by default).

### AdsGram
1. Register at https://partner.adsgram.ai, create an **Ad platform** (Telegram direct link, Web app URL = `BASE_URL/app`, Bot ID).
2. Create an **Ad unit** of type **Reward** (not Interstitial). Submit for moderation. Copy the **BlockID** → `ADSGRAM_BLOCK_ID`.
3. Server verification (recommended): set `ADSGRAM_REWARD_SECRET=<long random letters/digits>` and in the unit's **Reward URL** field enter
   `https://<BASE_URL host>/api/adsgram/reward?secret=<ADSGRAM_REWARD_SECRET>&userid=[userId]`
   AdsGram replaces `[userId]` with the Telegram ID. Per AdsGram docs this is optional and "makes sense" above ~50k daily users — ask their support if it is
   unavailable for you. If you set the secret but the Reward URL is not configured on their side, **AdsGram rewards will be denied** (fail-closed).
4. Without a secret, AdsGram is `client_attested` and stays **ineligible** until you enable "Allow client-attested rewards" for it.
* Test platform + `ADSGRAM_DEBUG=true` for development (test ads, no Reward URL call, no stats).

### Tads.me
1. Register at https://tads.me, add your Mini App (Publisher → Sites), create a **fullscreen** widget (Publisher → Widgets), copy the ID → `TADS_WIDGET_ID`.
2. Tads documents only browser callbacks (`onShowReward`, `onClickReward`, `onAdsNotFound`) — no postback, no signature, no reporting API that I could find.
   Therefore Tads rewards are `client_attested` and Tads is **never selected** unless you opt in (`TADS_ALLOW_CLIENT_REWARD=true` or the toggle in the panel).
   A click is never treated as completion; only `onShowReward`.

### Credentials / IDs you must obtain
| Item | Where | Env / setting |
|---|---|---|
| Monetag zone ID | existing | `MONETAG_ZONE_ID` (already set) |
| AdsGram BlockID (Reward unit) | partner.adsgram.ai | `ADSGRAM_BLOCK_ID` (or dashboard field) |
| AdsGram Reward URL secret | you invent it | `ADSGRAM_REWARD_SECRET` |
| Tads widget ID (fullscreen) | tads.me | `TADS_WIDGET_ID` (or dashboard field) |
| Admin token | you invent it | `ADS_ADMIN_TOKEN` |

No AdsGram/Tads API key is needed (neither documents a reporting API); block/widget IDs are public identifiers, secrets stay in env.

## Turning it on
1. `SMART_ADS_ENABLED=true`, `ADS_ADMIN_TOKEN=...`, plus the IDs above. Redeploy (the migration runs on startup; or `python -m ads.migrate`).
2. Owner bot → **🧠 Smart Ads** (toggle networks / mode / fallback) and `https://<BASE_URL>/admin/ads` (analytics, config, revenue entry).
3. Enter each network's real daily revenue from its own dashboard (Confirmed revenue form). This is what makes eCPM comparisons possible.

### First live test (do this before trusting it)
1. Start with `mode=manual` + one network, `ADSGRAM_DEBUG`/`TADS_DEBUG=true` on a test platform; confirm the ad shows and the file arrives.
2. Production IDs, watch one ad per network yourself; check **Latest verified rewards** in the dashboard shows the right `trust`.
3. AdsGram: confirm a `adsgram_reward_hit` log line appears after you watch an ad. If not, the Reward URL is not set correctly.
4. Close/skip an ad and confirm **no file** arrives and the attempt shows as abandoned.

## How smart rotation works
* **Request**: `/api/smart/start` builds an ordered chain of eligible networks (enabled, configured, reward-trust allowed, not cooling down).
* **Modes**: `auto` (performance-weighted), `priority` (fixed order), `manual` (forced first pick; others remain fallback). Configurable fallback and attempts (1–3).
* **Scoring** (rolling `window_days`, default 7): weighted blend of revenue-per-request, fill rate, completion rate, (1 − error rate), recent yield (weights editable).
  * A network is ranked only after `min_requests` (100) in the window; below that it is *explored* with a small share (`explore_share` 15%, unmeasured networks get 2×).
  * Revenue counts **only when ≥2 networks have owner-confirmed revenue with ≥`min_revenue_impressions`**; values are shrunk toward the pooled mean.
    Otherwise the revenue weight is dropped and reliability decides. Monetag's postback estimate is shown but never compared with confirmed revenue.
  * Cold start (no data): priority-weighted random.
* **Health**: ≥`fail_min_attempts` attempts with ≥`fail_threshold` failures in `cooldown_minutes` ⇒ network benched; if everything is benched the least-bad still serve.
* **One ad per request**: one atomic "ad slot" per session; a fallback is issued **only if the previous attempt never displayed an ad**. An ad that displayed and then
  failed/was closed never chains another ad; the user retries manually.
* The optimizer runs every `ADS_OPT_INTERVAL`s under a DB lease (safe with multiple instances) and also expires stuck attempts.

## Reward verification
| Network | Signal | Trust label | Notes |
|---|---|---|---|
| Monetag, `AD_VERIFY_MODE=postback` | `valued` impression postback for this ad's `ymid` | `server_postback` | strongest |
| Monetag, legacy | SDK promise + min seconds | `client_attested` | existing behaviour |
| AdsGram + secret | `onReward` **and** a Reward-URL hit for the user after the ad started, consumed once | `server_reward_url` | hit carries user id only (no ad id) |
| AdsGram, no secret | `onReward` only | `client_attested` | opt-in |
| Tads | `onShowReward` only | `client_attested` | opt-in |

Always enforced: Telegram `initData` signature + session ownership, unique `ad_id` per attempt (also Monetag `ymid`), exactly-once outcome and reward
record (`ad_rewards._id = ad_id`), client can send only `completed`/`non_valued` (revenue/trust/network are ignored), min-seconds, rate limits.
Every reward record stores its trust level so you can audit it.

## Data model (all new, MongoDB)
`ad_config` (settings, plan, health, leases) · `ad_requests` (attempts) · `ad_events` (audit, 30d TTL) · `ad_rewards` (verified rewards, kept) ·
`ad_revenue` (owner-confirmed revenue) · `ad_stats_daily` · `ad_buckets` (10-min rollups, 2d TTL) · `ad_reward_hits` (AdsGram, 2d TTL) · `ad_plan_history`.
Indexes/TTLs: `ads/migrate.py` (idempotent; touches no existing collection). Existing sessions get optional `smart_*` fields only.

## Admin
* **Dashboard** `/admin/ads` (Bearer token typed into the page, kept only in sessionStorage; no secret is ever served to the browser):
  KPIs, daily chart, per-network table (status, last success, today/total verified, fill, completion, failures, confirmed vs estimated revenue/eCPM, trust), routing,
  network toggles, optimization settings, confirmed-revenue entry, latest rewards.
* **API** (Bearer `ADS_ADMIN_TOKEN`; 404 if unset; 10 failed attempts / 10 min / IP ⇒ 429): `GET /admin/ads/api/overview?days=`, `GET|POST /admin/ads/api/config`,
  `POST /admin/ads/api/revenue {"day","network","revenue_usd"}`, `GET /admin/ads/api/rewards`.
* **Telegram** 🧠 Smart Ads (owner only).

## Tests — what was actually run
`./run_tests.sh` (tests/ was missing from the zip you sent and has been recreated). Covered: scoring/selection, verification decisions, config validation,
start/fallback/one-ad-per-request, duplicate and concurrent reward claims, wrong user/session, AdsGram hit single-use/user-bound/time-bound, invalid postbacks/Reward-URL calls,
revenue/eCPM math (confirmed vs estimated vs unavailable), admin auth/lockout/validation, HTTP handlers + delivery tail, route registration, panel toggles, migration idempotency,
all three browser adapters with mocked SDKs (completion, skip, no-fill, errors, timeouts, click ≠ completion).
**Not run here**: real FastAPI, real MongoDB (an in-memory fake was used, so atomicity of `find_one_and_update` is relied upon, not proven), python-telegram-bot callback glue
(only the pure panel builders are tested), any live ad network.

## Limitations / manual steps
* No live publisher verification. AdsGram/Tads SDK usage follows their public docs as of 2026-11-05; the Tads vanilla-JS page was only partly readable (init key `widgetId`,
  script `https://w.tads.me/widget.js` taken from docs + the official `TADS-me/tads-examples` repo). Re-check on first live test.
* Revenue: no documented reporting API for AdsGram/Tads (and none for Monetag) ⇒ revenue/eCPM is **owner-entered** or "unavailable"; nothing is estimated for them.
* Tads cannot be server-verified; AdsGram's Reward URL has no ad id (a hit may be consumed by any open attempt of the same user within the window) and may require ~50k DAU.
* Monetag has no first-frame callback, so "displayed" is inferred (failure after ≥3 s counts as displayed → no fallback). Tads has no documented close callback: a closed overlay
  ends via a 90 s timeout (abandoned).
* India/device segmentation is **not** used for ranking: none of the networks send country in callbacks; only Monetag zone stats can use `MONETAG_GEO_HEADER`.
* Rate limits are per process. One live ad is enforced per session, not across multiple simultaneous sessions of the same user.
* The Monetag default zone SDK tag is still loaded statically (existing behaviour); AdsGram/Tads SDKs load lazily, only when selected.


## Troubleshooting: `POST /api/smart/start → 422` and "Session expired."
**Cause (fixed):** `ads/routes.py` had `from __future__ import annotations` while FastAPI's `Request` is imported *inside* `register()`.
FastAPI resolves handler annotations from module globals, so `request: Request` stayed an unresolved string and FastAPI treated `request` as a
required **query parameter**. Every call - whatever the body - returned `422 {"detail":[{"type":"missing","loc":["query","request"],"msg":"Field required"}]}`.
The 422 JSON has no `ok`/`error`, so the Mini App fell through to its `|| "Session expired."` fallback. MongoDB TTL, sid and payload were never the problem
(the payload `{sid, init_data}` matches what the backend reads). Fix: the future-import is removed (with a warning comment) and a regression test guards it.

Also added: structured `{"ok":false,"code":...,"error":...}` for validation errors (422), invalid JSON / wrong content-type (400); safe logs (`validation_error`,
`bad_request`, `smart_start` with session lookup result - never initData, tokens or secrets); the Mini App now shows the real category (network, timeout, HTTP status,
server message) and only says "Session expired" when the server says so; 20 s request timeout.

| Symptom | Meaning / action |
|---|---|
| `validation_error` in UI/logs | Read `details[].field`; log line `{"ev":"validation_error",...}` |
| `bad_content_type` / `invalid_json` | Client must send `Content-Type: application/json` and a JSON object |
| "Session expired. Please open the link again." (`code: session`) | Real: sid unknown or older than `SESSION_TTL` (default 30 min) - reopen from the bot |
| "Verification failed" | initData invalid/older than 1 h, or opened by a different Telegram user |
| "No ad provider is available" (`no_network`) | No network is enabled+configured+trusted. Networks are enabled in **/admin/ads** (AdsGram/Tads default **off**); Tads is excluded while `TADS_ALLOW_CLIENT_REWARD=false` |
| "An ad is already loading" (`busy`) | One live ad per session; clears after `ADS_UNSHOWN_LOCK_SECONDS` |
| AdsGram ad plays but "couldn't confirm" | Reward URL not configured/reachable or secret mismatch; check `adsgram_hit_bad_secret` logs |
| `ADSGRAM_REWARD_SECRET=YOUR_RANDOM_SECRET` | That is a placeholder - replace it with a real random value in both env and the AdsGram Reward URL |

### API smoke test
```
curl -s -X POST $BASE_URL/api/smart/start -H 'Content-Type: application/json' -d '{}'
  -> {"ok":false,"error":"Please open this inside Telegram."}            (200, understandable, not 422)
curl -s -X POST $BASE_URL/api/smart/start -d 'sid=1'
  -> 400 {"ok":false,"code":"bad_content_type",...}
curl -s -X POST $BASE_URL/api/smart/start -H 'Content-Type: application/json' -d '{oops'
  -> 400 {"ok":false,"code":"invalid_json",...}
```
A fully valid call needs real Telegram `initData` (HMAC-signed with your bot token) - test inside the Mini App.

### Deploy (Koyeb / Mistral-style container)
Push the repo, keep the Dockerfile builder, set env vars from `_.env.example`, `SMART_ADS_ENABLED=true`, redeploy. Logs must show
`POST /api/smart/start 200` instead of 422. Then open `/admin/ads` and enable AdsGram.
