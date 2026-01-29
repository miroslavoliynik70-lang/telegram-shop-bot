import os

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
_raw = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = {int(x) for x in _raw.split(",") if x.strip().isdigit()}
CURRENCY = os.getenv("CURRENCY", "EUR")
