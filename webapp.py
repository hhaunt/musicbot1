"""Веб-сервер мини-приложения: статика, JSON API и раздача аудио."""
import asyncio
import hashlib
import hmac
import logging
import mimetypes
import os
import random
import shutil
import time
from collections import Counter
from itertools import zip_longest
from pathlib import Path
from urllib.parse import quote

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, FSInputFile
from aiogram.utils.web_app import safe_parse_webapp_init_data
from aiogram.webhook.aiohttp_server import SimpleRequestHandler
from aiohttp import web

import db
import images
import music
from config import (ADMIN_IDS, BASE_DIR, BOT_TOKEN, BRAND, CACHE_DIR, CACHE_FILES, CACHE_MAX_MB,
                    MAX_PLAYLISTS, PROFILES_PUBLIC_DEFAULT, WEBAPP_PORT, WEBAPP_URL)

log = logging.getLogger("mono.web")
WEBHOOK_PATH = "/tg/webhook"
# Telegram присылает этот ключ в заголовке — чужие запросы на вебхук отбрасываются
WEBHOOK_SECRET = hashlib.sha256(("webhook:" + BOT_TOKEN).encode()).hexdigest()[:32]
routes = web.RouteTableDef()
# на Linux-образах эти типы часто не прописаны — без них браузер может не узнать FLAC и m4a
mimetypes.add_type("audio/flac", ".flac")
mimetypes.add_type("audio/mp4", ".m4a")
mimetypes.add_type("audio/ogg", ".ogg")
mimetypes.add_type("audio/ogg", ".opus")
_locks: dict[int, asyncio.Lock] = {}
_cloud_cache: dict[int, tuple[float, dict]] = {}
_lyrics_cache: dict[int, dict] = {}
_releases_cache: tuple[float, list] = (0.0, [])
_releases_lock = asyncio.Lock()
CLOUD_TTL = 30 * 60   # сколько секунд держать готовую подборку
CLOUD_SIZE = 30       # треков в подборке


# ── авторизация ──────────────────────────────────────────────────────────

def user_id(request: web.Request) -> int:
    """Проверяет подпись Telegram у initData и возвращает id пользователя."""
    try:
        data = safe_parse_webapp_init_data(BOT_TOKEN, request.headers.get("X-Init-Data", ""))
    except ValueError:
        raise web.HTTPUnauthorized()
    if not data.user:
        raise web.HTTPUnauthorized()
    return data.user.id


def stream_key(uid: int) -> str:
    """Ключ для ссылок на аудио: тег <audio> не умеет отправлять заголовки."""
    return hmac.new(BOT_TOKEN.encode(), str(uid).encode(), hashlib.sha256).hexdigest()[:32]


# ── сериализация ─────────────────────────────────────────────────────────

async def fav_ids(uid: int) -> set[int]:
    return {t["id"] for t in await db.favorites(uid)}


def track_json(t, favs: set[int]) -> dict:
    keys = t.keys()

    def stat(ext: str, app: str):
        """Число с площадки-источника, а если она его не сообщает — число внутри MusicCloud."""
        value = t[ext] if ext in keys else None
        if value is not None:
            return value, "ext"
        return (t[app] if app in keys else 0), "app"

    plays, plays_src = stat("ext_plays", "app_plays")
    likes, likes_src = stat("ext_likes", "app_likes")
    return {"id": t["id"], "title": t["title"], "artist": t["artist"],
            "duration": t["duration"], "cover": t["cover"], "fav": t["id"] in favs,
            "plays": plays, "likes": likes, "stats_from": plays_src if plays_src == likes_src else "mixed",
            "released": t["released"] if "released" in keys else None}


async def ensure_album(album_id: int):
    album = await db.get_album(album_id)
    if not album:
        a = await music.get_album(album_id)
        ids = [await db.upsert_found(t)
               for t in a.tracks]
        await db.save_album(a.id, a.title, a.artist, a.year, a.cover, ids)
        album = await db.get_album(album_id)
    return album


async def own_playlist(request: web.Request, uid: int):
    pl = await db.get_playlist(int(request.match_info["pid"]), uid)
    if not pl:
        raise web.HTTPNotFound()
    return pl


# ── API ──────────────────────────────────────────────────────────────────

@routes.get("/api/me")
async def api_me(request):
    try:
        tg = safe_parse_webapp_init_data(BOT_TOKEN, request.headers.get("X-Init-Data", "")).user
    except ValueError:
        tg = None
    if not tg:
        raise web.HTTPUnauthorized()
    uid = tg.id
    name = " ".join(x for x in (tg.first_name, tg.last_name) if x)[:64]
    await db.upsert_user(uid, name, tg.username, tg.photo_url, PROFILES_PUBLIC_DEFAULT)
    await db.touch_usage(uid, "app")
    return web.json_response({
        "stats": await db.usage_stats() if uid in ADMIN_IDS else None,
        "uid": uid, "key": stream_key(uid),
        "favorites": len(await db.favorites(uid)),
        "playlists": len(await db.playlists(uid)),
        "plays": await db.play_count(uid),
        "followers": await db.count_social("follow", "user", str(uid)),
        "likes": await db.count_social("like", "user", str(uid)),
        "public": bool((await db.get_user(uid))["public"]),
        "notify": bool((await db.get_user(uid))["notify"]),
        "seconds": await db.listen_total(uid),
    })


