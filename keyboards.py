"""Инлайн-клавиатуры. Только монохромные глифы — без цветных эмодзи.

Контекст очереди (ctx) в callback_data: s — поиск, f — избранное,
l<ID> — плейлист, a<ID> — альбом.
"""
from aiogram.types import InlineKeyboardButton as Btn
from aiogram.types import InlineKeyboardMarkup, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import WEBAPP_URL

HEART = "♥︎"  # с селектором «текстовый вид», чтобы Telegram не рисовал цветной эмодзи
MENU = Btn(text="☰  Меню", callback_data="menu")


def menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(Btn(text="⌕  Поиск", callback_data="search"))
    b.row(Btn(text="◎  Альбомы", callback_data="albums"),
          Btn(text=f"{HEART}  Лайки", callback_data="fav:0"))
    b.row(Btn(text="≡  Плейлисты", callback_data="pls"))
    if WEBAPP_URL:
        b.row(Btn(text="▷  Открыть плеер", web_app=WebAppInfo(url=WEBAPP_URL)))
    return b.as_markup()


def albums(album_ids: list[int]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for i, aid in enumerate(album_ids, 1):
        b.add(Btn(text=f"{i:02d}", callback_data=f"alb:{aid}:0"))
    b.adjust(4)
    b.row(Btn(text="⌕  Искать ещё", callback_data="albums"), MENU)
    return b.as_markup()


def back_to_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[MENU]])


def track_list(track_ids: list[int], ctx: str, start: int = 1, page: int = 0,
               pages: int = 1, page_cb: str = "", extra: list[Btn] | None = None
               ) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for i, tid in enumerate(track_ids):
        b.add(Btn(text=f"{start + i:02d}", callback_data=f"play:{tid}:{ctx}"))
    b.adjust(4)
    if pages > 1:
        b.row(Btn(text="◁", callback_data=f"{page_cb}:{(page - 1) % pages}"),
              Btn(text=f"{page + 1} / {pages}", callback_data="noop"),
              Btn(text="▷", callback_data=f"{page_cb}:{(page + 1) % pages}"))
    if extra:
        b.row(*extra)
    b.row(MENU)
    return b.as_markup()


def player(tid: int, ctx: str, is_fav: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(Btn(text="◁", callback_data=f"nav:{tid}:{ctx}:-1"),
          Btn(text=HEART if is_fav else "♡", callback_data=f"like:{tid}:{ctx}"),
          Btn(text="＋", callback_data=f"addm:{tid}:{ctx}"),
          Btn(text="▷", callback_data=f"nav:{tid}:{ctx}:1"))
    if ctx.startswith("l"):
        b.row(Btn(text="－  Убрать из плейлиста", callback_data=f"rm:{tid}:{ctx[1:]}"))
    b.row(MENU)
    return b.as_markup()


def add_to(tid: int, ctx: str, playlists) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for p in playlists:
        b.row(Btn(text=f"＋  {p['name']}", callback_data=f"addto:{tid}:{p['id']}:{ctx}"))
    b.row(Btn(text="◁  Назад", callback_data=f"back:{tid}:{ctx}"))
    return b.as_markup()


def playlists(items) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for i, p in enumerate(items, 1):
        b.add(Btn(text=f"{i:02d}", callback_data=f"pl:{p['id']}:0"))
    b.adjust(4)
    b.row(Btn(text="＋  Новый плейлист", callback_data="plnew"))
    b.row(MENU)
    return b.as_markup()


def confirm_delete(pid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        Btn(text="✕  Удалить", callback_data=f"pldelok:{pid}"),
        Btn(text="◁  Отмена", callback_data=f"pl:{pid}:0"),
    ]])
