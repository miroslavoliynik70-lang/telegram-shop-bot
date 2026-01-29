import os

BOT_TOKEN = os.getenv("BOT_TOKEN")

_raw = os.getenv("5370573727", "6180039889")
ADMIN_IDS = {int(x) for x in _raw.split(",") if x.strip().isdigit()}

CURRENCY = "RUB"
