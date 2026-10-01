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
"""


async def init() -> None:
    global _db
    _db = await aiosqlite.connect(DB_PATH)
    _db.row_factory = aiosqlite.Row
    await _db.executescript(SCHEMA)
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


# ── треки ────────────────────────────────────────────────────────────────

async def upsert_track(url: str, title: str, artist: str, duration: int) -> int:
    await _db.execute(
        "INSERT INTO tracks (url, title, artist, duration) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(url) DO UPDATE SET title=excluded.title, artist=excluded.artist, "
        "duration=excluded.duration",
        (url, title, artist, duration),
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
