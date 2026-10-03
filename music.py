"""Поиск и загрузка аудио через yt-dlp, альбомы — через открытый API Deezer."""
import asyncio
import random
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from itertools import zip_longest
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from yt_dlp import YoutubeDL

from config import (MAX_DOWNLOADS, MAX_FILE_SIZE, RELEASE_COUNTRIES, RELEASE_FRESH_DAYS,
                    RELEASE_WORLD, SEARCH_SOURCE, SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET,
                    TMP_DIR, YANDEX_MUSIC_TOKEN)

DEEZER = "https://api.deezer.com"
# Трек альбома не имеет ссылки: при загрузке он ищется по «исполнитель - название»
SEARCH_PREFIX = "search:"

_TIMEOUT = aiohttp.ClientTimeout(total=10)
_cover_cache: dict[str, bytes] = {}


class TrackProtected(RuntimeError):
    """Трек защищён DRM — источник не отдаёт его для загрузки."""


@dataclass
class Found:
    url: str
    title: str
    artist: str
    duration: int
    cover: str | None = None
    genre: str | None = None
    plays: int | None = None      # прослушивания на площадке-источнике, если она их сообщает
    likes: int | None = None      # лайки там же
    released: str | None = None   # дата выхода, ГГГГ-ММ-ДД


@dataclass
class Album:
    id: int
    title: str
    artist: str
    cover: str | None
    count: int = 0
    year: str = ""
    tracks: list[Found] = field(default_factory=list)
    tag: str = ""  # для ленты релизов: «СНГ» или «мир»


def _thumbnail(e: dict) -> str | None:
    thumbs = [t for t in e.get("thumbnails") or [] if t.get("url")]
    for t in thumbs:
        if t.get("id") == "t300x300":
            return t["url"]
    if thumbs:
        return thumbs[-1]["url"]
    return e.get("thumbnail")


def _search(query: str, limit: int) -> list[Found]:
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True}
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"{SEARCH_SOURCE}{limit}:{query}", download=False)
    return [f for f in map(_entry, (info or {}).get("entries") or []) if f]


def _entry(e: dict | None) -> Found | None:
    """Запись yt-dlp из поиска или плейлиста → трек; None, если в ней нет ссылки или названия."""
    if not e:
        return None
    url = e.get("webpage_url") or e.get("url")
    title = e.get("title")
    if not url or not title or not str(url).startswith("http"):
        return None
    artist = e.get("uploader") or e.get("channel") or e.get("artist") or ""
    genre = e.get("genre") or next(iter(e.get("genres") or []), None)
    released = None
    if e.get("timestamp"):
        released = date.fromtimestamp(int(e["timestamp"])).isoformat()
    elif re.fullmatch(r"\d{8}", str(e.get("upload_date") or "")):
        d = e["upload_date"]
        released = f"{d[:4]}-{d[4:6]}-{d[6:]}"
    count = lambda k: int(e[k]) if isinstance(e.get(k), (int, float)) else None  # noqa: E731
    return Found(url, title.strip(), artist.strip(), int(e.get("duration") or 0),
                 _thumbnail(e), (genre or "").strip()[:40] or None,
                 count("view_count"), count("like_count"), released)


# ── импорт плейлистов по ссылке ──────────────────────────────────────────

# Только известные музыкальные площадки: произвольный адрес позволил бы заставить
# сервер ходить куда угодно, в том числе во внутреннюю сеть хостинга.
IMPORT_HOSTS = ("youtube.com", "youtu.be", "soundcloud.com", "bandcamp.com", "deezer.com")
IMPORT_LIMIT = 100


class ImportError_(RuntimeError):
    """Понятная пользователю причина, по которой плейлист не импортирован."""


def _extract_playlist(url: str) -> tuple[str, list[Found]]:
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True,
            "playlistend": IMPORT_LIMIT}
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False) or {}
    entries = info.get("entries")
    if entries is None:  # ссылка на один трек, а не на плейлист
        entries = [info]
    tracks = [f for f in map(_entry, list(entries)[:IMPORT_LIMIT]) if f]
    return (info.get("title") or "Импорт").strip(), tracks


def _sc_found(t: dict) -> Found | None:
    """Трек из API SoundCloud → Found."""
    url, title = t.get("permalink_url"), t.get("title")
    if not url or not title:
        return None
    user = t.get("user") or {}
    art = (t.get("artwork_url") or user.get("avatar_url") or "").replace("-large.", "-t300x300.") or None
    released = (t.get("release_date") or t.get("display_date") or t.get("created_at") or "")[:10] or None
    return Found(url, title.strip(), (user.get("username") or "").strip(),
                 int((t.get("full_duration") or t.get("duration") or 0) / 1000), art,
                 (t.get("genre") or "").strip()[:40] or None,
                 t.get("playback_count"), t.get("likes_count"), released)


