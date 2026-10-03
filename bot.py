import asyncio
import html
import logging
import shutil
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BufferedInputFile, CallbackQuery, FSInputFile,
                           InlineKeyboardButton, InlineKeyboardMarkup,
                           InputMediaPhoto, MenuButtonWebApp, Message, WebAppInfo)

import db
import images
import keyboards as kb
import music
import releases
import webapp
from config import (ADMIN_IDS, BASE_DIR, BOT_TOKEN, BRAND, DB_PATH, MAX_PLAYLISTS, PAGE_SIZE,
                    TMP_DIR, WEBAPP_URL)

log = logging.getLogger("mono")
router = Router()


class Form(StatesGroup):
    playlist_name = State()
    album_query = State()


# ── вспомогательное ──────────────────────────────────────────────────────

def photo(data: bytes) -> BufferedInputFile:
    return BufferedInputFile(data, "card.jpg")


async def show(msg: Message, card: bytes, caption: str,
               markup: InlineKeyboardMarkup, edit: bool) -> None:
    """Показывает карточку: редактирует текущую, если это возможно, иначе шлёт новую."""
    if edit and msg.photo:
        try:
            await msg.edit_media(InputMediaPhoto(media=photo(card), caption=caption),
                                 reply_markup=markup)
            return
        except TelegramBadRequest:
            pass
    await msg.answer_photo(photo(card), caption=caption, reply_markup=markup)


def track_rows(tracks) -> list[tuple[str, str, str]]:
    return [(t["title"], t["artist"] or "—", images.fmt_duration(t["duration"]))
            for t in tracks]


async def ctx_tracks(user_id: int, ctx: str):
    """Очередь, из которой запущен трек: поиск, избранное или плейлист."""
    if ctx == "f":
        return await db.favorites(user_id)
    if ctx.startswith("l"):
        if await db.get_playlist(int(ctx[1:]), user_id):
            return await db.playlist_tracks(int(ctx[1:]))
        return []
    if ctx.startswith("a"):
        return await db.album_tracks(int(ctx[1:]))
    return await db.last_search(user_id)


async def thumbs_of(tracks) -> list[bytes | None]:
    return await music.fetch_covers([t["cover"] for t in tracks])


