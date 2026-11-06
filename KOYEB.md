# Koyeb deploy — SIRF 1 service

Koyeb -> Create Service -> GitHub repo -> Builder: Dockerfile -> Port 8000 -> Health check `/`.
ROLE set mat karo (default = both: main bot + delivery bots + Mini App ek hi service me).
BASE_URL = is service ka apna URL (https://<n>.koyeb.app).

---

# Monetag Smart Monetization Engine

Pehle jaisa sab chalta rahega. Naya kuch bhi tab tak active nahi jab tak aap env set na karo.

## Kya Monetag officially support karta hai (docs.monetag.com se verify kiya)
- **Rewarded Interstitial** — `show_<ZONE>({ymid, requestVar})`, ad band hone par promise resolve. **Yahi gate hai.**
- **Rewarded Popup** (`type:'pop'`) — naye tab me advertiser page kholta hai, ~2s baad confirm. User ko ad "dekhna" nahi padta => reward semantics alag => **gate ke liye implement nahi kiya.**
- **In-App Interstitial** (`type:'inApp'`) — apne aap interval par dikhta hai, per-show promise nahi (`undefined`) => reward gate nahi ban sakta => **implement nahi kiya.**
- Postback macros: `ymid, zone_id, sub_zone_id, request_var, telegram_id, event_type, reward_event_type, estimated_price`. **Country/geo macro nahi hai.**
- "One Mini App = one SDK main zone = one postback setup." Main zone ke andar kaunsa sub-zone/advertiser chalega, ye **Monetag decide karta hai**, aap nahi.
- Koi documented publisher reporting API nahi mila => **confirmed revenue automatically fetch nahi hota** (neeche manual entry).

## Step 1 — Postback (analytics + secure verification ki neev)
1. Koyeb env: `MONETAG_POSTBACK_SECRET=<lamba random string>` (sirf letters/digits).
2. Monetag SSP -> apna **main zone** -> *Postback URL*:
```
https://<BASE_URL>/api/monetag/postback?secret=<MONETAG_POSTBACK_SECRET>&ymid={ymid}&event_type={event_type}&reward_event_type={reward_event_type}&zone_id={zone_id}&sub_zone_id={sub_zone_id}&estimated_price={estimated_price}&request_var={request_var}&telegram_id={telegram_id}
```
3. Har extra main zone me bhi yahi URL daalo.
4. 2-3 ads khud dekho. Owner bot -> **💰 Monetag**: impressions aane chahiye. Ya `GET /admin/monetag` (token ke saath).

## Step 2 — Server-side reward verification (recommended)
Purana flow (`AD_VERIFY_MODE=legacy`) = SDK promise resolve + `MIN_AD_SECONDS`. Monetag docs ke mutabik client-side signal forge ho sakta hai.
Step 1 verify hone ke baad `AD_VERIFY_MODE=postback` karo: file tabhi milegi jab is ad ke `ymid` ka Monetag `valued` impression postback server par aa chuka ho.
**Postback URL galat/missing ho to postback mode me koi file nahi milegi** — isliye pehle Step 1 test karo.

## Step 3 — Multi-zone + auto optimization
```
MONETAG_ZONES=12181474,<second main zone>[|<its sdk url>]
MONETAG_DEFAULT_ZONE=12181474
MONETAG_OPTIMIZE=true
```
Dhyan: extra zone Monetag dashboard me **main SDK zone** hona chahiye. Docs ye confirm nahi karte ki ek hi Mini App URL par 2 main zones ban sakte hain; agar nahi bane to optimizer ka koi matlab nahi (sirf 1 zone). Ek zone ho to bhi analytics poora kaam karta hai.

### Optimizer kaise kaam karta hai
- Metric: `eCPM = sum(estimated_price) / impressions * 1000`, rolling `MONETAG_WINDOW_DAYS` din. Yeh **historical measured** hai; Monetag live CPM nahi deta.
- Warm-up: jab tak har zone par `MONETAG_MIN_IMPRESSIONS` impressions nahi => default zone ~80%, baaki `MONETAG_EXPLORE_SHARE` (20%) me barabar.
- Compare: small-sample se bachne ke liye scores pooled average ki taraf shrink hote hain; leader tabhi badalta hai jab challenger noise band (`MONETAG_Z_SCORE` x standard error) se zyada behtar ho **aur** pichhle switch ko `MONETAG_MIN_SWITCH_HOURS` ho chuke ho.
- Traffic: leader >= 50%; baaki zones ko `MONETAG_MIN_SHARE` floor; saaf taur par kharab zone ka exploration aadha.
- Auto-bench: kisi zone ke ads `MONETAG_BENCH_FAIL_RATE` se zyada fail (Empty feed/timeout/error, min `MONETAG_BENCH_MIN_ATTEMPTS`) => `MONETAG_BENCH_MINUTES` ke liye band. Sab fail ho to default zone.
- Har `MONETAG_OPT_INTERVAL` sec me ek round; multi-instance me lease se sirf ek chalta hai. Hot path me 30s cache, DB query nahi.
- 1k-5k/day par ~300 impressions/zone me kuch ghante-din lagte hain; chhota eCPM farq statistically detect nahi hoga — yeh jaan-boojh kar conservative hai.

## Environment variables
| Var | Default | Matlab |
|---|---|---|
| `MONETAG_ZONES` | khali | Main SDK zones `zone[\|sdk_url],...` |
| `MONETAG_DEFAULT_ZONE` | `MONETAG_ZONE_ID` | Fallback/warm-up zone |
| `MONETAG_OPTIMIZE` | `false` | Optimizer on/off |
| `MONETAG_POSTBACK_SECRET` | khali | Postback endpoint ka secret (khali = endpoint band) |
| `AD_VERIFY_MODE` | `legacy` | `legacy` / `postback` |
| `POSTBACK_WAIT_SECONDS` | `20` | Postback ka intezaar |
| `BLOCK_NON_VALUED` | `true` | SDK `non_valued` bataye to file nahi |
| `MONETAG_MIN_IMPRESSIONS` | `300` | Compare se pehle min impressions/zone |
| `MONETAG_WINDOW_DAYS` | `7` | Rolling window |
| `MONETAG_EXPLORE_SHARE` | `0.2` | Non-leader zones ko kul traffic (max 0.5) |
| `MONETAG_MIN_SHARE` | `0.05` | Har zone ka floor |
| `MONETAG_MIN_SWITCH_HOURS` | `24` | 2 switches ke beech |
| `MONETAG_Z_SCORE` | `1.28` | Significance (~90%) |
| `MONETAG_OPT_INTERVAL` | `900` | Optimizer period (sec) |
| `MONETAG_BENCH_FAIL_RATE` / `_MIN_ATTEMPTS` / `_MINUTES` | `0.6` / `20` / `30` | Auto-bench |
| `MONETAG_ADMIN_TOKEN` | khali | `/admin/monetag` Bearer token |
| `MONETAG_GEO_HEADER` | khali | Proxy ka country header (country-wise eCPM) |

Secrets (`MONETAG_POSTBACK_SECRET`, `MONETAG_ADMIN_TOKEN`) frontend me kabhi nahi jaate; access log me `secret=***` ban kar chhapte hain.

## Analytics
- Owner bot -> **💰 Monetag**
- `curl -H "Authorization: Bearer $MONETAG_ADMIN_TOKEN" https://<BASE_URL>/admin/monetag?days=7`
- **Estimated** = postback `estimated_price` ka sum (andaza). **Confirmed** = sirf wo jo aap dashboard dekh kar dalo:
```
curl -X POST -H "Authorization: Bearer $MONETAG_ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d '{"day":"2026-11-03","revenue_usd":12.34}' https://<BASE_URL>/admin/monetag/confirmed
```
- Country-wise eCPM: Monetag postback me country nahi aata. Sirf tab milega jab aapka proxy country header bheje aur `MONETAG_GEO_HEADER` set ho (Koyeb ke default header ki guarantee nahi — pehle check karo).
- Fill rate sirf SDK ke `Empty feed` reports se maapa jata hai (client-reported).

## Testing
1. `python -m unittest discover -s tests -v`
2. Deploy, `MONETAG_OPTIMIZE=false`, `AD_VERIFY_MODE=legacy`: link kholo, ad dekho, file aani chahiye (purana flow).
3. Postback set karo, ad dekho, 💰 Monetag me impression + estimated revenue check karo.
4. `AD_VERIFY_MODE=postback`: ad dekho => file kuch second baad aani chahiye. Ad turant band karke dobara try karo, logs me `non_valued`/unverified dekho.
5. Extra zone daalo, `MONETAG_OPTIMIZE=true`: `MONETAG_ZONES` me dono, kuch ghante baad 💰 Monetag me traffic split dikhega.
