"""Поиск и загрузка аудио через yt-dlp, альбомы — через открытый API Deezer."""
import asyncio
import random
import re
import shutil
from dataclasses import dataclass, field
from datetime import date, timedelta
from itertools import zip_longest
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from yt_dlp import YoutubeDL

from config import (MAX_DOWNLOADS, MAX_FILE_SIZE, RELEASE_COUNTRIES, RELEASE_FRESH_DAYS,
                    RELEASE_WORLD, SEARCH_SOURCE, TMP_DIR)

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


async def import_playlist(url: str) -> tuple[str, list[Found]]:
    """(название, треки) плейлиста по ссылке с YouTube, SoundCloud, Bandcamp или Deezer."""
    parts = urlparse(url.strip())
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or \
            not any(host == h or host.endswith("." + h) for h in IMPORT_HOSTS):
        raise ImportError_("Поддерживаются ссылки YouTube, SoundCloud, Bandcamp и Deezer")
    if host.endswith("deezer.com"):
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
        raise ImportError_("В плейлисте не нашлось треков")
    return title[:40], tracks[:IMPORT_LIMIT]


def _download(url: str) -> Path:
    TMP_DIR.mkdir(exist_ok=True)
    has_ffmpeg = shutil.which("ffmpeg") is not None
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "max_filesize": MAX_FILE_SIZE,
        "outtmpl": str(TMP_DIR / "%(id)s.%(ext)s"),
        # без ffmpeg берём то, что Telegram проигрывает как есть (mp3 / m4a)
        "format": "bestaudio/best" if has_ffmpeg
        else "bestaudio[ext=mp3]/bestaudio[ext=m4a]/bestaudio[protocol^=http]/bestaudio/best",
    }
    if has_ffmpeg:
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ]
    if url.startswith(SEARCH_PREFIX):
        url = f"{SEARCH_SOURCE}1:{url[len(SEARCH_PREFIX):]}"
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as e:
        if "DRM" in str(e):
            raise TrackProtected("трек защищён правообладателем") from e
        raise
    if info.get("entries") is not None:  # результат поиска — берём первый трек
        entries = [e for e in info["entries"] if e]
        if not entries:
            raise RuntimeError("трек не найден")
        info = entries[0]
    downloads = info.get("requested_downloads") or []
    path = Path(downloads[0]["filepath"]) if downloads else None
    if not path or not path.exists():
        raise RuntimeError("файл не загружен (возможно, он больше 50 МБ)")
    if path.stat().st_size > MAX_FILE_SIZE:
        path.unlink(missing_ok=True)
        raise RuntimeError("файл больше 50 МБ")
    return path


async def search(query: str, limit: int = 8) -> list[Found]:
    return await asyncio.to_thread(_search, query, limit)


_downloads = asyncio.Semaphore(MAX_DOWNLOADS)


async def download(url: str) -> Path:
    async with _downloads:
        return await asyncio.to_thread(_download, url)


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


async def artist_info(name: str) -> dict:
    """Подписчики (фанаты) исполнителя на Deezer и его фото; пусто, если Deezer его не знает."""
    try:
        found = await _deezer_artist(name)
    except Exception:
        found = None
    if not found:
        return {"fans": None, "picture": None}
    return {"fans": found.get("nb_fan"), "picture": found.get("picture_medium") or None}


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
