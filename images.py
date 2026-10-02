"""Генерация чёрно-белых карточек интерфейса и обложек (Pillow)."""
import hashlib
import random
import urllib.request
from functools import lru_cache
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from config import BRAND

W = 1000
PAD = 70
BG = (0, 0, 0)
FG = (255, 255, 255)
DIM = (125, 125, 125)
LINE = (40, 40, 40)

_ASSETS = Path(__file__).parent / "assets"
# Можно положить свои шрифты в assets/regular.ttf и assets/bold.ttf
_REGULAR = [_ASSETS / "regular.ttf", "C:/Windows/Fonts/segoeui.ttf", "arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/System/Library/Fonts/Supplemental/Arial.ttf", "DejaVuSans.ttf"]
_BOLD = [_ASSETS / "bold.ttf", "C:/Windows/Fonts/segoeuib.ttf", "arialbd.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
         "/System/Library/Fonts/Supplemental/Arial Bold.ttf", "DejaVuSans-Bold.ttf"]


_FONT_URL = ("https://raw.githubusercontent.com/matplotlib/matplotlib/main/"
             "lib/matplotlib/mpl-data/fonts/ttf/")


def _fetch_font(bold: bool) -> Path | None:
    """На серверах часто нет шрифтов с кириллицей — один раз скачиваем DejaVu в assets/."""
    target = _ASSETS / ("bold.ttf" if bold else "regular.ttf")
    try:
        _ASSETS.mkdir(exist_ok=True)
        name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
        with urllib.request.urlopen(_FONT_URL + name, timeout=20) as r:
            target.write_bytes(r.read())
        return target
    except OSError:
        return None


@lru_cache(maxsize=64)
def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    for candidate in (_BOLD if bold else _REGULAR):
        try:
            return ImageFont.truetype(str(candidate), size)
        except OSError:
            continue
    fetched = _fetch_font(bold)
    if fetched:
        try:
            return ImageFont.truetype(str(fetched), size)
        except OSError:
            pass
    return ImageFont.load_default(size)  # без кириллицы — крайний случай


def _fit(d: ImageDraw.ImageDraw, text: str, font, max_w: int) -> str:
    if d.textlength(text, font=font) <= max_w:
        return text
    while text and d.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text.rstrip() + "…"


def _jpeg(img: Image.Image) -> bytes:
    buf = BytesIO()
    img.save(buf, "JPEG", quality=92)
    return buf.getvalue()


_logo: Image.Image | None = None
_logo_mask: Image.Image | None = None
LOGO = 36


def set_logo(data: bytes | None) -> None:
    """Логотип (аватарка бота) для шапки карточек; без него рисуется точка."""
    global _logo, _logo_mask
    if not data:
        return
    try:
        _logo = ImageOps.fit(Image.open(BytesIO(data)).convert("RGB"), (LOGO, LOGO), Image.LANCZOS)
    except Exception:
        _logo = None
        return
    big = Image.new("L", (LOGO * 4, LOGO * 4), 0)
    ImageDraw.Draw(big).ellipse((0, 0, LOGO * 4 - 1, LOGO * 4 - 1), fill=255)
    _logo_mask = big.resize((LOGO, LOGO), Image.LANCZOS)


def _mark(img: Image.Image, d: ImageDraw.ImageDraw, label: str = BRAND) -> None:
    if _logo:
        img.paste(_logo, (PAD, 50), _logo_mask)
        d.text((PAD + LOGO + 14, 54), label, font=_font(22, True), fill=FG)
    else:
        d.ellipse((PAD, 62, PAD + 12, 74), fill=FG)
        d.text((PAD + 26, 56), label, font=_font(22, True), fill=FG)


def _header(img: Image.Image, d: ImageDraw.ImageDraw, title: str, right: str = "") -> None:
    _mark(img, d)
    if right:
        f = _font(20)
        d.text((W - PAD - d.textlength(right, font=f), 57), right, font=f, fill=DIM)
    d.text((PAD, 104), _fit(d, title, _font(58, True), W - 2 * PAD), font=_font(58, True), fill=FG)


def _bars(d: ImageDraw.ImageDraw, seed: str, x0: int, x1: int, base: int, max_h: int) -> None:
    rnd = random.Random(seed)
    x, h = x0, max_h / 2
    while x < x1:
        h = min(max_h, max(6, h + rnd.uniform(-max_h, max_h) * 0.45))
        d.rectangle((x, base - h, x + 5, base), fill=FG if rnd.random() > 0.25 else DIM)
        x += 13