@routes.post("/api/me/notify")
async def api_me_notify(request):
    uid = user_id(request)
    notify = bool((await request.json()).get("notify"))
    await db.set_notify(uid, notify)
    return web.json_response({"notify": notify})


# ── «Сейчас играет» для Discord ──────────────────────────────────────────
# Мини-приложение сообщает, что и с какой секунды играет; программа-компаньон на компьютере
# слушателя забирает это и показывает в профиле Discord. Сам сервер в Discord не ходит.

_now_playing: dict[int, dict] = {}
_app_bot: Bot | None = None  # нужен, чтобы скачивать из Telegram присланные слушателями файлы
_bot_username: str | None = None


def presence_key(uid: int) -> str:
    """Отдельный ключ для компаньона Discord, чтобы не раздавать ключ от аудио."""
    return hmac.new(BOT_TOKEN.encode(), f"presence:{uid}".encode(), hashlib.sha256).hexdigest()[:32]


@routes.post("/api/nowplaying")
async def api_nowplaying(request):
    uid = user_id(request)
    body = await request.json()
    try:
        tid, pos = int(body.get("id")), max(0.0, float(body.get("pos") or 0))
    except (TypeError, ValueError):
        raise web.HTTPBadRequest()
    _now_playing[uid] = {"id": tid, "pos": pos, "playing": bool(body.get("playing")),
                         "ts": time.time()}
    return web.json_response({"ok": True})


@routes.get("/api/presence/key")
async def api_presence_key(request):
    uid = user_id(request)
    return web.json_response({"uid": uid, "key": presence_key(uid)})


@routes.get("/api/presence")
async def api_presence(request):
    """Состояние для компаньона: ключ в ссылке, потому что входа через Telegram у него нет."""
    global _bot_username
    uid = request.query.get("u", "")
    if not uid.isdigit() or not hmac.compare_digest(request.query.get("k", ""),
                                                    presence_key(int(uid))):
        raise web.HTTPUnauthorized()
    state = _now_playing.get(int(uid))
    # приложение шлёт вести каждые 25 секунд, даже на паузе; полторы минуты тишины — оно закрыто
    if not state or time.time() - state["ts"] > 90:
        return web.json_response({"playing": False, "paused": False})
    track = await db.get_track(state["id"])
    if not track:
        return web.json_response({"playing": False, "paused": False})
    if _bot_username is None:
        try:
            _bot_username = (await request.app["bot"].me()).username or ""
        except Exception:
            _bot_username = ""
    playing = state["playing"]
    return web.json_response({
        "playing": playing, "paused": not playing,
        "title": track["title"], "artist": track["artist"],
        "cover": track["cover"], "duration": track["duration"],
        # на паузе позиция стоит на месте
        "position": state["pos"] + (time.time() - state["ts"] if playing else 0),
        "brand": BRAND, "link": f"https://t.me/{_bot_username}" if _bot_username else None,
        # иконка приложения — аватарка бота, её же отдаёт /api/logo
        "logo": f"{WEBAPP_URL}/api/logo" if request.app["logo"] and WEBAPP_URL else None,
    })


@routes.post("/api/listen")
async def api_listen(request):
    """Приложение раз в полминуты сообщает, сколько секунд трека реально прозвучало."""
    uid = user_id(request)
    body = await request.json()
    try:
        tid, seconds = int(body.get("id")), int(body.get("seconds"))
    except (TypeError, ValueError):
        raise web.HTTPBadRequest()
    if 0 < seconds <= 120 and await db.get_track(tid):  # больше двух минут за раз не бывает
        await db.add_listen(uid, tid, seconds)
    return web.json_response({"ok": True})


@routes.get("/api/stats/me")
async def api_stats_me(request):
    """Часы прослушивания: всего, по исполнителям и по жанрам."""
    uid = user_id(request)
    rows = await db.listen_rows(uid)
    genres = {r["id"]: r["genre"] for r in rows}
    # жанр неизвестных треков узнаём понемногу, начиная с самых слушаемых
    unknown = [r for r in rows if r["genre"] is None][:8]
    looked = await asyncio.gather(*(music.genre_for(r["title"], r["artist"]) for r in unknown),
                                  return_exceptions=True)
    for r, genre in zip(unknown, looked):
        if isinstance(genre, str):
            await db.set_genre(r["id"], genre)
            genres[r["id"]] = genre
    by_artist, by_genre = Counter(), Counter()
    for r in rows:
        by_artist[_artist_of(r["title"], r["artist"]) or "Неизвестный"] += r["seconds"]
        if genres[r["id"]]:
            by_genre[genres[r["id"]]] += r["seconds"]
    top = lambda c, n: [{"name": k, "seconds": v} for k, v in c.most_common(n)]  # noqa: E731
    return web.json_response({"seconds": sum(r["seconds"] for r in rows),
                              "artists": top(by_artist, 10), "genres": top(by_genre, 8)})


