"""Уведомления о новых релизах исполнителей, на которых подписаны слушатели."""
import asyncio
import html
import logging
from datetime import date, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

import db
import music
from config import RELEASE_CHECK_HOURS, WEBAPP_URL

log = logging.getLogger("mono.releases")
FRESH_DAYS = 30  # о релизах старше не сообщаем: это переиздания или поздно добавленное


async def notify(bot: Bot, name: str, album: music.Album) -> None:
    buttons = [[InlineKeyboardButton(text="◎  Открыть альбом", callback_data=f"alb:{album.id}:0")]]
    if WEBAPP_URL:
        buttons.append([InlineKeyboardButton(text="▷  Открыть плеер",
                                             web_app=WebAppInfo(url=WEBAPP_URL))])
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    text = (f"Новый релиз\n<b>{html.escape(name)}</b> — «{html.escape(album.title)}»\n\n"
            "Уведомления отключаются в профиле мини-приложения.")
    for user_id in await db.artist_fans(name):
        try:
            if album.cover:
                await bot.send_photo(user_id, album.cover, caption=text, reply_markup=markup)
            else:
                await bot.send_message(user_id, text, reply_markup=markup)
        except TelegramAPIError as e:  # человек заблокировал бота и т. п.
            log.info("release notice to %s not delivered: %s", user_id, e)
        await asyncio.sleep(0.1)


async def check_artist(bot: Bot, name: str) -> None:
    watch = await db.get_watch(name)
    if watch and watch["deezer_id"] == -1:
        return
    found = await music.latest_release(name, watch["deezer_id"] if watch else 0)
    if found is None:
        await db.save_watch(name, -1, 0, "")
        return
    deezer_id, album = found
    if album is None:
        await db.save_watch(name, deezer_id, 0, "")
        return
    fresh = album.year >= (date.today() - timedelta(days=FRESH_DAYS)).isoformat()
    # при первой проверке только запоминаем текущий релиз, не уведомляя о нём
    if watch and watch["deezer_id"] > 0 and album.id != watch["last_album"] \
            and album.year > watch["last_date"] and fresh:
        log.info("new release: %s — %s", name, album.title)
        await notify(bot, name, album)
    await db.save_watch(name, deezer_id, album.id, album.year)


async def watch(bot: Bot) -> None:
    await asyncio.sleep(60)
    while True:
        try:
            for name in await db.watched_artists():
                try:
                    await check_artist(bot, name)
                except Exception as e:
                    log.warning("release check failed for %s: %s", name, e)
                await asyncio.sleep(0.5)  # бережём лимиты Deezer
        except Exception as e:
            log.warning("release watcher: %s", e)
        await asyncio.sleep(RELEASE_CHECK_HOURS * 3600)
