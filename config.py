import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

# --- Telegram ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# --- Timezone ---
TZ = ZoneInfo("Europe/Kyiv")

# --- Data source ---
API_URL = os.getenv("API_URL", "https://svitlo-proxy.svitlo-proxy.workers.dev")

# Start with one region, later we'll scale to all Ukraine
DEFAULT_REGION = os.getenv("DEFAULT_REGION", "dnipropetrovska-oblast")

# --- Storage ---
DB_PATH = os.getenv("DB_PATH", "bot.db")

# --- Caching / scheduling ---
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "600"))  # 10 minutes
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "10"))
NOTIFY_BEFORE_MINUTES = int(os.getenv("NOTIFY_BEFORE_MINUTES", "15"))



