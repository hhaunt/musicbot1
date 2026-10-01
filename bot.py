import asyncio
import html
import logging
import sys

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BufferedInputFile, CallbackQuery, FSInputFile,
                           InlineKeyboardButton, InlineKeyboardMarkup,
                           InputMediaPhoto, Message)

import db
import images
import keyboards as kb
import music
from config import BOT_TOKEN, MAX_PLAYLISTS, PAGE_SIZE

log = logging.getLogger("mono")
router = Router()


class Form(StatesGroup):
    playlist_name = State()


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
    return await db.last_search(user_id)


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
        card = images.message_card("Избранное", "Здесь пока пусто.\nНажмите ☆ под треком, чтобы сохранить его.")
        await show(msg, card, "", kb.back_to_menu(), edit)
        return
    chunk, page, pages = paginate(tracks, page)
    start = page * PAGE_SIZE + 1
    card = images.list_card("Избранное", f"{len(tracks)} треков", track_rows(chunk), start)
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
    card = images.list_card(pl["name"], f"{len(tracks)} треков", track_rows(chunk), start)
    markup = kb.track_list([t["id"] for t in chunk], f"l{pid}", start, page, pages,
                           f"pl:{pid}", extra)
    await show(msg, card, "", markup, edit)


async def send_track(msg: Message, user_id: int, tid: int, ctx: str) -> bool:
    t = await db.get_track(tid)
    if not t:
        return False
    markup = kb.player(tid, ctx, await db.is_fav(user_id, tid))
    if t["file_id"]:
        try:
            await msg.answer_audio(t["file_id"], reply_markup=markup)
            return True
        except TelegramBadRequest:
            await db.set_file_id(tid, None)

    await msg.bot.send_chat_action(msg.chat.id, "upload_document")
    try:
        path = await music.download(t["url"])
    except Exception as e:
        log.warning("download failed for %s: %s", t["url"], e)
        await msg.answer(f"✕  Не удалось загрузить «{html.escape(t['title'])}».")
        return False
    try:
        sent = await msg.answer_audio(
            FSInputFile(path),
            title=t["title"][:64],
            performer=(t["artist"] or "MONO")[:64],
            duration=t["duration"] or None,
            thumbnail=BufferedInputFile(images.cover(t["title"], t["artist"]), "cover.jpg"),
            reply_markup=markup,
        )
        media = sent.audio or sent.document
        if media:
            await db.set_file_id(tid, media.file_id)
    finally:
        path.unlink(missing_ok=True)
    return True


# ── команды и текст ──────────────────────────────────────────────────────

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
    ids = [await db.upsert_track(f.url, f.title, f.artist, f.duration) for f in found]
    await db.set_last_search(msg.from_user.id, ids)
    rows = [(f.title, f.artist or "—", images.fmt_duration(f.duration)) for f in found]
    card = images.list_card("Поиск", query[:30], rows)
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
        card = images.message_card(pl["name"], "Удалить этот плейлист?\nТреки останутся в избранном и поиске.")
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
    await cq.answer("Добавлено в избранное" if now_fav else "Убрано из избранного")


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

async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if not BOT_TOKEN:
        sys.exit("Укажите BOT_TOKEN в файле .env (см. .env.example)")
    await db.init()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(router)
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