async def _soundcloud_playlist(url: str) -> tuple[str, list[Found]] | None:
    """Плейлист SoundCloud через его открытый веб-API. Полностью SoundCloud отдаёт только первые
    5 треков, остальные — одними номерами; их дозапрашиваем пачками по 50.
    None — ссылка ведёт не на плейлист (например, на профиль), пусть разбирается yt-dlp."""
    global _sc_client_id
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30),
                                     headers={"User-Agent": "Mozilla/5.0 MusicCloudBot"}) as s:
        client_id = await _soundcloud_client_id(s)
        if not client_id:
            return None
        async with s.get("https://api-v2.soundcloud.com/resolve",
                         params={"url": url, "client_id": client_id}) as r:
            if r.status == 404:
                raise ImportError_("Плейлист не найден — возможно, он закрыт или удалён")
            if r.status in (401, 403):
                _sc_client_id = None
                return None
            data = await r.json(content_type=None) or {}
        if data.get("kind") == "track":
            items, title = [data], data.get("title")
        elif isinstance(data.get("tracks"), list):
            items, title = data["tracks"][:IMPORT_LIMIT], data.get("title")
        else:
            return None
        full = {t["id"]: t for t in items if t.get("id") and t.get("title")}
        missing = [t["id"] for t in items if t.get("id") and not t.get("title")]
        for i in range(0, len(missing), 50):
            async with s.get("https://api-v2.soundcloud.com/tracks",
                             params={"ids": ",".join(map(str, missing[i:i + 50])),
                                     "client_id": client_id}) as r:
                for t in (await r.json(content_type=None) if r.status == 200 else None) or []:
                    full[t.get("id")] = t
    tracks = [f for f in (_sc_found(full[t["id"]]) for t in items if t.get("id") in full) if f]
    return (title or "Импорт").strip(), tracks


async def import_playlist(url: str) -> tuple[str, list[Found]]:
    """(название, треки) плейлиста по ссылке с YouTube, SoundCloud, Bandcamp или Deezer."""
    parts = urlparse(url.strip())
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or \
            not any(host == h or host.endswith("." + h) for h in IMPORT_HOSTS):
        raise ImportError_("Поддерживаются ссылки YouTube, SoundCloud, Bandcamp и Deezer")
    if host == "on.soundcloud.com":  # короткая ссылка из приложения SoundCloud — раскрываем её
        try:
            async with aiohttp.ClientSession(timeout=_TIMEOUT) as s, \
                    s.get(url.strip(), allow_redirects=True) as r:
                url = str(r.url)
        except aiohttp.ClientError as e:
            raise ImportError_("Не удалось открыть ссылку SoundCloud") from e
        host = (urlparse(url).hostname or "").lower()
        if not (host == "soundcloud.com" or host.endswith(".soundcloud.com")):
            raise ImportError_("Короткая ссылка ведёт не на SoundCloud")
    sc_result = None
    if host == "soundcloud.com" or host.endswith(".soundcloud.com"):
        try:
            sc_result = await _soundcloud_playlist(url.strip())
        except ImportError_:
            raise
        except Exception:
            sc_result = None  # не вышло через API SoundCloud — пробуем как раньше, через yt-dlp
    if sc_result:
        title, tracks = sc_result
    elif host.endswith("deezer.com"):
        found = re.search(r"/playlist/(\d+)", parts.path)
        if not found:
            raise ImportError_("Нужна ссылка именно на плейлист Deezer")
        data = await _deezer(f"playlist/{found.group(1)}")
        tracks = [_deezer_track(x) for x in (data.get("tracks") or {}).get("data") or []]
        title = data.get("title") or "Импорт"
    else:
        try:
            title, tracks = await asyncio.to_thread(_extract_playlist, url.strip())
        except Exception as e:
            raise ImportError_("Не удалось прочитать плейлист — возможно, он закрыт") from e
    if not tracks:
        raise ImportError_("В плейлисте не нашлось доступных треков — возможно, он закрыт "
                           "или ссылка ведёт не на плейлист")
    return title[:40], tracks[:IMPORT_LIMIT]


# Форматы, которые без перекодирования играют и Telegram, и браузеры. FLAC оставляем как есть:
# перекодировать его в mp3 значит потерять качество, ради которого его и выбирают.
PLAYABLE = {"mp3", "m4a", "flac"}


