import time

import aiosqlite

from config import DB_PATH

_db: aiosqlite.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    artist TEXT NOT NULL DEFAULT '',
    duration INTEGER NOT NULL DEFAULT 0,
    file_id TEXT
);
CREATE TABLE IF NOT EXISTS favorites (
    user_id INTEGER NOT NULL,
    track_id INTEGER NOT NULL,
    added INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    PRIMARY KEY (user_id, track_id)
);
CREATE TABLE IF NOT EXISTS playlists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS playlist_tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    playlist_id INTEGER NOT NULL,
    track_id INTEGER NOT NULL,
    UNIQUE (playlist_id, track_id)
);
CREATE TABLE IF NOT EXISTS last_search (
    user_id INTEGER NOT NULL,
    pos INTEGER NOT NULL,
    track_id INTEGER NOT NULL,
    PRIMARY KEY (user_id, pos)
);
CREATE TABLE IF NOT EXISTS albums (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    artist TEXT NOT NULL DEFAULT '',
    year TEXT NOT NULL DEFAULT '',
    cover TEXT
);
CREATE TABLE IF NOT EXISTS album_tracks (
    album_id INTEGER NOT NULL,
    pos INTEGER NOT NULL,
    track_id INTEGER NOT NULL,
    PRIMARY KEY (album_id, pos)
);
CREATE TABLE IF NOT EXISTS plays (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    track_id INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS plays_user ON plays (user_id, id);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    username TEXT,
    photo TEXT,
    public INTEGER NOT NULL DEFAULT 0
);
-- подписки и лайки: rel = follow | like, kind = artist | user,
-- target = имя исполнителя или id пользователя
CREATE TABLE IF NOT EXISTS social (
    user_id INTEGER NOT NULL,
    rel TEXT NOT NULL,
    kind TEXT NOT NULL,
    target TEXT NOT NULL COLLATE NOCASE,
    PRIMARY KEY (user_id, rel, kind, target)
);
CREATE INDEX IF NOT EXISTS social_target ON social (rel, kind, target);
-- сколько секунд каждый человек реально слушал каждый трек
CREATE TABLE IF NOT EXISTS listen_time (
    user_id INTEGER NOT NULL,
    track_id INTEGER NOT NULL,
    seconds INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, track_id)
);
-- последний известный релиз исполнителя — чтобы уведомлять о новых.
-- deezer_id: 0 — ещё не искали, -1 — в каталоге не найден
CREATE TABLE IF NOT EXISTS artist_watch (
    name TEXT PRIMARY KEY COLLATE NOCASE,
    deezer_id INTEGER NOT NULL DEFAULT 0,
    last_album INTEGER NOT NULL DEFAULT 0,
    last_date TEXT NOT NULL DEFAULT ''
);
-- комментарии к трекам; at — секунда трека, к которой относится комментарий (NULL — ко всему треку)
CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    at INTEGER,
    created INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS comments_track ON comments (track_id);