def paginate(items, page: int):
    pages = max(1, -(-len(items) // PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    return items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE], page, pages


# ── экраны ───────────────────────────────────────────────────────────────

async def show_menu(msg: Message, user, edit: bool) -> None:
    favs = await db.favorites(user.id)
    pls = await db.playlists(user.id)
    card = images.menu_card(user.first_name or "", len(favs), len(pls))
    await show(msg, card, "Отправьте название трека — я найду его.", kb.menu(), edit)


async def show_favorites(msg: Message, user_id: int, page: int, edit: bool) -> None:
    tracks = await db.favorites(user_id)
    if not tracks:
        card = images.message_card("Лайки", "Здесь пока пусто.\nНажмите ♡ под треком, чтобы сохранить его.")
        await show(msg, card, "", kb.back_to_menu(), edit)
        return
    chunk, page, pages = paginate(tracks, page)
    start = page * PAGE_SIZE + 1
    card = images.list_card("Лайки", f"{len(tracks)} треков", track_rows(chunk), start,
                            await thumbs_of(chunk))
    markup = kb.track_list([t["id"] for t in chunk], "f", start, page, pages, "fav")
    await show(msg, card, "", markup, edit)


async def show_playlists(msg: Message, user_id: int, edit: bool) -> None:
    items = await db.playlists(user_id)
    if items:
        rows = [(p["name"], f"{p['cnt']} треков", "") for p in items]
        card = images.list_card("Плейлисты", str(len(items)), rows)
    else:
        card = images.message_card("Плейлисты", "У вас ещё нет плейлистов.\nСоздайте первый кнопкой ниже.")
    await show(msg, card, "", kb.playlists(items), edit)


async def show_playlist(msg: Message, user_id: int, pid: int, page: int, edit: bool) -> None:
    pl = await db.get_playlist(pid, user_id)
    if not pl:
        await show_playlists(msg, user_id, edit)
        return
    tracks = await db.playlist_tracks(pid)
    extra = [InlineKeyboardButton(text="◁  Плейлисты", callback_data="pls"),
             InlineKeyboardButton(text="✕  Удалить", callback_data=f"pldel:{pid}")]
    if not tracks:
        card = images.message_card(pl["name"], "Плейлист пуст.\nДобавляйте треки кнопкой ＋ под ними.")
        await show(msg, card, "", kb.track_list([], f"l{pid}", extra=extra), edit)
        return
    chunk, page, pages = paginate(tracks, page)
    start = page * PAGE_SIZE + 1
    card = images.list_card(pl["name"], f"{len(tracks)} треков", track_rows(chunk), start,
                            await thumbs_of(chunk))
    markup = kb.track_list([t["id"] for t in chunk], f"l{pid}", start, page, pages,
                           f"pl:{pid}", extra)
    await show(msg, card, "", markup, edit)


async def show_album(msg: Message, album_id: int, page: int, edit: bool) -> bool:
    album = await db.get_album(album_id)
    if not album:  # первый показ — загружаем из Deezer и запоминаем
        try:
            a = await music.get_album(album_id)
        except Exception as e:
            log.warning("album %s failed: %s", album_id, e)
            return False
        ids = [await db.upsert_found(t)
               for t in a.tracks]
        await db.save_album(a.id, a.title, a.artist, a.year, a.cover, ids)
        album = await db.get_album(album_id)
    tracks = await db.album_tracks(album_id)
    chunk, page, pages = paginate(tracks, page)
    start = page * PAGE_SIZE + 1
    info = " · ".join(x for x in (album["year"], f"{len(tracks)} треков") if x)
    card = images.album_card(album["title"], album["artist"], info,
                             await music.fetch_cover(album["cover"]), track_rows(chunk), start)
    extra = [InlineKeyboardButton(text="＋  В плейлисты", callback_data=f"albsave:{album_id}"),
             InlineKeyboardButton(text="◁  Альбомы", callback_data="albums")]
    markup = kb.track_list([t["id"] for t in chunk], f"a{album_id}", start, page, pages,
                           f"alb:{album_id}", extra)
    await show(msg, card, "", markup, edit)
    return True


async def send_track(msg: Message, user_id: int, tid: int, ctx: str) -> bool:
    t = await db.get_track(tid)
    if not t:
        return False
    markup = kb.player(tid, ctx, await db.is_fav(user_id, tid))
    if t["url"].startswith(UPLOAD_PREFIX):
        # загруженный слушателем файл есть только в Telegram — пересылаем его, ссылку не трогаем
        try:
            await msg.answer_audio(t["file_id"], reply_markup=markup)
        except TelegramBadRequest:
            await msg.answer_document(t["file_id"], reply_markup=markup)
        return True
    if t["file_id"]:
        try:
            await msg.answer_audio(t["file_id"], reply_markup=markup)
            return True
        except TelegramBadRequest:
            await db.set_file_id(tid, None)

    await msg.bot.send_chat_action(msg.chat.id, "upload_document")
    try:
        path = await music.download(t["url"], f"{t['artist']} - {t['title']}")
    except Exception as e:
        log.warning("download failed for %s: %s", t["url"], e)
        reason = ("трек защищён правообладателем и недоступен для загрузки"
                  if isinstance(e, music.TrackProtected) else "не удалось загрузить")
        await msg.answer(f"✕  «{html.escape(t['title'])}»: {reason}.")
        return False
    try:
        sent = await msg.answer_audio(
            FSInputFile(path),
            title=t["title"][:64],
            performer=(t["artist"] or BRAND)[:64],
            duration=t["duration"] or None,
            thumbnail=BufferedInputFile(
                images.cover(t["title"], t["artist"], await music.fetch_cover(t["cover"])),
                "cover.jpg"),
            reply_markup=markup,
        )
        media = sent.audio or sent.document
        if media:
            await db.set_file_id(tid, media.file_id)
    finally:
        path.unlink(missing_ok=True)
    return True


# ── свои треки: аудиофайлы (в том числе FLAC), присланные боту ───────────

UPLOAD_PREFIX = "tg:"  # url таких треков: tg:<уникальный id файла в Telegram>.<расширение>
AUDIO_EXT = {".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav", ".aac"}
UPLOAD_STREAM_LIMIT = 20 * 1024 * 1024  # больше Bot API не даёт боту скачать файл


def _is_audio_document(msg: Message) -> bool:
    d = msg.document
    if not d:
        return False
    name = (d.file_name or "").lower()
    return (d.mime_type or "").startswith("audio/") or any(name.endswith(e) for e in AUDIO_EXT)


@router.message(F.audio | F.func(_is_audio_document))
async def on_audio_upload(msg: Message):
    """Сохраняет присланный файл как трек и сразу ставит ему лайк, чтобы он был в «Лайках»."""
    media = msg.audio or msg.document
    name = getattr(media, "file_name", None) or ""
    ext = (Path(name).suffix.lower() if name else "") or (
        ".flac" if "flac" in (media.mime_type or "") else ".mp3")
    stem = Path(name).stem if name else "Без названия"
    title = (getattr(media, "title", None) or stem)[:120]
    artist = (getattr(media, "performer", None) or "")[:120]
    tid = await db.upsert_track(f"{UPLOAD_PREFIX}{media.file_unique_id}{ext}", title, artist,
                                int(getattr(media, "duration", None) or 0))
    await db.set_file_id(tid, media.file_id)
    if not await db.is_fav(msg.from_user.id, tid):
        await db.toggle_fav(msg.from_user.id, tid)
    note = ""
    if (media.file_size or 0) > UPLOAD_STREAM_LIMIT:
        note = ("\nФайл больше 20 МБ: в чате он работает, а в мини-приложении Telegram "
                "не даст боту его прочитать — сожмите файл, если нужен там.")
    await msg.answer(f"♥︎  «{html.escape(title)}» добавлен в лайки.{note}")


# ── команды и текст ──────────────────────────────────────────────────────

@router.message(Command("stats"))
async def cmd_stats(msg: Message):
    if msg.from_user.id not in ADMIN_IDS:
        # отвечаем всегда — иначе непонятно, дошла ли команда и какой id вписывать
        await msg.answer(
            "Статистика доступна только администратору.\n"
            f"Ваш id: <code>{msg.from_user.id}</code>\n"
            f"Сейчас в ADMIN_IDS: <code>{', '.join(map(str, sorted(ADMIN_IDS))) or 'пусто'}</code>\n\n"
            f"Задайте переменную окружения <code>ADMIN_IDS={msg.from_user.id}</code> "
            "(только цифры, без кавычек) и перезапустите бота.")
        return
    try:
        s = await db.usage_stats()
    except Exception as e:
        log.exception("stats failed")
        await msg.answer(f"✕  Не удалось собрать статистику: <code>{html.escape(str(e))}</code>")
        return
    rows = [(title, f"за 7 дней: {s[key]['week']}  ·  за сутки: {s[key]['day']}", str(s[key]["total"]))
            for key, title in (("all", "Всего людей"), ("bot", "Чат с ботом"), ("app", "Мини-приложение"))]
    rows.append(("Прослушиваний в приложении", "за всё время", str(s["plays"])))
    await msg.answer_photo(photo(images.list_card("Статистика", "пользователи", rows)))


@router.message(CommandStart())
@router.message(Command("menu"))
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    await show_menu(msg, msg.from_user, edit=False)


@router.message(Form.playlist_name, F.text)
async def playlist_name(msg: Message, state: FSMContext):
    await state.clear()
    name = " ".join(msg.text.split())[:40]
    if name and not name.startswith("/"):
        await db.create_playlist(msg.from_user.id, name)
    await show_playlists(msg, msg.from_user.id, edit=False)


@router.message(Form.album_query, F.text & ~F.text.startswith("/"))
async def do_album_search(msg: Message, state: FSMContext):
    await state.clear()
    query = " ".join(msg.text.split())[:100]
    await msg.bot.send_chat_action(msg.chat.id, "typing")
    try:
        found = await music.search_albums(query, PAGE_SIZE)
    except Exception as e:
        log.warning("album search failed for %r: %s", query, e)
        found = []
    if not found:
        card = images.message_card("Альбомы", f"По запросу «{query}» ничего не найдено.")
        await show(msg, card, "", kb.albums([]), edit=False)
        return
    rows = [(a.title, a.artist or "—", f"{a.count} тр.") for a in found]
    thumbs = await music.fetch_covers([a.cover for a in found])
    card = images.list_card("Альбомы", query[:30], rows, thumbs=thumbs)
    await show(msg, card, "Выберите номер альбома.", kb.albums([a.id for a in found]),
               edit=False)


@router.message(F.text & ~F.text.startswith("/"))
async def do_search(msg: Message):
    query = " ".join(msg.text.split())[:100]
    await msg.bot.send_chat_action(msg.chat.id, "typing")
    try:
        found = await music.search(query, PAGE_SIZE)
    except Exception as e:
        log.warning("search failed for %r: %s", query, e)
        found = []
    if not found:
        card = images.message_card("Поиск", f"По запросу «{query}» ничего не найдено.")
        await show(msg, card, "", kb.back_to_menu(), edit=False)
        return
    ids = [await db.upsert_found(f)
           for f in found]
    await db.set_last_search(msg.from_user.id, ids)
    rows = [(f.title, f.artist or "—", images.fmt_duration(f.duration)) for f in found]
    thumbs = await music.fetch_covers([f.cover for f in found])
    card = images.list_card("Поиск", query[:30], rows, thumbs=thumbs)
    await show(msg, card, "Выберите номер трека.", kb.track_list(ids, "s"), edit=False)


# ── навигация ────────────────────────────────────────────────────────────

@router.callback_query(F.data == "noop")
async def cb_noop(cq: CallbackQuery):
    await cq.answer()


@router.callback_query(F.data == "menu")
async def cb_menu(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_menu(cq.message, cq.from_user, edit=True)
    await cq.answer()


@router.callback_query(F.data == "search")
async def cb_search(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    card = images.message_card("Поиск", "Отправьте сообщением название трека\nили имя исполнителя.")
    await show(cq.message, card, "", kb.back_to_menu(), edit=True)
    await cq.answer()


@router.callback_query(F.data == "albums")
async def cb_albums(cq: CallbackQuery, state: FSMContext):
    await state.set_state(Form.album_query)
    card = images.message_card("Альбомы", "Отправьте сообщением название альбома\nили имя исполнителя.")
    await show(cq.message, card, "", kb.back_to_menu(), edit=True)
    await cq.answer()


@router.callback_query(F.data.startswith("alb:"))
async def cb_album(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    _, aid, page = cq.data.split(":")
    if await show_album(cq.message, int(aid), int(page), edit=True):
        await cq.answer()
    else:
        await cq.answer("Не удалось открыть альбом", show_alert=True)


@router.callback_query(F.data.startswith("albsave:"))
async def cb_album_save(cq: CallbackQuery):
    album = await db.get_album(int(cq.data.split(":")[1]))
    if not album:
        await cq.answer("Альбом не найден")
        return
    if len(await db.playlists(cq.from_user.id)) >= MAX_PLAYLISTS:
        await cq.answer(f"Не больше {MAX_PLAYLISTS} плейлистов", show_alert=True)
        return
    pid = await db.create_playlist(cq.from_user.id, album["title"][:40])
    for t in await db.album_tracks(album["id"]):
        await db.add_to_playlist(pid, t["id"])
    await cq.answer("Альбом сохранён в плейлисты", show_alert=True)


@router.callback_query(F.data.startswith("fav:"))
async def cb_favorites(cq: CallbackQuery):
    await show_favorites(cq.message, cq.from_user.id, int(cq.data.split(":")[1]), edit=True)
    await cq.answer()


@router.callback_query(F.data == "pls")
async def cb_playlists(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_playlists(cq.message, cq.from_user.id, edit=True)
    await cq.answer()


@router.callback_query(F.data == "plnew")
async def cb_playlist_new(cq: CallbackQuery, state: FSMContext):
    if len(await db.playlists(cq.from_user.id)) >= MAX_PLAYLISTS:
        await cq.answer(f"Не больше {MAX_PLAYLISTS} плейлистов", show_alert=True)
        return
    await state.set_state(Form.playlist_name)
    card = images.message_card("Новый плейлист", "Отправьте сообщением название плейлиста.")
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="◁  Отмена", callback_data="pls")]])
    await show(cq.message, card, "", markup, edit=True)
    await cq.answer()


@router.callback_query(F.data.startswith("pl:"))
async def cb_playlist(cq: CallbackQuery):
    _, pid, page = cq.data.split(":")
    await show_playlist(cq.message, cq.from_user.id, int(pid), int(page), edit=True)
    await cq.answer()


@router.callback_query(F.data.startswith("pldel:"))
async def cb_playlist_delete(cq: CallbackQuery):
    pid = int(cq.data.split(":")[1])
    pl = await db.get_playlist(pid, cq.from_user.id)
    if pl:
        card = images.message_card(pl["name"], "Удалить этот плейлист?\nТреки останутся в лайках и поиске.")
        await show(cq.message, card, "", kb.confirm_delete(pid), edit=True)
    await cq.answer()


@router.callback_query(F.data.startswith("pldelok:"))
async def cb_playlist_delete_ok(cq: CallbackQuery):
    pid = int(cq.data.split(":")[1])
    if await db.get_playlist(pid, cq.from_user.id):
        await db.delete_playlist(pid)
    await show_playlists(cq.message, cq.from_user.id, edit=True)
    await cq.answer("Плейлист удалён")


# ── плеер ────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("play:"))
async def cb_play(cq: CallbackQuery):
    _, tid, ctx = cq.data.split(":")
    await cq.answer("Загружаю…")
    await send_track(cq.message, cq.from_user.id, int(tid), ctx)


@router.callback_query(F.data.startswith("nav:"))
async def cb_nav(cq: CallbackQuery):
    _, tid, ctx, step = cq.data.split(":")
    ids = [t["id"] for t in await ctx_tracks(cq.from_user.id, ctx)]
    if not ids:
        await cq.answer("Очередь пуста")
        return
    # если текущего трека в очереди уже нет — начинаем с начала
    nxt = ids[(ids.index(int(tid)) + int(step)) % len(ids)] if int(tid) in ids else ids[0]
    if nxt == int(tid):
        await cq.answer("В очереди только этот трек")
        return
    await cq.answer("Загружаю…")
    if await send_track(cq.message, cq.from_user.id, nxt, ctx):
        try:
            await cq.message.delete()
        except TelegramBadRequest:
            pass


@router.callback_query(F.data.startswith("like:"))
async def cb_like(cq: CallbackQuery):
    _, tid, ctx = cq.data.split(":")
    now_fav = await db.toggle_fav(cq.from_user.id, int(tid))
    await cq.message.edit_reply_markup(reply_markup=kb.player(int(tid), ctx, now_fav))
    await cq.answer("Лайк поставлен" if now_fav else "Лайк убран")


@router.callback_query(F.data.startswith("addm:"))
async def cb_add_menu(cq: CallbackQuery):
    _, tid, ctx = cq.data.split(":")
    pls = await db.playlists(cq.from_user.id)
    if not pls:
        await cq.answer("Сначала создайте плейлист: Меню → Плейлисты", show_alert=True)
        return
    await cq.message.edit_reply_markup(reply_markup=kb.add_to(int(tid), ctx, pls))
    await cq.answer()


@router.callback_query(F.data.startswith("addto:"))
async def cb_add_to(cq: CallbackQuery):
    _, tid, pid, ctx = cq.data.split(":")
    pl = await db.get_playlist(int(pid), cq.from_user.id)
    if not pl:
        await cq.answer("Плейлист не найден")
        return
    added = await db.add_to_playlist(int(pid), int(tid))
    markup = kb.player(int(tid), ctx, await db.is_fav(cq.from_user.id, int(tid)))
    await cq.message.edit_reply_markup(reply_markup=markup)
    await cq.answer(f"Добавлено в «{pl['name']}»" if added else "Уже в этом плейлисте")


@router.callback_query(F.data.startswith("back:"))
async def cb_back(cq: CallbackQuery):
    _, tid, ctx = cq.data.split(":")
    markup = kb.player(int(tid), ctx, await db.is_fav(cq.from_user.id, int(tid)))
    await cq.message.edit_reply_markup(reply_markup=markup)
    await cq.answer()


@router.callback_query(F.data.startswith("rm:"))
async def cb_remove(cq: CallbackQuery):
    _, tid, pid = cq.data.split(":")
    if await db.get_playlist(int(pid), cq.from_user.id):
        await db.remove_from_playlist(int(pid), int(tid))
    markup = kb.player(int(tid), "s", await db.is_fav(cq.from_user.id, int(tid)))
    await cq.message.edit_reply_markup(reply_markup=markup)
    await cq.answer("Убрано из плейлиста")


# ── запуск ───────────────────────────────────────────────────────────────

async def load_logo(bot: Bot) -> bytes | None:
    """Аватарка бота — она же логотип на карточках и в мини-приложении."""
    try:
        photos = await bot.get_user_profile_photos((await bot.me()).id, limit=1)
        if photos.photos:
            return (await bot.download(photos.photos[0][-1])).read()
    except Exception as e:
        log.warning("не удалось получить аватарку бота: %s", e)
    return None


async def keep_webhook(bot: Bot, dp: Dispatcher) -> None:
    """Ставит вебхук и следит, чтобы его никто не сбросил."""
    url = WEBAPP_URL + webapp.WEBHOOK_PATH
    first = True
    while True:
        try:
            if (await bot.get_webhook_info()).url != url:
                await bot.set_webhook(url, secret_token=webapp.WEBHOOK_SECRET,
                                      drop_pending_updates=first,
                                      allowed_updates=dp.resolve_used_update_types())
                log.info("webhook set to %s", url)
            elif first:
                log.info("webhook ok: %s", url)
        except Exception as e:
            log.warning("не удалось проверить вебхук: %s", e)
        first = False
        await asyncio.sleep(60)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if not BOT_TOKEN:
        sys.exit("Укажите BOT_TOKEN в файле .env (см. .env.example)")
    shutil.rmtree(TMP_DIR, ignore_errors=True)  # недокачанное с прошлого запуска
    log.info("запуск: база %s, мини-приложение %s", DB_PATH, WEBAPP_URL or "выключено")
    try:
        # если постоянная папка недоступна или зависает, бот всё равно должен подняться
        await asyncio.wait_for(db.init(), 30)
    except Exception as e:
        fallback = BASE_DIR / "mono.db"
        log.error("база в %s не открылась (%r) — временно использую %s; "
                  "данные там сотрутся при обновлении", DB_PATH, e, fallback)
        await db.init(fallback)
    log.info("база готова")
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(router)

    @dp.update.outer_middleware()
    async def track_usage(handler, event, data):
        user = data.get("event_from_user")
        if user and not user.is_bot:
            try:
                await db.touch_usage(user.id, "bot")
            except Exception as e:
                log.warning("usage tracking failed: %s", e)
        return await handler(event, data)
    runner = None
    try:
        logo = await load_logo(bot)
        images.set_logo(logo)
        log.info("бот @%s подключён к Telegram", (await bot.me()).username)
        watcher = asyncio.create_task(releases.watch(bot))  # noqa: F841 — держим ссылку на задачу
        if WEBAPP_URL:
            runner = await webapp.start(bot, dp, logo)
            try:
                await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(
                    text="Плеер", web_app=WebAppInfo(url=WEBAPP_URL)))
            except TelegramBadRequest as e:
                log.error("WEBAPP_URL %r отклонён Telegram: %s", WEBAPP_URL, e)
            # С публичным адресом работаем через вебхук: Telegram доставляет каждое
            # обновление ровно один раз, даже если где-то осталась вторая копия бота.
            await keep_webhook(bot, dp)
        else:
            await bot.delete_webhook(drop_pending_updates=True)
            await dp.start_polling(bot)
    finally:
        if runner:
            await runner.cleanup()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