def _fetch(url: str) -> Path:
    """Скачивает один трек по ссылке. TrackProtected — если площадка закрыла его DRM."""
    TMP_DIR.mkdir(exist_ok=True)
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "max_filesize": MAX_FILE_SIZE,
        "outtmpl": str(TMP_DIR / "%(id)s.%(ext)s"),
        # сначала без потерь, затем то, что играет везде, и уже потом что угодно
        "format": "bestaudio[acodec=flac]/bestaudio[ext=mp3]/bestaudio[ext=m4a]/bestaudio/best",
    }
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as e:
        if "DRM" in str(e):
            raise TrackProtected("трек защищён правообладателем") from e
        raise
    downloads = (info or {}).get("requested_downloads") or []
    path = Path(downloads[0]["filepath"]) if downloads else None
    if not path or not path.exists():
        raise RuntimeError("файл не загружен (возможно, он больше 50 МБ)")
    if path.suffix.lstrip(".").lower() not in PLAYABLE and shutil.which("ffmpeg"):
        path = _to_mp3(path)  # opus, webm и т. п. — в mp3, чтобы играло на iPhone
    if path.stat().st_size > MAX_FILE_SIZE:
        path.unlink(missing_ok=True)
        raise RuntimeError("файл больше 50 МБ")
    return path


def _to_mp3(src: Path) -> Path:
    dst = src.with_suffix(".mp3")
    result = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-vn",
                             "-b:a", "192k", str(dst)], capture_output=True, timeout=300)
    if result.returncode != 0 or not dst.exists():
        return src  # не вышло — отдадим как есть
    src.unlink(missing_ok=True)
    return dst


def _download(url: str, hint: str = "") -> Path:
    """Скачивает трек. Если он закрыт DRM или недоступен, ищет тот же трек в другой загрузке
    («исполнитель - название») — в основном источнике, а затем на YouTube."""
    query = url[len(SEARCH_PREFIX):] if url.startswith(SEARCH_PREFIX) else hint
    candidates = [] if url.startswith(SEARCH_PREFIX) else [url]
    sources = [SEARCH_SOURCE] + (["ytsearch"] if SEARCH_SOURCE != "ytsearch" else [])
    last_error: Exception = RuntimeError("трек не найден")
    tried: set[str] = set()

    def attempt(link: str) -> Path | None:
        nonlocal last_error
        if link in tried:
            return None
        tried.add(link)
        try:
            return _fetch(link)
        except Exception as e:
            last_error = e
            return None

    for link in candidates:
        if (path := attempt(link)):
            return path
    if query:
        for source in sources:
            try:
                with YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": True,
                                "skip_download": True}) as ydl:
                    info = ydl.extract_info(f"{source}5:{query}", download=False) or {}
            except Exception:
                continue
            for f in map(_entry, info.get("entries") or []):
                if f and (path := attempt(f.url)):
                    return path
    raise last_error


async def search(query: str, limit: int = 8) -> list[Found]:
    return await asyncio.to_thread(_search, query, limit)


_downloads = asyncio.Semaphore(MAX_DOWNLOADS)


async def download(url: str, hint: str = "") -> Path:
    """hint — «исполнитель - название», чтобы найти замену, если по ссылке трек недоступен."""
    async with _downloads:
        return await asyncio.to_thread(_download, url, hint)


# ── обложки ──────────────────────────────────────────────────────────────

async def fetch_cover(url: str | None) -> bytes | None:
    if not url:
        return None
    if url in _cover_cache:
        return _cover_cache[url]
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as s, s.get(url) as r:
            if r.status != 200:
                return None
            data = await r.read()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None
    if len(_cover_cache) > 300:
        _cover_cache.clear()
    _cover_cache[url] = data
    return data


async def fetch_covers(urls: list[str | None]) -> list[bytes | None]:
    return list(await asyncio.gather(*(fetch_cover(u) for u in urls)))


# ── тексты песен (LRCLIB) ────────────────────────────────────────────────

LRCLIB = "https://lrclib.net/api/search"
_JUNK = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")  # «(Official Video)», «[HD]» и т. п.


async def lyrics(title: str, artist: str, duration: int) -> dict:
    """Текст трека из открытой базы LRCLIB: {'synced': LRC с таймкодами, 'plain': обычный}."""
    if " - " in title:  # на SoundCloud исполнитель часто записан в названии
        artist, title = (x.strip() for x in title.split(" - ", 1))
    title = _JUNK.sub("", title).strip() or title
    found: list = []
    async with aiohttp.ClientSession(timeout=_TIMEOUT,
                                     headers={"User-Agent": "MusicCloudBot"}) as s:
        for params in ({"track_name": title, "artist_name": artist},
                       {"q": f"{artist} {title}".strip()}):
            async with s.get(LRCLIB, params=params) as r:
                data = await r.json(content_type=None) if r.status == 200 else []
            found = [x for x in data if isinstance(x, dict)
                     and (x.get("syncedLyrics") or x.get("plainLyrics"))] \
                if isinstance(data, list) else []
            if found:
                break
    if not found:
        return {"synced": None, "plain": None}
    # сначала версии с таймкодами, среди них — ближайшая по длительности
    best = min(found, key=lambda x: (not x.get("syncedLyrics"),
                                     abs((x.get("duration") or 0) - duration) if duration else 0))
    return {"synced": best.get("syncedLyrics") or None, "plain": best.get("plainLyrics") or None}