@routes.get("/api/releases")
async def api_releases(request):
    """Новые релизы популярных исполнителей — одна лента на всех, обновляется раз в 3 часа."""
    user_id(request)
    global _releases_cache
    async with _releases_lock:  # сборка идёт десятки секунд — не запускаем её дважды
        fresh = time.time() - _releases_cache[0] < (3 * 3600 if _releases_cache[1] else 300)
        if not fresh:
            try:
                found = await music.new_releases(24)
            except Exception as e:
                log.warning("releases failed: %s", e)
                found = []
            _releases_cache = (time.time(), [{"id": a.id, "title": a.title, "artist": a.artist,
                                              "cover": a.cover, "tag": a.tag} for a in found])
    return web.json_response(_releases_cache[1])


@routes.post("/api/me/public")
async def api_me_public(request):
    uid = user_id(request)
    public = bool((await request.json()).get("public"))
    await db.set_public(uid, public)
    return web.json_response({"public": public})


@routes.get("/api/history")
async def api_history(request):
    uid = user_id(request)
    favs = await fav_ids(uid)
    return web.json_response([track_json(t, favs) for t in await db.history(uid)])


# ── люди и исполнители: подписки и лайки ─────────────────────────────────

def user_json(u) -> dict:
    return {"id": u["id"], "name": u["name"] or "Без имени", "photo": u["photo"]}


async def social_json(uid: int, kind: str, target: str) -> dict:
    return {"followers": await db.count_social("follow", kind, target),
            "likes": await db.count_social("like", kind, target),
            "following": await db.has_social(uid, "follow", kind, target),
            "liked": await db.has_social(uid, "like", kind, target)}


@routes.get("/api/people")
async def api_people(request):
    uid = user_id(request)
    query = request.query.get("q", "").strip().casefold()
    out = []
    for u in await db.public_users(uid):
        if query and query not in (u["name"] or "").casefold():
            continue
        out.append({**user_json(u), "followers": u["followers"]})
        if len(out) >= 50:
            break
    return web.json_response(out)


@routes.get("/api/user/{id}")
async def api_user(request):
    uid = user_id(request)
    other = await db.get_user(int(request.match_info["id"]))
    # закрытый профиль виден только самому владельцу
    if not other or (not other["public"] and other["id"] != uid):
        raise web.HTTPNotFound()
    favs = await fav_ids(uid)
    tracks = [track_json(t, favs) for t in (await db.favorites(other["id"]))[:100]]
    return web.json_response({**user_json(other), "tracks": tracks,
                              **await social_json(uid, "user", str(other["id"]))})


@routes.get("/api/artist")
async def api_artist(request):
    uid = user_id(request)
    name = " ".join(request.query.get("name", "").split())[:100]
    sort = request.query.get("sort", "popular")
    if not name or sort not in ("popular", "new"):
        raise web.HTTPBadRequest()
    try:
        found, info = await asyncio.gather(music.artist_tracks(name, sort, 25),
                                           music.artist_info(name))
    except Exception as e:
        log.warning("artist tracks failed for %r: %s", name, e)
        found, info = [], {"fans": None, "picture": None, "followers": []}
    return web.json_response({"name": name, "sort": sort, "tracks": await save_tracks(uid, found),
                              "fans": info["fans"], "picture": info["picture"],
                              "platforms": info.get("followers") or [],
                              **await social_json(uid, "artist", name)})


@routes.post("/api/social")
async def api_social(request):
    """Ставит или снимает подписку (rel=follow) либо лайк (rel=like)."""
    uid = user_id(request)
    body = await request.json()
    rel, kind = body.get("rel"), body.get("kind")
    target = " ".join(str(body.get("target", "")).split())[:100]
    if rel not in ("follow", "like") or kind not in ("artist", "user") or not target:
        raise web.HTTPBadRequest()
    if kind == "user":
        other = await db.get_user(int(target)) if target.isdigit() else None
        if not other or not other["public"] or other["id"] == uid:
            raise web.HTTPNotFound()
    on = await db.toggle_social(uid, rel, kind, target)
    return web.json_response({"on": on, "count": await db.count_social(rel, kind, target)})


@routes.get("/api/subs")
async def api_subs(request):
    uid = user_id(request)
    users = []
    for target in await db.following(uid, "user"):
        u = await db.get_user(int(target))
        if u and u["public"]:
            users.append(user_json(u))
    return web.json_response({"artists": await db.following(uid, "artist"), "users": users})


