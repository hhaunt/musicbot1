import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
SEARCH_SOURCE = os.getenv("SEARCH_SOURCE", "scsearch").strip() or "scsearch"
BRAND = os.getenv("BRAND", "").strip() or "MusicCloud"   # название на карточках

# Мини-приложение: публичный HTTPS-адрес и порт встроенного веб-сервера.
# Пока WEBAPP_URL пуст, мини-приложение выключено.
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip().rstrip("/")
if WEBAPP_URL.startswith("http://"):
    WEBAPP_URL = "https://" + WEBAPP_URL[len("http://"):]
elif WEBAPP_URL and not WEBAPP_URL.startswith("https://"):
    WEBAPP_URL = "https://" + WEBAPP_URL  # Telegram принимает только HTTPS-ссылки
WEBAPP_PORT = int(os.getenv("WEBAPP_PORT") or os.getenv("PORT") or 8080)

# Где хранить базу. По умолчанию — рядом с кодом, но при пересборке из GitHub эта папка
# создаётся заново и база пропадает. Укажите DATA_DIR на постоянный диск хостинга (Volume),
# например DATA_DIR=/data, — тогда лайки, плейлисты и статистика переживут обновления.
DATA_DIR = Path(os.getenv("DATA_DIR") or BASE_DIR)
DB_PATH = DATA_DIR / "mono.db"
TMP_DIR = BASE_DIR / "tmp"
CACHE_DIR = BASE_DIR / "cache"     # аудио для мини-приложения
CACHE_FILES = 40                   # сколько треков держать в кэше
CACHE_MAX_MB = int(os.getenv("CACHE_MAX_MB") or 120)   # и сколько места он может занять
MAX_DOWNLOADS = 2                  # одновременных загрузок — чтобы не упереться в память

# Кому доступна команда /stats: id через запятую, например ADMIN_IDS=123456,789012
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()}

# Как часто проверять новые релизы исполнителей, на которых подписаны слушатели
RELEASE_CHECK_HOURS = float(os.getenv("RELEASE_CHECK_HOURS") or 6)

# Лента «Новые релизы»: по чартам каких стран искать популярных исполнителей
# (двухбуквенные коды стран) и за сколько дней релиз считается новым
def _codes(name: str, default: str) -> list[str]:
    return [c.strip().lower() for c in (os.getenv(name) or default).split(",") if c.strip()]


RELEASE_COUNTRIES = _codes("RELEASE_COUNTRIES", "ru,kz,by,ua,uz")   # СНГ
RELEASE_WORLD = _codes("RELEASE_WORLD", "us,gb")                    # зарубежные
RELEASE_FRESH_DAYS = int(os.getenv("RELEASE_FRESH_DAYS") or 60)

# Показывать ли новых пользователей другим в разделе «Люди» без их согласия.
# По умолчанию нет: каждый сам включает «Открытый профиль».
PROFILES_PUBLIC_DEFAULT = os.getenv("PROFILES_PUBLIC_DEFAULT", "").strip() == "1"

PAGE_SIZE = 8                     # треков на одной карточке
MAX_FILE_SIZE = 49 * 1024 * 1024   # лимит Telegram для ботов — 50 МБ
MAX_PLAYLISTS = 20