# ── альбомы (Deezer) ─────────────────────────────────────────────────────

async def _deezer(path: str, **params) -> dict:
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as s, \
            s.get(f"{DEEZER}/{path}", params=params) as r:
        data = await r.json(content_type=None)
    if not isinstance(data, dict) or data.get("error"):
        raise RuntimeError(f"Deezer: {data.get('error') if isinstance(data, dict) else data}")
    return data


def _deezer_track(x: dict) -> Found:
    who = (x.get("artist") or {}).get("name") or ""
    title = x.get("title") or ""
    cover = (x.get("album") or {}).get("cover_medium") or None
    # у Deezer нет числа прослушиваний, только рейтинг популярности — его не показываем
    return Found(f"{SEARCH_PREFIX}{who} - {title}", title, who, int(x.get("duration") or 0), cover,
                 released=(x.get("release_date") or None))


async def chart(limit: int = 30) -> list[Found]:
    """Популярные треки — подборка для тех, о чьём вкусе ещё ничего не известно."""
    data = await _deezer("chart/0/tracks", limit=limit)
    return [_deezer_track(x) for x in data.get("data") or []]


async def _artist_picks(name: str) -> list[Found]:
    """Лучшие треки исполнителя и нескольких похожих на него."""
    found = (await _deezer("search/artist", q=name, limit=1)).get("data") or []
    if not found:
        return []
    related = (await _deezer(f"artist/{found[0]['id']}/related", limit=8)).get("data") or []
    ids = [found[0]["id"]] + [r["id"] for r in random.sample(related, min(3, len(related)))]
    tops = await asyncio.gather(*(_deezer(f"artist/{i}/top", limit=4) for i in ids),
                                return_exceptions=True)
    return [_deezer_track(x) for t in tops if isinstance(t, dict) for x in t.get("data") or []]


async def _deezer_artist(name: str) -> dict | None:
    """Исполнитель в каталоге Deezer — только при точном совпадении имени."""
    found = (await _deezer("search/artist", q=name, limit=1)).get("data") or []
    if found and (found[0].get("name") or "").casefold() == name.casefold():
        return found[0]
    return None


async def artist_top(name: str, limit: int = 5) -> list[Found]:
    """Лучшие треки самого исполнителя; если Deezer его не знает — ищем в основном источнике."""
    found = await _deezer_artist(name)
    if found:
        top = (await _deezer(f"artist/{found['id']}/top", limit=limit)).get("data") or []
        if top:
            return [_deezer_track(x) for x in top]
    return await search(name, limit)


async def search_artists(query: str, limit: int = 8) -> list[dict]:
    """Исполнители для общего поиска: имя, фото и подписчики на Deezer."""
    data = (await _deezer("search/artist", q=query, limit=limit)).get("data") or []
    return [{"name": a.get("name") or "", "picture": a.get("picture_medium") or None,
             "fans": a.get("nb_fan")} for a in data if a.get("name")]


# ── подписчики исполнителя на разных площадках ───────────────────────────
# Везде берём только исполнителя с точно таким же именем — иначе покажем чужие цифры.

_sc_client_id: str | None = None


async def _soundcloud_client_id(s: aiohttp.ClientSession) -> str | None:
    """Открытый ключ веб-версии SoundCloud: его же сайт подставляет в свои запросы."""
    global _sc_client_id
    if _sc_client_id:
        return _sc_client_id
    async with s.get("https://soundcloud.com/") as r:
        page = await r.text()
    scripts = re.findall(r'<script crossorigin src="(https://a-v2\.sndcdn\.com/assets/[^"]+\.js)"', page)
    for src in reversed(scripts):
        async with s.get(src) as r:
            found = re.search(r'client_id\s*:\s*"([0-9a-zA-Z]{32})"', await r.text())
        if found:
            _sc_client_id = found.group(1)
            return _sc_client_id
    return None


async def _soundcloud_followers(s: aiohttp.ClientSession, name: str) -> int | None:
    global _sc_client_id
    client_id = await _soundcloud_client_id(s)
    if not client_id:
        return None
    async with s.get("https://api-v2.soundcloud.com/search/users",
                     params={"q": name, "limit": 10, "client_id": client_id}) as r:
        if r.status in (401, 403):  # ключ устарел — в следующий раз найдём новый
            _sc_client_id = None
            return None
        data = await r.json(content_type=None)
    counts = [u.get("followers_count") or 0 for u in (data or {}).get("collection") or []
              if (u.get("username") or "").casefold() == name.casefold()]
    return max(counts) if counts else None