@routes.post("/api/play/{tid}")
async def api_play(request):
    """Отмечает прослушивание — по ним подбирается «Моё облако»."""
    uid = user_id(request)
    tid = int(request.match_info["tid"])
    if not await db.get_track(tid):
        raise web.HTTPNotFound()
    await db.add_play(uid, tid)
    return web.json_response({"ok": True})


# ── комментарии к трекам ─────────────────────────────────────────────────

COMMENT_MAX = 300        # символов в комментарии
COMMENTS_PER_TRACK = 30  # комментариев одного человека к одному треку
_last_comment: dict[int, float] = {}


def comment_json(c, uid: int) -> dict:
    return {"id": c["id"], "text": c["text"], "at": c["at"], "created": c["created"],
            "user": {"id": c["user_id"], "name": c["name"] or "Слушатель", "photo": c["photo"]},
            # удалить может автор и администратор
            "mine": c["user_id"] == uid or uid in ADMIN_IDS}


@routes.get("/api/comments/{tid}")
async def api_comments(request):
    uid = user_id(request)
    rows = await db.comments(int(request.match_info["tid"]))
    return web.json_response([comment_json(c, uid) for c in rows])


@routes.post("/api/comments/{tid}")
async def api_comment_add(request):
    """Комментарий к треку: с таймкодом (at — секунда трека) или без него."""
    uid = user_id(request)
    track = await db.get_track(int(request.match_info["tid"]))
    if not track:
        raise web.HTTPNotFound()
    body = await request.json()
    text = " ".join(str(body.get("text", "")).split())[:COMMENT_MAX]
    if not text:
        return web.json_response({"error": "Пустой комментарий"}, status=400)
    at = body.get("at")
    if at is not None:
        try:
            at = max(0, int(at))
        except (TypeError, ValueError):
            at = None
        if at is not None and track["duration"] and at > track["duration"] + 5:
            at = None
    if time.time() - _last_comment.get(uid, 0) < 3:
        return web.json_response({"error": "Слишком часто — подождите пару секунд"}, status=429)
    if await db.comment_count(track["id"], uid) >= COMMENTS_PER_TRACK:
        return web.json_response({"error": "Слишком много комментариев к этому треку"}, status=429)
    if not await db.get_user(uid):
        raise web.HTTPUnauthorized()
    _last_comment[uid] = time.time()
    cid = await db.add_comment(track["id"], uid, text, at)
    return web.json_response(comment_json(await db.get_comment(cid), uid))


@routes.delete("/api/comment/{cid}")
async def api_comment_delete(request):
    uid = user_id(request)
    c = await db.get_comment(int(request.match_info["cid"]))
    if not c:
        raise web.HTTPNotFound()
    if c["user_id"] != uid and uid not in ADMIN_IDS:
        raise web.HTTPForbidden()
    await db.delete_comment(c["id"])
    return web.json_response({"ok": True})


@routes.get("/api/lyrics/{tid}")
async def api_lyrics(request):
    user_id(request)
    tid = int(request.match_info["tid"])
    if tid not in _lyrics_cache:
        track = await db.get_track(tid)
        if not track:
            raise web.HTTPNotFound()
        try:
            found = await music.lyrics(track["title"], track["artist"], track["duration"])
        except Exception as e:
            log.warning("lyrics failed for %s: %s", track["title"], e)
            return web.json_response({"synced": None, "plain": None})
        if len(_lyrics_cache) > 500:
            _lyrics_cache.clear()
        _lyrics_cache[tid] = found
    return web.json_response(_lyrics_cache[tid])


# ── «Моё облако»: подборка под вкус ──────────────────────────────────────

def _artist_of(title: str, artist: str) -> str:
    # на SoundCloud исполнитель часто записан в названии: «Исполнитель - Трек»
    return (title.split(" - ")[0] if " - " in title else artist).strip()


async def taste_profile(uid: int) -> tuple[Counter, Counter]:
    """Очки исполнителей и жанров по тому, что человек реально делал.
    Исполнитель: лайк трека — 3, трек в плейлисте — 2, запуск — 1, каждая прослушанная
    минута — 0,5, подписка — 6, лайк исполнителю — 4. Жанр: прослушанная минута — 1, лайк — 3."""
    artists, genres = Counter(), Counter()
    for title, artist, weight in await db.taste(uid):
        if (name := _artist_of(title, artist)):
            artists[name] += weight
    for r in await db.listen_rows(uid):
        minutes = r["seconds"] / 60
        if (name := _artist_of(r["title"], r["artist"])):
            artists[name] += minutes * 0.5
        if r["genre"]:
            genres[r["genre"]] += minutes
    for t in await db.favorites(uid):
        if t["genre"]:
            genres[t["genre"]] += 3
    for name in await db.social_targets(uid, "follow", "artist"):
        artists[name] += 6
    for name in await db.social_targets(uid, "like", "artist"):
        artists[name] += 4
    return artists, genres


