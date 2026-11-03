import os


def _req(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing environment variable: {name}")
    return value


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


# main   = manager bot (admin commands, uploads, health check)
# worker = secondary delivery bots + Mini App (ad verification)
ROLE = os.getenv("ROLE", "main").strip().lower()
if ROLE not in ("main", "worker"):
    raise RuntimeError("ROLE must be 'main' or 'worker'")

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

# ---- ad verification (worker) ----
VERIFY_REQUIRED = _bool("VERIFY_REQUIRED", True)
ADS_REQUIRED = int(os.getenv("ADS_REQUIRED", "1"))
MIN_AD_SECONDS = int(os.getenv("MIN_AD_SECONDS", "5"))
SESSION_TTL = int(os.getenv("SESSION_TTL", "1800"))

# ---- direct Mini App launch ----
# True = permanent link seedha Mini App kholta hai (button dabane ki zaroorat nahi).
# Iske liye har delivery bot me BotFather -> Bot Settings -> Main Mini App set hona chahiye
# (URL: BASE_URL/app). Agar short name wala app bana hai (/newapp) to uska short name yahan daalo.
DIRECT_MINIAPP = _bool("DIRECT_MINIAPP", True)
MINIAPP_SHORT_NAME = os.getenv("MINIAPP_SHORT_NAME", "").strip()

# ---- Monetag ----
# MAIN zone ID (sub-zone nahi), Monetag dashboard -> Get SDK
MONETAG_ZONE_ID = os.getenv("MONETAG_ZONE_ID", "").strip()
# Dashboard ke "Get instructions" snippet me jo <script src="..."> hai, wahi daalo
MONETAG_SDK_URL = os.getenv("MONETAG_SDK_URL", "https://libtl.com/sdk.js").strip()
# True = reward sirf tab jab Monetag 'valued' bheje (user ne ad poora dekha)
MONETAG_REQUIRE_VALUED = _bool("MONETAG_REQUIRE_VALUED", True)

if ROLE == "worker" and VERIFY_REQUIRED and not MONETAG_ZONE_ID:
    raise RuntimeError(
        "VERIFY_REQUIRED=true hai par MONETAG_ZONE_ID set nahi hai. "
        "Ya to zone ID set karo ya VERIFY_REQUIRED=false karo."
    )