async def _yandex_followers(s: aiohttp.ClientSession, name: str) -> int | None:
    """Сколько людей добавили исполнителя в «Мне нравится» на Яндекс Музыке."""
    headers = {"Authorization": f"OAuth {YANDEX_MUSIC_TOKEN}"} if YANDEX_MUSIC_TOKEN else {}
    async with s.get("https://api.music.yandex.net/search", headers=headers,
                     params={"text": name, "type": "artist", "page": 0}) as r:
        data = await r.json(content_type=None) if r.status == 200 else {}
    results = (((data or {}).get("result") or {}).get("artists") or {}).get("results") or []
    match = next((a for a in results if (a.get("name") or "").casefold() == name.casefold()), None)
    if not match:
        return None
    async with s.get(f"https://api.music.yandex.net/artists/{match['id']}/brief-info",
                     headers=headers) as r:
        info = await r.json(content_type=None) if r.status == 200 else {}
    return (((info or {}).get("result") or {}).get("artist") or {}).get("likesCount")


_spotify_token: tuple[float, str | None] = (0.0, None)


async def _spotify_followers(s: aiohttp.ClientSession, name: str) -> int | None:
    """Spotify отдаёт данные только приложениям с ключами — без SPOTIFY_CLIENT_ID пропускаем."""
    global _spotify_token
    if not (SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET):
        return None
    if time.time() >= _spotify_token[0]:
        async with s.post("https://accounts.spotify.com/api/token",
                          data={"grant_type": "client_credentials"},
                          auth=aiohttp.BasicAuth(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET)) as r:
            token = await r.json(content_type=None) if r.status == 200 else {}
        if not token.get("access_token"):
            return None
        _spotify_token = (time.time() + int(token.get("expires_in") or 3600) - 60, token["access_token"])
    async with s.get("https://api.spotify.com/v1/search",
                     params={"q": name, "type": "artist", "limit": 10},
                     headers={"Authorization": f"Bearer {_spotify_token[1]}"}) as r:
        data = await r.json(content_type=None) if r.status == 200 else {}
    counts = [(a.get("followers") or {}).get("total") or 0
              for a in ((data or {}).get("artists") or {}).get("items") or []
              if (a.get("name") or "").casefold() == name.casefold()]
    return max(counts) if counts else None


_artist_cache: dict[str, tuple[float, dict]] = {}


async def artist_info(name: str) -> dict:
    """Фото исполнителя и его подписчики на Deezer, SoundCloud, Spotify и Яндекс Музыке.
    Площадки, где исполнитель не нашёлся или которые не ответили, просто не попадают в список."""
    key = name.casefold()
    if key in _artist_cache and time.time() - _artist_cache[key][0] < 6 * 3600:
        return _artist_cache[key][1]

    async def guard(coro):
        try:
            return await coro
        except Exception:
            return None

    deezer = await guard(_deezer_artist(name))
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15),
                                     headers={"User-Agent": "Mozilla/5.0 MusicCloudBot"}) as s:
        sc, sp, ya = await asyncio.gather(guard(_soundcloud_followers(s, name)),
                                          guard(_spotify_followers(s, name)),
                                          guard(_yandex_followers(s, name)))
    followers = [{"platform": p, "count": c} for p, c in
                 (("Deezer", (deezer or {}).get("nb_fan")), ("SoundCloud", sc),
                  ("Spotify", sp), ("Яндекс Музыка", ya)) if c is not None]
    info = {"fans": (deezer or {}).get("nb_fan"), "picture": (deezer or {}).get("picture_medium"),
            "followers": followers}
    if len(_artist_cache) > 500:
        _artist_cache.clear()
    _artist_cache[key] = (time.time(), info)
    return info


async def artist_tracks(name: str, sort: str, limit: int = 25) -> list[Found]:
    """Треки исполнителя: sort='popular' — самые популярные, 'new' — самые свежие релизы."""
    found = await _deezer_artist(name)
    if found and sort == "new":
        albums = (await _deezer(f"artist/{found['id']}/albums", limit=50)).get("data") or []
        albums = sorted((a for a in albums if a.get("id") and a.get("release_date")),
                        key=lambda a: a["release_date"], reverse=True)
        out: list[Found] = []
        for a in albums[:6]:  # самые свежие релизы, пока не наберётся нужное число треков
            data = (await _deezer(f"album/{a['id']}/tracks", limit=50)).get("data") or []
            for x in data:
                t = _deezer_track(x)
                t.cover = t.cover or a.get("cover_medium")
                t.released = a["release_date"]
                out.append(t)
            if len(out) >= limit:
                break
        if out:
            return out[:limit]
    elif found:  # у Deezer список /top уже упорядочен по популярности
        top = (await _deezer(f"artist/{found['id']}/top", limit=limit)).get("data") or []
        if top:
            return [_deezer_track(x) for x in top]
    # Deezer исполнителя не знает — сортируем обычный поиск по данным площадки
    tracks = await search(name, limit)
    if sort == "new":
        return sorted(tracks, key=lambda t: t.released or "", reverse=True)
    return sorted(tracks, key=lambda t: t.plays or 0, reverse=True)