async def favorite_genre(uid: int, favs=None) -> str | None:
    _, genres = await taste_profile(uid)
    return genres.most_common(1)[0][0] if genres else None


async def _safe(coro, what: str) -> list:
    try:
        return await coro
    except Exception as e:
        log.warning("cloud: %s failed: %s", what, e)
        return []


# «Моя волна» по образцу стриминговых сервисов: бесконечный поток вокруг того, что человек
# слушает. Основа — похожие треки, которые сам SoundCloud подбирает к любимым трекам
# слушателя; сверху немного его же лайков и любимых исполнителей. Быстрые пропуски
# (в первые 30 секунд) — сигнал «не моё»: такие треки больше не попадают, а исполнитель,
# которого пропустили трижды, выпадает из волны.
WAVE_SEEDS = 5         # от скольких любимых треков строим волну за раз
WAVE_SHARES = (("похоже на ваше", 20), ("ваш исполнитель", 5), ("из ваших лайков", 5))


async def build_cloud(uid: int, exclude: frozenset | set = frozenset()) -> dict:
    favs = await db.favorites(uid)
    artists, _ = await taste_profile(uid)
    skipped = await db.skipped_ids(uid)
    banned = {a.casefold() for a, n in (await db.skipped_artists(uid)).items() if n >= 3}
    recent = await db.recent_play_ids(uid, 300)

    # зёрна волны: лайкнутые треки и то, что долго слушали; чем любимее, тем чаще выпадают
    weight: dict[int, float] = {}
    for t in favs:
        weight[t["id"]] = weight.get(t["id"], 0) + 3
    for r in await db.listen_rows(uid):
        weight[r["id"]] = weight.get(r["id"], 0) + r["seconds"] / 120
    pool = [tid for tid in weight if tid not in skipped]
    seeds: list = []
    while pool and len(seeds) < WAVE_SEEDS:
        tid = random.choices(pool, weights=[max(weight[t], 0.01) for t in pool])[0]
        pool.remove(tid)
        if (t := await db.get_track(tid)):
            seeds.append(t)

    async def related():
        got = await asyncio.gather(*(music.sc_related(t["url"], t["artist"], t["title"], 15)
                                     for t in seeds), return_exceptions=True)
        # по кругу от каждого зерна, чтобы волна не застревала на одном треке
        lists = [g for g in got if isinstance(g, list)]
        return [f for group in zip_longest(*lists) for f in group if f]

    async def top_artists():
        names = [name for name, _ in artists.most_common(3)]
        got = await asyncio.gather(*(music.artist_tracks(n, "popular", 8) for n in names),
                                   return_exceptions=True)
        return [t for g in got if isinstance(g, list) for t in g]

    found_related, found_artists = await asyncio.gather(_safe(related(), "related"),
                                                        _safe(top_artists(), "artists"))
    random.shuffle(found_artists)
    # знакомое: лайки, которые давно не звучали
    familiar = [t for t in favs if t["id"] not in recent and t["id"] not in exclude]
    random.shuffle(familiar)

    seen_ids: set[int] = set()
    picked: list[tuple[int, str]] = []

    async def take(source: list, why: str, saved: bool) -> bool:
        """Берёт из источника первый подходящий трек: не пропущенный, не звучавший недавно,
        не от исполнителя, которого постоянно пропускают."""
        while source:
            item = source.pop(0)
            artist = item["artist"] if saved else item.artist
            if (artist or "").casefold() in banned:
                continue
            tid = item["id"] if saved else await db.upsert_found(item)
            if tid in seen_ids or tid in skipped or tid in exclude or (not saved and tid in recent):
                continue
            seen_ids.add(tid)
            picked.append((tid, why))
            return True
        return False

    sources = {"похоже на ваше": (found_related, False), "ваш исполнитель": (found_artists, False),
               "из ваших лайков": (familiar, True)}
    left = {why: share for why, share in WAVE_SHARES}
    # по кругу: несколько похожих, потом что-то из своего — как чередует «волна»
    for limited in (True, False):
        progress = True
        while progress and len(picked) < CLOUD_SIZE:
            progress = False
            for why, _ in WAVE_SHARES:
                if len(picked) >= CLOUD_SIZE or (limited and left[why] <= 0):
                    continue
                source, saved = sources[why]
                for _ in range(4 if why == "похоже на ваше" else 1):
                    if len(picked) < CLOUD_SIZE and (not limited or left[why] > 0) \
                            and await take(source, why, saved):
                        left[why] -= 1
                        progress = True
    return {"items": picked, "based_on": {
        "seeds": [f"{t['artist']} — {t['title']}" if t["artist"] else t["title"] for t in seeds[:3]],
        "artists": [name for name, _ in artists.most_common(3)],
    }}


