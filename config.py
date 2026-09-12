"""Environment loading and constants."""
import os

from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
EXA_API_KEY = os.getenv("EXA_API_KEY", "")
AMBIGUOUS_API_KEY = os.getenv("AMBIGUOUS_API_KEY", "")

DB_PATH = os.getenv("DETOUR_DB", "detour.db")

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_MODEL = "openai/gpt-4o-mini"

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
# Nominatim blocks requests without a descriptive User-Agent.
USER_AGENT = "Detour/1.0 (hackathon errand bot)"

AMBIGUOUS_BASE_URL = os.getenv("AMBIGUOUS_BASE_URL", "https://api.ambiguous.com")

LOCAL_TZ = "Asia/Hong_Kong"

# How far around the user to look for a shop when they name a category, not a shop.
NEARBY_RADIUS_M = 2000
NO_CALENDAR_MINUTES = 1440

TRAVEL_MODES = ("walk", "mtr", "taxi")
MODE_LABELS = {"walk": "walking", "mtr": "MTR", "taxi": "taxi"}

# Checked before any network geocode so a flaky connection can't break the demo.
DEMO_PLACES = {
    "central cleaners": {
        "name": "Central Cleaners",
        "address": "12 Queen's Road Central, Central, Hong Kong",
        "lat": 22.28190,
        "lng": 114.15760,
    },
    "watsons central": {
        "name": "Watsons",
        "address": "Man Yee Building, 68 Des Voeux Road Central, Hong Kong",
        "lat": 22.28245,
        "lng": 114.15680,
    },
    "hong kong central library": {
        "name": "Hong Kong Central Library",
        "address": "66 Causeway Road, Causeway Bay, Hong Kong",
        "lat": 22.27760,
        "lng": 114.19100,
    },
    "keypro repair": {
        "name": "KeyPro Repair",
        "address": "Sai Yeung Choi Street South, Mong Kok, Hong Kong",
        "lat": 22.31930,
        "lng": 114.17020,
    },
    "cyberport": {
        "name": "Cyberport",
        "address": "100 Cyberport Road, Pok Fu Lam, Hong Kong",
        "lat": 22.26060,
        "lng": 114.13010,
    },
}