# Жанры SoundCloud и Deezer называются по-разному — сводим частые варианты к словам Deezer
_GENRE_ALIASES = {"hiphop": "rap", "hip": "rap", "trap": "rap", "electronic": "electro",
                  "edm": "electro", "house": "electro", "techno": "electro", "dubstep": "electro",
                  "drum": "electro", "rnb": "r&b", "soul": "r&b", "indie": "alternative",
                  "punk": "rock", "metal": "metal", "classical": "classical", "folk": "folk"}
_genres: list[dict] = []


GENRE_RU = {"Pop": "Поп", "Rap/Hip Hop": "Рэп и хип-хоп", "Rock": "Рок", "Dance": "Танцевальная",
            "R&B": "R&B", "Alternative": "Альтернатива", "Electro": "Электроника", "Folk": "Фолк",
            "Reggae": "Регги", "Jazz": "Джаз", "Classical": "Классика", "Films/Games": "Саундтреки",
            "Metal": "Метал", "Soul & Funk": "Соул и фанк", "Blues": "Блюз", "Latin Music": "Латино",
            "Kids": "Детская", "Country": "Кантри", "African Music": "Африканская",
            "Asian Music": "Азиатская", "Brazilian Music": "Бразильская",
            "Indian Music": "Индийская"}


async def _load_genres() -> list[dict]:
    global _genres
    if not _genres:
        _genres = (await _deezer("genre")).get("data") or []
    return _genres


# Поджанры и направления, которых нет среди основных жанров Deezer.
# Треки для них берутся из популярных публичных плейлистов по запросу.
EXTRA_GENRES = {
    "phonk": ("Фонк", "phonk"), "lofi": ("Лоу-фай", "lofi"), "trap": ("Трэп", "trap"),
    "drill": ("Дрилл", "drill"), "rusrap": ("Русский рэп", "русский рэп"),
    "rusrock": ("Русский рок", "русский рок"), "ruspop": ("Русская поп-музыка", "русская поп музыка"),
    "house": ("Хаус", "house music"), "techno": ("Техно", "techno"), "trance": ("Транс", "trance"),
    "dnb": ("Драм-н-бейс", "drum and bass"), "dubstep": ("Дабстеп", "dubstep"),
    "synthwave": ("Синтвейв", "synthwave"), "ambient": ("Эмбиент", "ambient"),
    "hyperpop": ("Гиперпоп", "hyperpop"), "indie": ("Инди", "indie"), "punk": ("Панк", "punk rock"),
    "postpunk": ("Пост-панк", "post punk"), "grunge": ("Гранж", "grunge"),
    "kpop": ("K-pop", "k-pop"), "disco": ("Диско", "disco"), "chanson": ("Шансон", "шансон"),
    "acoustic": ("Акустика", "acoustic"), "instrumental": ("Инструментал", "instrumental"),
    "anime": ("Аниме", "anime openings"), "workout": ("Для тренировки", "workout"),
    "sleep": ("Для сна", "sleep music"), "retro": ("Ретро 80-х и 90-х", "80s 90s hits"),
}
EXTRA_PREFIX = "x-"


async def genres() -> list[dict]:
    """Жанры для раздела «Жанры»: основные из Deezer и дополнительные направления."""
    main = [{"id": str(g["id"]), "name": GENRE_RU.get(g.get("name"), g.get("name") or "")}
            for g in await _load_genres() if g.get("id")]
    extra = [{"id": EXTRA_PREFIX + key, "name": name} for key, (name, _) in EXTRA_GENRES.items()]
    return main + extra


async def search_playlists(query: str, limit: int = 20) -> list[dict]:
    """Публичные плейлисты Deezer по запросу."""
    data = (await _deezer("search/playlist", q=query, limit=limit)).get("data") or []
    return [{"id": p["id"], "title": p.get("title") or "", "cover": p.get("picture_medium") or None,
             "count": int(p.get("nb_tracks") or 0), "user": (p.get("user") or {}).get("name") or ""}
            for p in data if p.get("id")]


async def public_playlist(playlist_id: int, limit: int = 100) -> dict:
    """Публичный плейлист Deezer с треками."""
    data = await _deezer(f"playlist/{playlist_id}")
    tracks = [_deezer_track(x) for x in ((data.get("tracks") or {}).get("data") or [])[:limit]]
    return {"id": playlist_id, "title": data.get("title") or "",
            "cover": data.get("picture_medium") or None,
            "user": (data.get("creator") or {}).get("name") or "", "tracks": tracks}