@routes.get("/api/cloud")
async def api_cloud(request):
    uid = user_id(request)
    cached = _cloud_cache.get(uid)
    if "refresh" in request.query or not cached or time.time() - cached[0] > CLOUD_TTL:
        cached = (time.time(), await build_cloud(uid))
        _cloud_cache[uid] = cached
    favs = await fav_ids(uid)
    tracks = [{**track_json(await db.get_track(tid), favs), "why": why}
              for tid, why in cached[1]["items"]]
    return web.json_response({"based_on": cached[1]["based_on"], "tracks": tracks})


@routes.get("/api/cloud/more")
async def api_cloud_more(request):
    """Продолжение очереди, когда она закончилась: свежая подборка по вкусу без уже звучавшего."""
    uid = user_id(request)
    exclude = {int(x) for x in request.query.get("exclude", "").split(",") if x.isdigit()}
    built = await build_cloud(uid, exclude)  # каждый раз новая: зёрна волны выбираются случайно
    favs = await fav_ids(uid)
    tracks = [{**track_json(await db.get_track(tid), favs), "why": why}
              for tid, why in built["items"]]
    return web.json_response(tracks)


@routes.post("/api/skip/{tid}")
async def api_skip(request):
    """Трек пропустили в первые 30 секунд — волна больше его не предложит."""
    uid = user_id(request)
    tid = int(request.match_info["tid"])
    if await db.get_track(tid):
        await db.add_skip(uid, tid)
        _cloud_cache.pop(uid, None)  # следующая подборка уже учтёт пропуск
    return web.json_response({"ok": True})


@routes.get("/api/search/all")
async def api_search_all(request):
    """Общий поиск: исполнители, альбомы, плейлисты и треки по одному запросу."""
    uid = user_id(request)
    query = " ".join(request.query.get("q", "").split())[:100]
    if not query:
        return web.json_response({"artists": [], "albums": [], "playlists": [], "tracks": []})
    tracks, artists, albums, playlists = await asyncio.gather(
        _safe(music.search(query, 20), "search"),
        _safe(music.search_artists(query, 8), "artists"),
        _safe(music.search_albums(query, 8), "albums"),
        _safe(music.search_playlists(query, 8), "playlists"))
    return web.json_response({
        "artists": artists,
        "albums": [{"id": a.id, "title": a.title, "artist": a.artist, "cover": a.cover,
                    "count": a.count} for a in albums],
        "playlists": playlists,
        "tracks": await save_tracks(uid, tracks),
    })


@routes.get("/api/search")
async def api_search(request):
    uid = user_id(request)
    query = " ".join(request.query.get("q", "").split())[:100]
    if not query:
        return web.json_response([])
    try:
        found = await music.search(query, 20)
    except Exception as e:
        log.warning("search failed for %r: %s", query, e)
        found = []
    favs = await fav_ids(uid)
    out = []
    for f in found:
        tid = await db.upsert_found(f)
        out.append(track_json(await db.get_track(tid), favs))
    return web.json_response(out)


@routes.get("/api/albums")
async def api_albums(request):
    user_id(request)
    query = " ".join(request.query.get("q", "").split())[:100]
    if not query:
        return web.json_response([])
    try:
        found = await music.search_albums(query, 20)
    except Exception as e:
        log.warning("album search failed for %r: %s", query, e)
        found = []
    return web.json_response([{"id": a.id, "title": a.title, "artist": a.artist,
                               "cover": a.cover, "count": a.count} for a in found])


@routes.get("/api/album/{aid}")
async def api_album(request):
    uid = user_id(request)
    try:
        album = await ensure_album(int(request.match_info["aid"]))
    except Exception as e:
        log.warning("album failed: %s", e)
        raise web.HTTPBadGateway()
    favs = await fav_ids(uid)
    tracks = [track_json(t, favs) for t in await db.album_tracks(album["id"])]
    return web.json_response({"id": album["id"], "title": album["title"],
                              "artist": album["artist"], "year": album["year"],
                              "cover": album["cover"], "tracks": tracks})


@routes.post("/api/album/{aid}/save")
async def api_album_save(request):
    uid = user_id(request)
    album = await db.get_album(int(request.match_info["aid"]))
    if not album:
        raise web.HTTPNotFound()
    if len(await db.playlists(uid)) >= MAX_PLAYLISTS:
        raise web.HTTPConflict()
    pid = await db.create_playlist(uid, album["title"][:40])
    for t in await db.album_tracks(album["id"]):
        await db.add_to_playlist(pid, t["id"])
    return web.json_response({"id": pid})


@routes.get("/api/favorites")
async def api_favorites(request):
    uid = user_id(request)
    tracks = await db.favorites(uid)
    favs = {t["id"] for t in tracks}
    return web.json_response([track_json(t, favs) for t in tracks])


@routes.post("/api/fav/{tid}")
async def api_fav(request):
    uid = user_id(request)
    tid = int(request.match_info["tid"])
    if not await db.get_track(tid):
        raise web.HTTPNotFound()
    return web.json_response({"fav": await db.toggle_fav(uid, tid)})


