"""Поиск и загрузка аудио через yt-dlp."""
import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path

from yt_dlp import YoutubeDL

from config import MAX_FILE_SIZE, SEARCH_SOURCE, TMP_DIR


@dataclass
class Found:
    url: str
    title: str
    artist: str
    duration: int


def _search(query: str, limit: int) -> list[Found]:
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True}
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"{SEARCH_SOURCE}{limit}:{query}", download=False)
    out = []
    for e in (info or {}).get("entries") or []:
        if not e:
            continue
        url = e.get("webpage_url") or e.get("url")
        title = e.get("title")
        if not url or not title:
            continue
        artist = e.get("uploader") or e.get("channel") or e.get("artist") or ""
        out.append(Found(url, title.strip(), artist.strip(), int(e.get("duration") or 0)))
    return out


def _download(url: str) -> Path:
    TMP_DIR.mkdir(exist_ok=True)
    has_ffmpeg = shutil.which("ffmpeg") is not None
    opts = {
        "quiet": True,
        "no_warnings": True,
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
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
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


async def download(url: str) -> Path:
    return await asyncio.to_thread(_download, url)