async def genre_tracks(genre_id: str, limit: int = 40) -> list[Found]:
    """Популярные треки жанра по его id (числовой — жанр Deezer, x-… — дополнительный)."""
    if not genre_id.startswith(EXTRA_PREFIX):
        data = (await _deezer(f"chart/{int(genre_id)}/tracks", limit=limit)).get("data") or []
        return [_deezer_track(x) for x in data]
    _, query = EXTRA_GENRES[genre_id[len(EXTRA_PREFIX):]]
    # два самых подходящих плейлиста с достаточным числом треков, вперемешку
    found = [p for p in await search_playlists(query, 10) if p["count"] >= 20][:2]
    lists = await asyncio.gather(*(public_playlist(p["id"], limit) for p in found),
                                 return_exceptions=True)
    out, seen = [], set()
    for group in zip_longest(*(x["tracks"] for x in lists if isinstance(x, dict))):
        for t in group:
            if t and t.url not in seen:
                seen.add(t.url)
                out.append(t)
    return out[:limit]


async def genre_chart(genre: str, limit: int = 25) -> list[Found]:
    """Популярные треки жанра. Пусто, если у Deezer не нашлось похожего жанра."""
    await _load_genres()
    words = [w for w in re.split(r"[^\w&]+", genre.casefold()) if len(w) >= 3 or w == "r&b"]
    words += [_GENRE_ALIASES[w] for w in words if w in _GENRE_ALIASES]
    for g in _genres:
        name = (g.get("name") or "").casefold()
        if g.get("id") and any(w in name for w in words):
            data = (await _deezer(f"chart/{g['id']}/tracks", limit=limit)).get("data") or []
            return [_deezer_track(x) for x in data]
    return []


async def related_tracks(name: str) -> list[Found]:
    """Лучшие треки трёх самых похожих на исполнителя артистов (без него самого)."""
    found = await _deezer_artist(name)
    if not found:
        return []
    related = (await _deezer(f"artist/{found['id']}/related", limit=3)).get("data") or []
    tops = await asyncio.gather(*(_deezer(f"artist/{r['id']}/top", limit=4) for r in related[:3]),
                                return_exceptions=True)
    return [_deezer_track(x) for t in tops if isinstance(t, dict) for x in t.get("data") or []]


async def recommend(artists: list[str]) -> list[Found]:
    """Подборка по списку любимых исполнителей."""
    res = await asyncio.gather(*(_artist_picks(a) for a in artists), return_exceptions=True)
    out = [t for r in res if isinstance(r, list) for t in r]
    if not out:  # Deezer не знает этих исполнителей — ищем их же в основном источнике
        res = await asyncio.gather(*(search(a, 6) for a in artists[:3]), return_exceptions=True)
        out = [t for r in res if isinstance(r, list) for t in r]
    return out


def _album_genre(album: dict) -> str | None:
    genres = (album.get("genres") or {}).get("data") or []
    return (genres[0].get("name") or None) if genres else None


async def genre_for(title: str, artist: str) -> str:
    """Жанр трека по каталогу Deezer; пустая строка, если узнать не удалось."""
    if " - " in title:
        artist, title = (x.strip() for x in title.split(" - ", 1))
    title = _JUNK.sub("", title).strip() or title
    found = (await _deezer("search", q=f"{artist} {title}".strip(), limit=1)).get("data") or []
    album_id = (found[0].get("album") or {}).get("id") if found else None
    if not album_id:
        return ""
    return _album_genre(await _deezer(f"album/{album_id}")) or ""


def _album_brief(a: dict) -> Album:
    return Album(a["id"], a.get("title") or "", (a.get("artist") or {}).get("name") or "",
                 a.get("cover_medium") or None, int(a.get("nb_tracks") or 0),
                 a.get("release_date") or "")


async def _editorial_releases(limit: int) -> list[Album]:
    """Запасной вариант: редакционная подборка Deezer или чарт альбомов."""
    for path in ("editorial/0/releases", "chart/0/albums"):
        try:
            data = (await _deezer(path, limit=limit)).get("data") or []
        except Exception:
            data = []
        if data:
            return [_album_brief(a) for a in data if a.get("id")]
    return []


# Кто сейчас популярен в стране, берём из открытых чартов Apple Music: у Deezer чарты
# стран СНГ не обновляются. Сами релизы и обложки — по-прежнему из каталога Deezer.
APPLE_CHART = "https://rss.applemarketingtools.com/api/v2/{country}/music/most-played/50/songs.json"


async def _chart_artists(country: str, limit: int) -> list[str]:
    """Имена исполнителей из чарта страны (код вроде ru, kz, us), самые частые — первыми."""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s, \
            s.get(APPLE_CHART.format(country=country)) as r:
        if r.status != 200:
            return []
        data = await r.json(content_type=None)
    count: dict[str, int] = {}
    for item in ((data or {}).get("feed") or {}).get("results") or []:
        # «A, B & C» — совместный трек; берём первого исполнителя
        name = re.split(r",| & | feat\. ", item.get("artistName") or "")[0].strip()
        if name:
            count[name] = count.get(name, 0) + 1
    return sorted(count, key=count.get, reverse=True)[:limit]


