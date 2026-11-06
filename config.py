import os


def _req(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing environment variable: {name}")
    return value


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


# both   = ek hi service me sab kuch (default, recommended)
# main   = manager bot (admin commands, uploads, health check)
# worker = secondary delivery bots + Mini App (ad verification)
ROLE = os.getenv("ROLE", "both").strip().lower()
if ROLE not in ("main", "worker", "both"):
    raise RuntimeError("ROLE must be 'both', 'main' or 'worker'")

MAIN_BOT_TOKEN = _req("MAIN_BOT_TOKEN")
OWNER_ID = int(_req("OWNER_ID"))
MONGO_URI = _req("MONGO_URI")
DB_NAME = os.getenv("DB_NAME", "filebot")
FERNET_KEY = _req("FERNET_KEY")
WEBHOOK_SECRET = _req("WEBHOOK_SECRET")

BASE_URL = _req("BASE_URL").rstrip("/")
LINK_BASE_URL = os.getenv("LINK_BASE_URL", BASE_URL).rstrip("/")

PRIMARY_CHANNEL = int(_req("PRIMARY_CHANNEL"))
BACKUP_CHANNEL = int(_req("BACKUP_CHANNEL"))
CHANNELS = [PRIMARY_CHANNEL, BACKUP_CHANNEL]

HEALTH_INTERVAL = int(os.getenv("HEALTH_INTERVAL", "300"))
SYNC_INTERVAL = int(os.getenv("SYNC_INTERVAL", "60"))

# ---- optional buttons on the bot menu ----
CHANNEL_URL = os.getenv("CHANNEL_URL", "").strip()
SUPPORT_URL = os.getenv("SUPPORT_URL", "").strip()

# ---- ad verification (worker) ----
VERIFY_REQUIRED = _bool("VERIFY_REQUIRED", True)
ADS_REQUIRED = int(os.getenv("ADS_REQUIRED", "1"))
MIN_AD_SECONDS = int(os.getenv("MIN_AD_SECONDS", "5"))
SESSION_TTL = int(os.getenv("SESSION_TTL", "1800"))

# Delivered file itne second baad user ki chat se auto-delete (0 = band)
AUTO_DELETE_SECONDS = int(os.getenv("AUTO_DELETE_SECONDS", "120"))

# ---- direct Mini App launch ----
# False (default) = link -> bot -> 'Verify & Get Video' button -> Mini App me turant ad.
# True = link seedha Mini App kholta hai (button nahi).
# Iske liye har delivery bot me BotFather -> Bot Settings -> Main Mini App set hona chahiye
# (URL: BASE_URL/app). Agar short name wala app bana hai (/newapp) to uska short name yahan daalo.
DIRECT_MINIAPP = _bool("DIRECT_MINIAPP", False)
MINIAPP_SHORT_NAME = os.getenv("MINIAPP_SHORT_NAME", "").strip()

# ---- Monetag (Rewarded Interstitial, Telegram Mini App SDK) ----
# Monetag dashboard -> Telegram Mini Apps -> apna zone ID
MONETAG_ZONE_ID = os.getenv("MONETAG_ZONE_ID", "12181474").strip()
MONETAG_SDK_URL = os.getenv("MONETAG_SDK_URL", "https://libtl.com/sdk.js").strip()
# True = Mini App me chhota event log + asli error dikhao (sirf test ke liye)
MONETAG_SHOW_LOG = _bool("MONETAG_SHOW_LOG", False)

# ---- Create Post (manager bot) ----
# Post ka default caption (image ke niche). \n se nayi line. Owner photo ke saath caption likhe to wahi use hota hai.
POST_CAPTION = os.getenv(
    "POST_CAPTION",
    "🎬 Watch this video for FREE!\n\n▶️ Tap the button below to play instantly",
).replace("\\n", "\n")
POST_BUTTON_TEXT = os.getenv("POST_BUTTON_TEXT", "👉 Play Online Free").strip()[:60] or "Play"
# Optional: channel jaha bot post seedha daal de (bot ko us channel ka admin banao). Khali = band.
_pc = os.getenv("POST_CHANNEL", "").strip()
POST_CHANNEL = (int(_pc) if _pc.lstrip("-").isdigit() else _pc) if _pc else None
POST_MAX_VIDEOS = int(os.getenv("POST_MAX_VIDEOS", "10"))
# "Aaj" ka din kis timezone me ginna hai (minutes, UTC se). India = 330
STATS_TZ_OFFSET_MIN = int(os.getenv("STATS_TZ_OFFSET_MIN", "330"))

# ---- Bulk Posts (manager bot): channel ki range scan karke posts banana ----
BULK_MAX_RANGE = int(os.getenv("BULK_MAX_RANGE", "3000"))        # ek baar me max kitne message ids
BULK_SCAN_DELAY = float(os.getenv("BULK_SCAN_DELAY", "0.4"))     # har message scan ke beech (sec)
BULK_POST_DELAY = float(os.getenv("BULK_POST_DELAY", "3"))       # channel me 2 posts ke beech (sec)

# True = post ka button seedha t.me/<bot> link kholta hai (Telegram ka "Open Link" confirm box nahi aata).
# Har post par random active bot chuna jata hai. False = purana /f/ redirect link (confirm box aayega).
POST_DIRECT_LINK = _bool("POST_DIRECT_LINK", True)

# ---- Copy Chats / Userbot (Telethon): apne Telegram account se chats ka media channel me copy ----
# my.telegram.org -> API development tools -> api_id + api_hash. Dono khali = feature band.
TG_API_ID = int(os.getenv("TG_API_ID", "0").strip() or 0)
TG_API_HASH = os.getenv("TG_API_HASH", "").strip()
UB_BATCH = int(os.getenv("UB_BATCH", "20"))        # ek forward call me max kitne messages
UB_DELAY = float(os.getenv("UB_DELAY", "3"))       # 2 forward calls ke beech (sec). Kam mat karo, account limit ho sakta hai


# ======================================================================
# ---- Monetag Smart Monetization Engine (sab kuch OPTIONAL / backward-compatible) ----
# Default values = purana behaviour. Kuch bhi set na karo to app pehle jaisa hi chalta hai.
# ======================================================================
def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)).strip())
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except ValueError:
        return default


