import os

BOT_TOKEN = os.getenv("8573209901:AAHhgoWdYpMlh3Wf2h-Qm5V4qVp9eTJegME")

_raw = os.getenv("5370573727", "6180039889")
ADMIN_IDS = {int(x) for x in _raw.split(",") if x.strip().isdigit()}

