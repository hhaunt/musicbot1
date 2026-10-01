"""Генерация чёрно-белых карточек интерфейса и обложек (Pillow)."""
import hashlib
import random
from functools import lru_cache
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

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


@lru_cache(maxsize=64)
def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    for candidate in (_BOLD if bold else _REGULAR):
        try:
            return ImageFont.truetype(str(candidate), size)
        except OSError:
            continue
    return ImageFont.load_default(size)


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


def _header(d: ImageDraw.ImageDraw, title: str, right: str = "") -> None:
    d.ellipse((PAD, 62, PAD + 12, 74), fill=FG)
    d.text((PAD + 26, 56), "MONO", font=_font(22, True), fill=FG)
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
    d.ellipse((PAD, 62, PAD + 12, 74), fill=FG)
    d.text((PAD + 26, 56), "PLAYER", font=_font(22, True), fill=FG)
    f = _font(20)
    name = _fit(d, user_name, f, 400)
    d.text((W - PAD - d.textlength(name, font=f), 57), name, font=f, fill=DIM)

    d.text((PAD - 6, 120), "MONO", font=_font(170, True), fill=FG)
    _bars(d, "mono", PAD, W - PAD, 440, 90)
    d.line((PAD, 470, W - PAD, 470), fill=LINE, width=2)
    d.text((PAD, 492), f"избранное  {fav_count}", font=_font(22), fill=DIM)
    right = f"плейлисты  {pl_count}"
    d.text((W - PAD - d.textlength(right, font=_font(22)), 492), right, font=_font(22), fill=DIM)
    return _jpeg(img)


def message_card(title: str, text: str) -> bytes:
    img = Image.new("RGB", (W, 420), BG)
    d = ImageDraw.Draw(img)
    _header(d, title)
    d.line((PAD, 200, W - PAD, 200), fill=LINE, width=2)
    y = 236
    for line in text.split("\n"):
        d.text((PAD, y), _fit(d, line, _font(28), W - 2 * PAD), font=_font(28), fill=DIM)
        y += 44
    return _jpeg(img)


def list_card(title: str, right: str, rows: list[tuple[str, str, str]],
              start: int = 1) -> bytes:
    """rows: (заголовок, подпись, текст справа). start — номер первой строки."""
    row_h = 78
    img = Image.new("RGB", (W, 220 + row_h * len(rows) + 30), BG)
    d = ImageDraw.Draw(img)
    _header(d, title, right)
    y = 200
    for i, (main, sub, side) in enumerate(rows):
        d.line((PAD, y, W - PAD, y), fill=LINE, width=2)
        d.text((PAD, y + 22), f"{start + i:02d}", font=_font(26, True), fill=DIM)
        side_w = d.textlength(side, font=_font(22)) if side else 0
        max_w = W - 2 * PAD - 80 - side_w - 30
        d.text((PAD + 80, y + 12), _fit(d, main, _font(27, True), max_w),
               font=_font(27, True), fill=FG)
        d.text((PAD + 80, y + 46), _fit(d, sub, _font(20), max_w), font=_font(20), fill=DIM)
        if side:
            d.text((W - PAD - side_w, y + 26), side, font=_font(22), fill=DIM)
        y += row_h
    d.line((PAD, y, W - PAD, y), fill=LINE, width=2)
    return _jpeg(img)


def cover(title: str, artist: str) -> bytes:
    """Обложка трека 320×320: уникальный узор из колец по названию."""
    s = 640  # рисуем в 2x и уменьшаем — так линии получаются сглаженными
    img = Image.new("RGB", (s, s), BG)
    d = ImageDraw.Draw(img)
    rnd = random.Random(hashlib.md5(f"{artist}|{title}".encode()).hexdigest())
    cx, cy = rnd.randint(200, 440), rnd.randint(170, 330)
    for _ in range(rnd.randint(7, 12)):
        r = rnd.randint(40, 420)
        a0 = rnd.randint(0, 360)
        d.arc((cx - r, cy - r, cx + r, cy + r), a0, a0 + rnd.randint(60, 330),
              fill=FG if rnd.random() > 0.4 else DIM, width=rnd.choice((2, 3, 6)))
    r = rnd.randint(14, 34)
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=FG)
    d.rectangle((0, 500, s, s), fill=BG)
    d.line((40, 500, s - 40, 500), fill=LINE, width=3)
    d.text((40, 522), _fit(d, title, _font(40, True), s - 80), font=_font(40, True), fill=FG)
    d.text((40, 576), _fit(d, artist, _font(28), s - 80), font=_font(28), fill=DIM)
    return _jpeg(img.resize((320, 320), Image.LANCZOS))
