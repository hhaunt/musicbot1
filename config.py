import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
SEARCH_SOURCE = os.getenv("SEARCH_SOURCE", "scsearch").strip() or "scsearch"

DB_PATH = BASE_DIR / "mono.db"
TMP_DIR = BASE_DIR / "tmp"

PAGE_SIZE = 8                      # треков на одной карточке
MAX_FILE_SIZE = 49 * 1024 * 1024   # лимит Telegram для ботов — 50 МБ
MAX_PLAYLISTS = 20