def _parse_zones(raw: str) -> dict:
    """'12181474,98765|https://x.com/sdk.js' -> {'12181474': <MONETAG_SDK_URL>, '98765': 'https://x.com/sdk.js'}"""
    out: dict = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        zone, _, url = part.partition("|")
        zone = zone.strip()
        if not zone.isdigit():
            raise RuntimeError(f"MONETAG_ZONES: '{zone}' is not a numeric zone ID")
        out[zone] = url.strip() or MONETAG_SDK_URL
    return out


# Sirf MAIN SDK zone IDs (Rewarded Interstitial). Format: zone[|sdk_url],zone[|sdk_url]
# Khali = sirf MONETAG_ZONE_ID. Sub-zones yahan mat daalo (Monetag docs: SDK me sirf main zone).
MONETAG_ZONES = _parse_zones(os.getenv("MONETAG_ZONES", ""))
MONETAG_DEFAULT_ZONE = os.getenv("MONETAG_DEFAULT_ZONE", MONETAG_ZONE_ID).strip() or MONETAG_ZONE_ID
MONETAG_ZONES.setdefault(MONETAG_ZONE_ID, MONETAG_SDK_URL)
MONETAG_ZONES.setdefault(MONETAG_DEFAULT_ZONE, MONETAG_SDK_URL)
if not MONETAG_DEFAULT_ZONE.isdigit():
    raise RuntimeError("MONETAG_DEFAULT_ZONE must be a numeric zone ID")