@routes.get("/api/playlists")
async def api_playlists(request):
    uid = user_id(request)
    return web.json_response([{"id": p["id"], "name": p["name"], "count": p["cnt"]}
                              for p in await db.playlists(uid)])


@routes.post("/api/playlists")
async def api_playlist_create(request):
    uid = user_id(request)
    body = await request.json()
    name = " ".join(str(body.get("name", "")).split())[:40]
    if not name:
        raise web.HTTPBadRequest()
    if len(await db.playlists(uid)) >= MAX_PLAYLISTS:
        raise web.HTTPConflict()
    return web.json_response({"id": await db.create_playlist(uid, name)})


@routes.post("/api/playlists/import")
async def api_playlist_import(request):
    """Создаёт плейлист из ссылки на плейлист YouTube, SoundCloud, Bandcamp или Deezer."""
    uid = user_id(request)
    url = str((await request.json()).get("url", ""))[:500]
    if len(await db.playlists(uid)) >= MAX_PLAYLISTS:
        return web.json_response({"error": f"Не больше {MAX_PLAYLISTS} плейлистов"}, status=409)
    try:
        title, tracks = await music.import_playlist(url)
    except music.ImportError_ as e:
        return web.json_response({"error": str(e)}, status=400)
    pid = await db.create_playlist(uid, title)
    for f in tracks:
        tid = await db.upsert_found(f)
        await db.add_to_playlist(pid, tid)
    return web.json_response({"id": pid, "name": title, "count": len(tracks)})


@routes.get("/api/genres")
async def api_genres(request):
    user_id(request)
    try:
        return web.json_response(await music.genres())
    except Exception as e:
        log.warning("genres failed: %s", e)
        return web.json_response([])


@routes.get("/api/genre/{gid}")
async def api_genre(request):
    uid = user_id(request)
    gid = request.match_info["gid"]
    if not (gid.isdigit() or gid[len(music.EXTRA_PREFIX):] in music.EXTRA_GENRES
            and gid.startswith(music.EXTRA_PREFIX)):
        raise web.HTTPNotFound()
    try:
        found = await music.genre_tracks(gid)
    except Exception as e:
        log.warning("genre tracks failed: %s", e)
        raise web.HTTPBadGateway()
    return web.json_response(await save_tracks(uid, found))


async def save_tracks(uid: int, found: list) -> list[dict]:
    """Сохраняет найденные треки в базу и возвращает их в виде для приложения."""
    favs = await fav_ids(uid)
    out = []
    for f in found:
        tid = await db.upsert_found(f)
        out.append(track_json(await db.get_track(tid), favs))
    return out


@routes.get("/api/playlist-search")
async def api_playlist_search(request):
    """Поиск публичных плейлистов."""
    user_id(request)
    query = " ".join(request.query.get("q", "").split())[:100]
    if not query:
        return web.json_response([])
    try:
        return web.json_response(await music.search_playlists(query))
    except Exception as e:
        log.warning("playlist search failed for %r: %s", query, e)
        return web.json_response([])


@routes.get("/api/public-playlist/{id}")
async def api_public_playlist(request):
    uid = user_id(request)
    try:
        pl = await music.public_playlist(int(request.match_info["id"]))
    except Exception as e:
        log.warning("public playlist failed: %s", e)
        raise web.HTTPBadGateway()
    return web.json_response({**pl, "tracks": await save_tracks(uid, pl["tracks"])})


@routes.get("/api/playlist/{pid}")
async def api_playlist(request):
    uid = user_id(request)
    pl = await own_playlist(request, uid)
    favs = await fav_ids(uid)
    tracks = [track_json(t, favs) for t in await db.playlist_tracks(pl["id"])]
    return web.json_response({"id": pl["id"], "name": pl["name"], "tracks": tracks})


@routes.delete("/api/playlist/{pid}")
async def api_playlist_delete(request):
    pl = await own_playlist(request, user_id(request))
    await db.delete_playlist(pl["id"])
    return web.json_response({"ok": True})


@routes.post("/api/playlist/{pid}/add/{tid}")
async def api_playlist_add(request):
    pl = await own_playlist(request, user_id(request))
    tid = int(request.match_info["tid"])
    if not await db.get_track(tid):
        raise web.HTTPNotFound()
    return web.json_response({"added": await db.add_to_playlist(pl["id"], tid)})


@routes.delete("/api/playlist/{pid}/track/{tid}")
async def api_playlist_remove(request):
    pl = await own_playlist(request, user_id(request))
    await db.remove_from_playlist(pl["id"], int(request.match_info["tid"]))
    return web.json_response({"ok": True})


# ── аудио ────────────────────────────────────────────────────────────────