def fmt_duration(sec: int) -> str:
    if not sec:
        return ""
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def menu_card(user_name: str, fav_count: int, pl_count: int) -> bytes:
    img = Image.new("RGB", (W, 560), BG)
    d = ImageDraw.Draw(img)
    _mark(img, d, "PLAYER")
    f = _font(20)
    name = _fit(d, user_name, f, 400)
    d.text((W - PAD - d.textlength(name, font=f), 57), name, font=f, fill=DIM)

    size = 170  # уменьшаем надпись, пока название не поместится по ширине
    while size > 40 and d.textlength(BRAND, font=_font(size, True)) > W - 2 * PAD:
        size -= 6
    d.text((PAD - 4, 130 + (170 - size) // 2), BRAND, font=_font(size, True), fill=FG)
    _bars(d, "mono", PAD, W - PAD, 440, 90)
    d.line((PAD, 470, W - PAD, 470), fill=LINE, width=2)
    d.text((PAD, 492), f"лайки  {fav_count}", font=_font(22), fill=DIM)
    right = f"плейлисты  {pl_count}"
    d.text((W - PAD - d.textlength(right, font=_font(22)), 492), right, font=_font(22), fill=DIM)
    return _jpeg(img)


def message_card(title: str, text: str) -> bytes:
    img = Image.new("RGB", (W, 420), BG)
    d = ImageDraw.Draw(img)
    _header(img, d, title)
    d.line((PAD, 200, W - PAD, 200), fill=LINE, width=2)
    y = 236
    for line in text.split("\n"):
        d.text((PAD, y), _fit(d, line, _font(28), W - 2 * PAD), font=_font(28), fill=DIM)
        y += 44
    return _jpeg(img)


ROW_H = 78
THUMB = 56


def _rings(seed: str) -> Image.Image:
    """Уникальный узор из колец 640×640 — заглушка, когда у трека нет обложки."""
    s = 640  # рисуем крупно и уменьшаем — так линии получаются сглаженными
    img = Image.new("RGB", (s, s), BG)
    d = ImageDraw.Draw(img)
    rnd = random.Random(hashlib.md5(seed.encode()).hexdigest())
    cx, cy = rnd.randint(200, 440), rnd.randint(170, 330)
    for _ in range(rnd.randint(7, 12)):
        r = rnd.randint(40, 420)
        a0 = rnd.randint(0, 360)
        d.arc((cx - r, cy - r, cx + r, cy + r), a0, a0 + rnd.randint(60, 330),
              fill=FG if rnd.random() > 0.4 else DIM, width=rnd.choice((2, 3, 6)))
    r = rnd.randint(14, 34)
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=FG)
    return img


def _art(data: bytes | None, size: int, seed: str) -> Image.Image:
    """Настоящая обложка в своих цветах либо сгенерированный узор."""
    if data:
        try:
            img = Image.open(BytesIO(data)).convert("RGB")
            return ImageOps.fit(img, (size, size), Image.LANCZOS)
        except Exception:
            pass
    return _rings(seed).resize((size, size), Image.LANCZOS)


def _rows(img: Image.Image, d: ImageDraw.ImageDraw, y: int, rows: list[tuple[str, str, str]],
          start: int, thumbs: list[bytes | None] | None) -> None:
    text_x = PAD + (146 if thumbs is not None else 80)
    for i, (main, sub, side) in enumerate(rows):
        d.line((PAD, y, W - PAD, y), fill=LINE, width=2)
        d.text((PAD, y + 22), f"{start + i:02d}", font=_font(26, True), fill=DIM)
        if thumbs is not None:
            pos = (PAD + 66, y + (ROW_H - THUMB) // 2 + 1)
            img.paste(_art(thumbs[i], THUMB, f"{sub}|{main}"), pos)
            d.rectangle((pos[0], pos[1], pos[0] + THUMB - 1, pos[1] + THUMB - 1), outline=LINE)
        side_w = d.textlength(side, font=_font(22)) if side else 0
        max_w = W - PAD - text_x - side_w - 30
        d.text((text_x, y + 12), _fit(d, main, _font(27, True), max_w),
               font=_font(27, True), fill=FG)
        d.text((text_x, y + 46), _fit(d, sub, _font(20), max_w), font=_font(20), fill=DIM)
        if side:
            d.text((W - PAD - side_w, y + 26), side, font=_font(22), fill=DIM)
        y += ROW_H
    d.line((PAD, y, W - PAD, y), fill=LINE, width=2)


def list_card(title: str, right: str, rows: list[tuple[str, str, str]], start: int = 1,
              thumbs: list[bytes | None] | None = None) -> bytes:
    """rows: (заголовок, подпись, текст справа). start — номер первой строки.
    thumbs — обложки строк (None в списке = сгенерированная заглушка)."""
    img = Image.new("RGB", (W, 220 + ROW_H * len(rows) + 30), BG)
    d = ImageDraw.Draw(img)
    _header(img, d, title, right)
    _rows(img, d, 200, rows, start, thumbs)
    return _jpeg(img)


def album_card(title: str, artist: str, info: str, art: bytes | None,
               rows: list[tuple[str, str, str]], start: int = 1) -> bytes:
    """Экран альбома: крупная обложка, название и список треков."""
    img = Image.new("RGB", (W, 350 + ROW_H * len(rows) + 30), BG)
    d = ImageDraw.Draw(img)
    _mark(img, d)
    f = _font(20)
    d.text((W - PAD - d.textlength("альбом", font=f), 57), "альбом", font=f, fill=DIM)
    img.paste(_art(art, 200, f"{artist}|{title}"), (PAD, 110))
    d.rectangle((PAD, 110, PAD + 199, 309), outline=LINE)
    x, max_w = PAD + 236, W - 2 * PAD - 236
    d.text((x, 130), _fit(d, title, _font(46, True), max_w), font=_font(46, True), fill=FG)
    d.text((x, 196), _fit(d, artist, _font(28), max_w), font=_font(28), fill=FG)
    d.text((x, 244), _fit(d, info, _font(22), max_w), font=_font(22), fill=DIM)
    _rows(img, d, 330, rows, start, None)
    return _jpeg(img)


def cover(title: str, artist: str, art: bytes | None = None) -> bytes:
    """Обложка трека 320×320 для плеера Telegram."""
    if art:
        return _jpeg(_art(art, 320, f"{artist}|{title}"))
    img = _rings(f"{artist}|{title}")
    d = ImageDraw.Draw(img)
    s = img.width
    d.rectangle((0, 500, s, s), fill=BG)
    d.line((40, 500, s - 40, 500), fill=LINE, width=3)
    d.text((40, 522), _fit(d, title, _font(40, True), s - 80), font=_font(40, True), fill=FG)
    d.text((40, 576), _fit(d, artist, _font(28), s - 80), font=_font(28), fill=DIM)
    return _jpeg(img.resize((320, 320), Image.LANCZOS))