# True = historical eCPM ke hisaab se traffic zones me baantna. False (default) = hamesha default zone.
MONETAG_OPTIMIZE = _bool("MONETAG_OPTIMIZE", False)

# ---- server-side verification ----
# Monetag SSP -> main zone -> Postback URL:
#   https://<BASE_URL>/api/monetag/postback?secret=<MONETAG_POSTBACK_SECRET>&ymid={ymid}&event_type={event_type}&reward_event_type={reward_event_type}&zone_id={zone_id}&sub_zone_id={sub_zone_id}&estimated_price={estimated_price}&request_var={request_var}&telegram_id={telegram_id}
MONETAG_POSTBACK_SECRET = os.getenv("MONETAG_POSTBACK_SECRET", "").strip()
# legacy   = purana flow (SDK promise resolve + MIN_AD_SECONDS). Postback sirf analytics ke liye.
# postback = file tabhi milegi jab Monetag ka 'valued' impression postback is ad ke ymid ka aa chuka ho.
AD_VERIFY_MODE = os.getenv("AD_VERIFY_MODE", "legacy").strip().lower()
if AD_VERIFY_MODE not in ("legacy", "postback"):
    raise RuntimeError("AD_VERIFY_MODE must be 'legacy' or 'postback'")
if AD_VERIFY_MODE == "postback" and not MONETAG_POSTBACK_SECRET:
    raise RuntimeError("AD_VERIFY_MODE=postback needs MONETAG_POSTBACK_SECRET")
POSTBACK_WAIT_SECONDS = _int("POSTBACK_WAIT_SECONDS", 20)
# SDK ne reward_event_type='non_valued' diya (unpaid/fraud/fallback) to file mat do, dobara ad dikhao
BLOCK_NON_VALUED = _bool("BLOCK_NON_VALUED", True)

# ---- optimizer tuning (conservative defaults for 1k-5k impressions/day) ----
MONETAG_MIN_IMPRESSIONS = _int("MONETAG_MIN_IMPRESSIONS", 300)    # har zone ke itne impressions (window me) ke baad hi compare
MONETAG_WINDOW_DAYS = max(1, _int("MONETAG_WINDOW_DAYS", 7))      # rolling window
MONETAG_EXPLORE_SHARE = min(0.5, max(0.0, _float("MONETAG_EXPLORE_SHARE", 0.2)))  # leader ke alawa zones ko kul traffic
MONETAG_MIN_SHARE = min(0.2, max(0.0, _float("MONETAG_MIN_SHARE", 0.05)))         # har live zone ko minimum share
MONETAG_MIN_SWITCH_HOURS = _float("MONETAG_MIN_SWITCH_HOURS", 24)  # 2 leader-switch ke beech kam se kam
MONETAG_Z_SCORE = _float("MONETAG_Z_SCORE", 1.28)                 # ~90% confidence; badhao = aur conservative
MONETAG_OPT_INTERVAL = max(60, _int("MONETAG_OPT_INTERVAL", 900))  # optimizer kitni der baad chale (sec)
# auto-bench: zone ke ads lagatar fail ho rahe ho to thodi der ke liye band
MONETAG_BENCH_FAIL_RATE = _float("MONETAG_BENCH_FAIL_RATE", 0.6)
MONETAG_BENCH_MIN_ATTEMPTS = _int("MONETAG_BENCH_MIN_ATTEMPTS", 20)
MONETAG_BENCH_MINUTES = _int("MONETAG_BENCH_MINUTES", 30)

# ---- analytics access ----
# GET /admin/monetag (Authorization: Bearer <token>). Khali = HTTP endpoint band (bot panel phir bhi chalta hai).
MONETAG_ADMIN_TOKEN = os.getenv("MONETAG_ADMIN_TOKEN", "").strip()
# Optional: proxy ka country header (e.g. CF-IPCountry). Khali = country-wise stats band.
MONETAG_GEO_HEADER = os.getenv("MONETAG_GEO_HEADER", "").strip()