def _prune_cache() -> None:
    """Оставляет самые свежие файлы: не больше CACHE_FILES штук и CACHE_MAX_MB мегабайт."""
    files = sorted(CACHE_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    total = 0
    for i, f in enumerate(files):
        total += f.stat().st_size
        if i and (i >= CACHE_FILES or total > CACHE_MAX_MB * 1024 * 1024):
            f.unlink(missing_ok=True)


async def cached_audio(track) -> Path:
    """Файл трека из кэша; при первом обращении скачивает его."""
    CACHE_DIR.mkdir(exist_ok=True)
    async with _locks.setdefault(track["id"], asyncio.Lock()):
        for path in CACHE_DIR.glob(f"{track['id']}.*"):
            os.utime(path)  # отмечаем как недавно использованный
            return path
        if track["url"].startswith("tg:"):
            # файл, который слушатель прислал боту: берём его из Telegram (Bot API — до 20 МБ)
            dst = CACHE_DIR / f"{track['id']}{Path(track['url']).suffix or '.mp3'}"
            await _app_bot.download(track["file_id"], destination=dst)
            _prune_cache()
            return dst
        src = await music.download(track["url"], f"{track['artist']} - {track['title']}")
        dst = CACHE_DIR / f"{track['id']}{src.suffix}"
        shutil.move(src, dst)
        _prune_cache()
        return dst


@routes.get("/api/stream/{tid}")
async def api_stream(request):
    uid = request.query.get("u", "")
    if not uid.isdigit() or not hmac.compare_digest(request.query.get("k", ""),
                                                    stream_key(int(uid))):
        raise web.HTTPUnauthorized()
    track = await db.get_track(int(request.match_info["tid"]))
    if not track:
        raise web.HTTPNotFound()
    try:
        path = await cached_audio(track)
    except Exception as e:
        log.warning("stream failed for %s: %s", track["url"], e)
        raise web.HTTPBadGateway()
    headers = {"Cache-Control": "private, max-age=3600"}
    if "dl" in request.query:
        headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(file_name(track, path))}"
    return web.FileResponse(path, headers=headers)


def file_name(track, path: Path) -> str:
    name = " - ".join(x for x in (track["artist"], track["title"]) if x)
    name = "".join(c for c in name if c not in '\\/:*?"<>|\r\n')[:120].strip() or "track"
    return name + path.suffix


@routes.post("/api/prepare/{tid}")
async def api_prepare(request):
    """Готовит файл к скачиванию и сообщает его имя."""
    user_id(request)
    track = await db.get_track(int(request.match_info["tid"]))
    if not track:
        raise web.HTTPNotFound()
    try:
        path = await cached_audio(track)
    except Exception as e:
        log.warning("prepare failed for %s: %s", track["url"], e)
        raise web.HTTPBadGateway()
    return web.json_response({"name": file_name(track, path)})


@routes.post("/api/send/{tid}")
async def api_send(request):
    """Отправляет трек файлом в чат с ботом — запасной способ скачивания."""
    uid = user_id(request)
    bot: Bot = request.app["bot"]
    track = await db.get_track(int(request.match_info["tid"]))
    if not track:
        raise web.HTTPNotFound()
    try:
        if track["file_id"]:
            try:
                await bot.send_audio(uid, track["file_id"])
                return web.json_response({"ok": True})
            except TelegramBadRequest:
                pass
        path = await cached_audio(track)
        art = await music.fetch_cover(track["cover"])
        sent = await bot.send_audio(
            uid, FSInputFile(path, filename=file_name(track, path)),
            title=track["title"][:64], performer=(track["artist"] or BRAND)[:64],
            duration=track["duration"] or None,
            thumbnail=BufferedInputFile(images.cover(track["title"], track["artist"], art),
                                        "cover.jpg"))
        media = sent.audio or sent.document
        if media:
            await db.set_file_id(track["id"], media.file_id)
    except Exception as e:
        log.warning("send failed for %s: %s", track["url"], e)
        raise web.HTTPBadGateway()
    return web.json_response({"ok": True})


# ── запуск ───────────────────────────────────────────────────────────────

@routes.get("/")
async def index(request):
    return web.FileResponse(BASE_DIR / "webapp" / "index.html",
                            headers={"Cache-Control": "no-cache"})


@routes.get("/api/logo")
async def api_logo(request):
    logo = request.app["logo"]
    if not logo:
        raise web.HTTPNotFound()
    return web.Response(body=logo, content_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=3600"})


async def start(bot: Bot, dp: Dispatcher, logo: bytes | None = None) -> web.AppRunner:
    app = web.Application()
    app["bot"] = bot
    global _app_bot
    _app_bot = bot
    app["logo"] = logo
    SimpleRequestHandler(dispatcher=dp, bot=bot,
                         secret_token=WEBHOOK_SECRET).register(app, path=WEBHOOK_PATH)
    app.add_routes(routes)
    runner = web.AppRunner(app)
    await runner.setup()
    if CACHE_DIR.exists():
        _prune_cache()  # после обновления лимитов кэш мог оказаться больше разрешённого
    await web.TCPSite(runner, "0.0.0.0", WEBAPP_PORT).start()
    log.info("mini app server listening on port %s", WEBAPP_PORT)
    return runner