async def _fresh_album(name: str, since: str, gate: asyncio.Semaphore) -> Album | None:
    """Самый свежий альбом исполнителя, если он вышел не раньше даты since."""
    async with gate:
        try:
            found = (await _deezer("search/artist", q=name, limit=1)).get("data") or []
            # только точное совпадение имени — иначе в ленту попадут чужие релизы
            if not found or (found[0].get("name") or "").casefold() != name.casefold():
                return None
            await asyncio.sleep(0.3)  # у Deezer лимит 50 запросов за 5 секунд
            data = (await _deezer(f"artist/{found[0]['id']}/albums", limit=25)).get("data") or []
        except Exception:
            return None
        finally:
            await asyncio.sleep(0.3)
    albums = [a for a in data if a.get("id") and (a.get("release_date") or "") >= since]
    if not albums:
        return None
    album = _album_brief(max(albums, key=lambda a: a["release_date"]))
    album.artist = album.artist or found[0].get("name") or name
    return album


async def new_releases(limit: int = 24) -> list[Album]:
    """Свежие релизы исполнителей, популярных в СНГ, и популярных зарубежных исполнителей."""
    since = (date.today() - timedelta(days=RELEASE_FRESH_DAYS)).isoformat()
    gate = asyncio.Semaphore(4)

    async def region_artists(countries: list[str]) -> list[str]:
        got = await asyncio.gather(*(_chart_artists(c, 25) for c in countries),
                                   return_exceptions=True)
        lists = [g for g in got if isinstance(g, list)]
        # по одному из каждого чарта по кругу, без повторов
        out: list[str] = []
        for i in range(max((len(x) for x in lists), default=0)):
            for x in lists:
                if i < len(x) and x[i].casefold() not in {o.casefold() for o in out}:
                    out.append(x[i])
        return out[:35]

    local, world = await asyncio.gather(region_artists(RELEASE_COUNTRIES),
                                        region_artists(RELEASE_WORLD))
    local_keys = {a.casefold() for a in local}
    world = [a for a in world if a.casefold() not in local_keys]

    async def fresh(ids: list[str], tag: str) -> list[Album]:
        got = await asyncio.gather(*(_fresh_album(i, since, gate) for i in ids))
        albums = sorted((a for a in got if a), key=lambda a: a.year, reverse=True)
        for a in albums:
            a.tag = tag
        return albums

    local_albums, world_albums = await asyncio.gather(fresh(local, "СНГ"), fresh(world, "мир"))
    # чередуем: релиз из СНГ, зарубежный, и так далее
    out, seen = [], set()
    for i in range(max(len(local_albums), len(world_albums))):
        for group in (local_albums, world_albums):
            if i < len(group) and group[i].id not in seen:
                seen.add(group[i].id)
                out.append(group[i])
    return out[:limit] or await _editorial_releases(limit)


async def latest_release(name: str, deezer_id: int = 0) -> tuple[int, Album | None] | None:
    """(id исполнителя в Deezer, его самый свежий альбом). None — исполнитель не найден."""
    if not deezer_id:
        found = (await _deezer("search/artist", q=name, limit=1)).get("data") or []
        # только точное совпадение имени: иначе придут уведомления о чужих релизах
        if not found or (found[0].get("name") or "").casefold() != name.casefold():
            return None
        deezer_id = found[0]["id"]
    albums = (await _deezer(f"artist/{deezer_id}/albums", limit=50)).get("data") or []
    albums = [a for a in albums if a.get("id") and a.get("release_date")]
    if not albums:
        return deezer_id, None
    newest = max(albums, key=lambda a: a["release_date"])
    album = _album_brief(newest)
    album.artist = album.artist or name
    return deezer_id, album


async def search_albums(query: str, limit: int = 8) -> list[Album]:
    data = await _deezer("search/album", q=query, limit=limit)
    return [Album(a["id"], a.get("title") or "", (a.get("artist") or {}).get("name") or "",
                  a.get("cover_medium") or None, int(a.get("nb_tracks") or 0))
            for a in data.get("data") or []]


async def get_album(album_id: int) -> Album:
    a, t = await asyncio.gather(_deezer(f"album/{album_id}"),
                                _deezer(f"album/{album_id}/tracks", limit=100))
    artist = (a.get("artist") or {}).get("name") or ""
    cover = a.get("cover_big") or a.get("cover_medium") or None
    genre = _album_genre(a)
    tracks = []
    for x in t.get("data") or []:
        who = (x.get("artist") or {}).get("name") or artist
        title = x.get("title") or ""
        tracks.append(Found(f"{SEARCH_PREFIX}{who} - {title}", title, who,
                            int(x.get("duration") or 0), cover, genre))
    return Album(album_id, a.get("title") or "", artist, cover, len(tracks),
                 (a.get("release_date") or "")[:4], tracks)