-- кто и когда пользовался: source = bot (чат с ботом) | app (мини-приложение)
CREATE TABLE IF NOT EXISTS usage (
    user_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    first_seen INTEGER NOT NULL,
    last_seen INTEGER NOT NULL,
    PRIMARY KEY (user_id, source)
);
"""

# Те, кто пользовался до появления статистики: их видно по сохранённым данным.
# Время визита неизвестно (0), поэтому они попадают только в «всего».
BACKFILL = """
INSERT OR IGNORE INTO usage SELECT DISTINCT user_id, 'bot', 0, 0 FROM last_search;
INSERT OR IGNORE INTO usage SELECT DISTINCT user_id, 'bot', 0, 0 FROM favorites;
INSERT OR IGNORE INTO usage SELECT DISTINCT user_id, 'bot', 0, 0 FROM playlists;
INSERT OR IGNORE INTO usage SELECT DISTINCT user_id, 'app', 0, 0 FROM plays;
INSERT OR IGNORE INTO usage SELECT id, 'app', 0, 0 FROM users;
"""


async def init() -> None:
    global _db
    _db = await aiosqlite.connect(DB_PATH)
    _db.row_factory = aiosqlite.Row
    await _db.executescript(SCHEMA)
    # базы, созданные до появления обложек
    columns = [r["name"] for r in await _all("PRAGMA table_info(tracks)")]
    if "cover" not in columns:
        await _db.execute("ALTER TABLE tracks ADD COLUMN cover TEXT")
    if "genre" not in columns:  # NULL — ещё не узнавали, '' — узнать не удалось
        await _db.execute("ALTER TABLE tracks ADD COLUMN genre TEXT")
    if "notify" not in [r["name"] for r in await _all("PRAGMA table_info(users)")]:
        await _db.execute("ALTER TABLE users ADD COLUMN notify INTEGER NOT NULL DEFAULT 1")
    await _db.executescript(BACKFILL)
    await _db.commit()


async def close() -> None:
    if _db:
        await _db.close()


async def _all(sql: str, *args):
    async with _db.execute(sql, args) as cur:
        return await cur.fetchall()


async def _one(sql: str, *args):
    async with _db.execute(sql, args) as cur:
        return await cur.fetchone()


# ── статистика использования ─────────────────────────────────────────────

_touched: dict[tuple[int, str], float] = {}


async def touch_usage(user_id: int, source: str) -> None:
    """Отмечает визит; одного и того же человека пишет в базу не чаще раза в 10 минут."""
    now = time.time()
    if now - _touched.get((user_id, source), 0) < 600:
        return
    if len(_touched) > 10000:
        _touched.clear()
    _touched[(user_id, source)] = now
    await _db.execute(
        "INSERT INTO usage (user_id, source, first_seen, last_seen) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(user_id, source) DO UPDATE SET last_seen = excluded.last_seen, "
        "first_seen = CASE WHEN first_seen = 0 THEN excluded.first_seen ELSE first_seen END",
        (user_id, source, int(now), int(now)))
    await _db.commit()


async def usage_stats() -> dict:
    """Число людей: всего, за 7 дней и за сутки — по боту, приложению и в сумме."""
    now = int(time.time())
    out = {}
    for key, where in (("bot", "source = 'bot'"), ("app", "source = 'app'"), ("all", "1 = 1")):
        row = await _one(
            f"SELECT COUNT(DISTINCT user_id) AS total, "
            f"COUNT(DISTINCT CASE WHEN last_seen >= ? THEN user_id END) AS week, "
            f"COUNT(DISTINCT CASE WHEN last_seen >= ? THEN user_id END) AS day "
            f"FROM usage WHERE {where}", now - 7 * 86400, now - 86400)
        out[key] = {"total": row["total"], "week": row["week"], "day": row["day"]}
    out["plays"] = (await _one("SELECT COUNT(*) AS n FROM plays"))["n"]
    return out


# ── треки ────────────────────────────────────────────────────────────────

async def upsert_track(url: str, title: str, artist: str, duration: int,
                       cover: str | None = None, genre: str | None = None) -> int:
    await _db.execute(
        "INSERT INTO tracks (url, title, artist, duration, cover, genre) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(url) DO UPDATE SET title=excluded.title, artist=excluded.artist, "
        "duration=excluded.duration, cover=COALESCE(excluded.cover, cover), "
        "genre=COALESCE(excluded.genre, genre)",
        (url, title, artist, duration, cover, genre or None),
    )
    await _db.commit()
    row = await _one("SELECT id FROM tracks WHERE url = ?", url)
    return row["id"]


async def get_track(track_id: int):
    return await _one("SELECT * FROM tracks WHERE id = ?", track_id)


async def set_file_id(track_id: int, file_id: str | None) -> None:
    await _db.execute("UPDATE tracks SET file_id = ? WHERE id = ?", (file_id, track_id))
    await _db.commit()


# ── последний поиск ──────────────────────────────────────────────────────

async def set_last_search(user_id: int, track_ids: list[int]) -> None:
    await _db.execute("DELETE FROM last_search WHERE user_id = ?", (user_id,))
    await _db.executemany(
        "INSERT INTO last_search (user_id, pos, track_id) VALUES (?, ?, ?)",
        [(user_id, i, tid) for i, tid in enumerate(track_ids)],
    )
    await _db.commit()


async def last_search(user_id: int):
    return await _all(
        "SELECT t.* FROM last_search s JOIN tracks t ON t.id = s.track_id "
        "WHERE s.user_id = ? ORDER BY s.pos",
        user_id,
    )


# ── прослушивания и вкус ─────────────────────────────────────────────────

async def add_play(user_id: int, track_id: int) -> None:
    await _db.execute("INSERT INTO plays (user_id, track_id) VALUES (?, ?)", (user_id, track_id))
    await _db.commit()


async def play_count(user_id: int) -> int:
    row = await _one("SELECT COUNT(*) AS n FROM plays WHERE user_id = ?", user_id)
    return row["n"]


async def taste(user_id: int) -> list[tuple[str, str, int]]:
    """(название, исполнитель, вес): избранное весит больше плейлистов и прослушиваний."""
    favs = await _all(
        "SELECT t.title, t.artist FROM favorites f JOIN tracks t ON t.id = f.track_id "
        "WHERE f.user_id = ?", user_id)
    lists = await _all(
        "SELECT t.title, t.artist FROM playlists p "
        "JOIN playlist_tracks pt ON pt.playlist_id = p.id JOIN tracks t ON t.id = pt.track_id "
        "WHERE p.user_id = ?", user_id)
    plays = await _all(
        "SELECT t.title, t.artist FROM plays p JOIN tracks t ON t.id = p.track_id "
        "WHERE p.user_id = ? ORDER BY p.id DESC LIMIT 200", user_id)
    return ([(r["title"], r["artist"], 3) for r in favs]
            + [(r["title"], r["artist"], 2) for r in lists]
            + [(r["title"], r["artist"], 1) for r in plays])


async def history(user_id: int, limit: int = 50):
    """Недавно прослушанные треки без повторов, свежие сверху."""
    return await _all(
        "SELECT t.* FROM tracks t JOIN (SELECT track_id, MAX(id) AS last FROM plays "
        "WHERE user_id = ? GROUP BY track_id) p ON p.track_id = t.id "
        "ORDER BY p.last DESC LIMIT ?", user_id, limit)


# ── время прослушивания ──────────────────────────────────────────────────

async def add_listen(user_id: int, track_id: int, seconds: int) -> None:
    await _db.execute(
        "INSERT INTO listen_time (user_id, track_id, seconds) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id, track_id) DO UPDATE SET seconds = seconds + excluded.seconds",
        (user_id, track_id, seconds))
    await _db.commit()


async def listen_rows(user_id: int):
    return await _all(
        "SELECT t.id, t.title, t.artist, t.genre, l.seconds FROM listen_time l "
        "JOIN tracks t ON t.id = l.track_id WHERE l.user_id = ? ORDER BY l.seconds DESC", user_id)


async def listen_total(user_id: int) -> int:
    row = await _one("SELECT COALESCE(SUM(seconds), 0) AS n FROM listen_time WHERE user_id = ?",
                     user_id)
    return row["n"]


async def set_genre(track_id: int, genre: str) -> None:
    await _db.execute("UPDATE tracks SET genre = ? WHERE id = ?", (genre, track_id))
    await _db.commit()


# ── комментарии ──────────────────────────────────────────────────────────

async def add_comment(track_id: int, user_id: int, text: str, at: int | None) -> int:
    cur = await _db.execute(
        "INSERT INTO comments (track_id, user_id, text, at, created) VALUES (?, ?, ?, ?, ?)",
        (track_id, user_id, text, at, int(time.time())))
    await _db.commit()
    return cur.lastrowid


async def comments(track_id: int):
    """Комментарии трека с именем и аватаркой автора."""
    return await _all(
        "SELECT c.*, u.name, u.photo FROM comments c LEFT JOIN users u ON u.id = c.user_id "
        "WHERE c.track_id = ? ORDER BY c.id DESC LIMIT 300", track_id)


async def get_comment(comment_id: int):
    return await _one(
        "SELECT c.*, u.name, u.photo FROM comments c LEFT JOIN users u ON u.id = c.user_id "
        "WHERE c.id = ?", comment_id)


async def comment_count(track_id: int, user_id: int) -> int:
    row = await _one("SELECT COUNT(*) AS n FROM comments WHERE track_id = ? AND user_id = ?",
                     track_id, user_id)
    return row["n"]


async def delete_comment(comment_id: int) -> None:
    await _db.execute("DELETE FROM comments WHERE id = ?", (comment_id,))
    await _db.commit()


# ── слежение за релизами любимых исполнителей ────────────────────────────

async def watched_artists() -> list[str]:
    """Все исполнители, на которых кто-то подписан или кому поставили лайк."""
    rows = await _all("SELECT DISTINCT target FROM social WHERE kind = 'artist'")
    return [r["target"] for r in rows]


async def artist_fans(name: str) -> list[int]:
    """Кого уведомлять о релизе: подписчики и лайкнувшие, не отключившие уведомления."""
    rows = await _all(
        "SELECT DISTINCT s.user_id FROM social s JOIN users u ON u.id = s.user_id "
        "WHERE s.kind = 'artist' AND s.target = ? AND u.notify = 1", name)
    return [r["user_id"] for r in rows]


async def get_watch(name: str):
    return await _one("SELECT * FROM artist_watch WHERE name = ?", name)


async def save_watch(name: str, deezer_id: int, last_album: int, last_date: str) -> None:
    await _db.execute(
        "INSERT OR REPLACE INTO artist_watch (name, deezer_id, last_album, last_date) "
        "VALUES (?, ?, ?, ?)", (name, deezer_id, last_album, last_date))
    await _db.commit()


async def set_notify(user_id: int, notify: bool) -> None:
    await _db.execute("UPDATE users SET notify = ? WHERE id = ?", (int(notify), user_id))
    await _db.commit()


# ── пользователи, подписки и лайки ───────────────────────────────────────

async def upsert_user(user_id: int, name: str, username: str | None, photo: str | None,
                      public_default: bool) -> None:
    await _db.execute(
        "INSERT INTO users (id, name, username, photo, public) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name, username=excluded.username, "
        "photo=excluded.photo",
        (user_id, name, username, photo, int(public_default)))
    await _db.commit()


async def get_user(user_id: int):
    return await _one("SELECT * FROM users WHERE id = ?", user_id)


async def set_public(user_id: int, public: bool) -> None:
    await _db.execute("UPDATE users SET public = ? WHERE id = ?", (int(public), user_id))
    await _db.commit()


async def public_users(exclude_id: int):
    return await _all(
        "SELECT u.*, (SELECT COUNT(*) FROM social s WHERE s.rel = 'follow' AND s.kind = 'user' "
        "AND s.target = CAST(u.id AS TEXT)) AS followers FROM users u "
        "WHERE u.public = 1 AND u.id != ? ORDER BY followers DESC, u.id LIMIT 500", exclude_id)


async def has_social(user_id: int, rel: str, kind: str, target: str) -> bool:
    return await _one(
        "SELECT 1 FROM social WHERE user_id = ? AND rel = ? AND kind = ? AND target = ?",
        user_id, rel, kind, target) is not None


async def toggle_social(user_id: int, rel: str, kind: str, target: str) -> bool:
    """Возвращает новое состояние: True — подписка или лайк теперь стоит."""
    if await has_social(user_id, rel, kind, target):
        await _db.execute(
            "DELETE FROM social WHERE user_id = ? AND rel = ? AND kind = ? AND target = ?",
            (user_id, rel, kind, target))
        await _db.commit()
        return False
    await _db.execute(
        "INSERT INTO social (user_id, rel, kind, target) VALUES (?, ?, ?, ?)",
        (user_id, rel, kind, target))
    await _db.commit()
    return True


async def count_social(rel: str, kind: str, target: str) -> int:
    row = await _one(
        "SELECT COUNT(*) AS n FROM social WHERE rel = ? AND kind = ? AND target = ?",
        rel, kind, target)
    return row["n"]


async def social_targets(user_id: int, rel: str, kind: str) -> list[str]:
    """На кого человек подписан (rel=follow) или кого лайкнул (rel=like)."""
    rows = await _all(
        "SELECT target FROM social WHERE user_id = ? AND rel = ? AND kind = ? "
        "ORDER BY rowid DESC", user_id, rel, kind)
    return [r["target"] for r in rows]


async def following(user_id: int, kind: str) -> list[str]:
    return await social_targets(user_id, "follow", kind)


# ── альбомы ──────────────────────────────────────────────────────────────

async def save_album(album_id: int, title: str, artist: str, year: str,
                     cover: str | None, track_ids: list[int]) -> None:
    await _db.execute(
        "INSERT OR REPLACE INTO albums (id, title, artist, year, cover) VALUES (?, ?, ?, ?, ?)",
        (album_id, title, artist, year, cover),
    )
    await _db.execute("DELETE FROM album_tracks WHERE album_id = ?", (album_id,))
    await _db.executemany(
        "INSERT INTO album_tracks (album_id, pos, track_id) VALUES (?, ?, ?)",
        [(album_id, i, tid) for i, tid in enumerate(track_ids)],
    )
    await _db.commit()


async def get_album(album_id: int):
    return await _one("SELECT * FROM albums WHERE id = ?", album_id)


async def album_tracks(album_id: int):
    return await _all(
        "SELECT t.* FROM album_tracks a JOIN tracks t ON t.id = a.track_id "
        "WHERE a.album_id = ? ORDER BY a.pos",
        album_id,
    )


# ── избранное ────────────────────────────────────────────────────────────

async def is_fav(user_id: int, track_id: int) -> bool:
    row = await _one(
        "SELECT 1 FROM favorites WHERE user_id = ? AND track_id = ?", user_id, track_id
    )
    return row is not None


async def toggle_fav(user_id: int, track_id: int) -> bool:
    """Возвращает новое состояние: True — трек теперь в избранном."""
    if await is_fav(user_id, track_id):
        await _db.execute(
            "DELETE FROM favorites WHERE user_id = ? AND track_id = ?", (user_id, track_id)
        )
        await _db.commit()
        return False
    await _db.execute(
        "INSERT INTO favorites (user_id, track_id) VALUES (?, ?)", (user_id, track_id)
    )
    await _db.commit()
    return True


async def favorites(user_id: int):
    return await _all(
        "SELECT t.* FROM favorites f JOIN tracks t ON t.id = f.track_id "
        "WHERE f.user_id = ? ORDER BY f.added DESC, f.rowid DESC",
        user_id,
    )


# ── плейлисты ────────────────────────────────────────────────────────────

async def create_playlist(user_id: int, name: str) -> int:
    cur = await _db.execute(
        "INSERT INTO playlists (user_id, name) VALUES (?, ?)", (user_id, name)
    )
    await _db.commit()
    return cur.lastrowid


async def playlists(user_id: int):
    return await _all(
        "SELECT p.id, p.name, COUNT(pt.id) AS cnt FROM playlists p "
        "LEFT JOIN playlist_tracks pt ON pt.playlist_id = p.id "
        "WHERE p.user_id = ? GROUP BY p.id ORDER BY p.id",
        user_id,
    )


async def get_playlist(playlist_id: int, user_id: int):
    return await _one(
        "SELECT * FROM playlists WHERE id = ? AND user_id = ?", playlist_id, user_id
    )


async def playlist_tracks(playlist_id: int):
    return await _all(
        "SELECT t.* FROM playlist_tracks pt JOIN tracks t ON t.id = pt.track_id "
        "WHERE pt.playlist_id = ? ORDER BY pt.id",
        playlist_id,
    )


async def add_to_playlist(playlist_id: int, track_id: int) -> bool:
    """False, если трек уже был в плейлисте."""
    cur = await _db.execute(
        "INSERT OR IGNORE INTO playlist_tracks (playlist_id, track_id) VALUES (?, ?)",
        (playlist_id, track_id),
    )
    await _db.commit()
    return cur.rowcount > 0


async def remove_from_playlist(playlist_id: int, track_id: int) -> None:
    await _db.execute(
        "DELETE FROM playlist_tracks WHERE playlist_id = ? AND track_id = ?",
        (playlist_id, track_id),
    )
    await _db.commit()


async def delete_playlist(playlist_id: int) -> None:
    await _db.execute("DELETE FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,))
    await _db.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))
    await _db.commit()
