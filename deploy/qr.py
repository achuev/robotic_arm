#!/usr/bin/env python3
"""Табличка с QR-кодом для демо-стенда SO-101.

Печатается и ставится рядом с рукой. Прохожий наводит камеру телефона,
попадает на сайт и берёт управление.

    deploy/.venv/bin/python deploy/qr.py http://192.168.1.42:8080 -o tablet.pdf
    deploy/.venv/bin/python deploy/qr.py https://arm.example.com  -o tablet.png

Адрес — обязательный аргумент, а не константа, потому что режима два и они
меняются на ходу:

  * локальный Wi-Fi   `http://<ip>:8080`  — IP выдаёт роутер, он разный;
  * публичный туннель `https://<домен>`   — домен cloudflared выдаёт при старте.

Зависимости стоят в отдельном venv (deploy/.venv, см. requirements-qr.txt) и
намеренно не заезжают в образ шлюза: печать таблички — операция для человека
с ноутбуком, а не часть рантайма стенда.

    python3 -m venv deploy/.venv
    deploy/.venv/bin/pip install -r deploy/requirements-qr.txt
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import qrcode
from PIL import Image, ImageDraw, ImageFont
from qrcode.constants import ERROR_CORRECT_Q

# Печатаем на 300 dpi: на меньшем разрешении края QR замыливаются и телефон
# ловит код хуже, особенно под углом и в свете ламп.
DPI = 300
MM = DPI / 25.4

PAGES_MM = {  # ширина × высота, мм
    "a4": (210, 297),
    "a5": (148, 210),
    "a6": (105, 148),
}

# Шрифты с кириллицей. Первый найденный выигрывает. Списки разные для macOS
# (ноутбук, с которого печатают) и Linux (стенд). Встроенный шрифт Pillow
# кириллицу не умеет вообще — без TTF надпись была бы из вопросиков.
FONT_CANDIDATES = [
    # (обычный, жирный)
    ("/System/Library/Fonts/Supplemental/Arial.ttf",
     "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    ("/Library/Fonts/Arial Unicode.ttf", "/Library/Fonts/Arial Unicode.ttf"),
    ("/System/Library/Fonts/Geneva.ttf", "/System/Library/Fonts/Geneva.ttf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/TTF/DejaVuSans.ttf", "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
     "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
]

TITLE = "Порулите роботом"
SUBTITLE = "Наведите камеру телефона"
FOOTER = "Ничего скачивать не нужно · 90 секунд на человека"

INK = (17, 20, 26)
MUTED = (110, 118, 132)
PAPER = (255, 255, 255)


@dataclass
class Fonts:
    regular: str
    bold: str

    @classmethod
    def find(cls, override: str | None) -> "Fonts":
        if override:
            return cls(override, override)
        for regular, bold in FONT_CANDIDATES:
            if Path(regular).exists() and Path(bold).exists():
                return cls(regular, bold)
        raise SystemExit(
            "Не нашёл шрифт с кириллицей. Укажите свой: --font /путь/к/шрифту.ttf\n"
            "На Debian/Ubuntu: apt-get install fonts-dejavu-core"
        )

    def at(self, size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
        return ImageFont.truetype(self.bold if bold else self.regular, size)


def make_qr(url: str, box_px: int) -> Image.Image:
    """QR под нужный размер в пикселях.

    ERROR_CORRECT_Q (25 %) — запас на то, что табличка испачкается, чуть
    порвётся или на неё ляжет блик. Для короткого URL это ничего не стоит.
    """
    qr = qrcode.QRCode(version=None, error_correction=ERROR_CORRECT_Q, box_size=10, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    # NEAREST, а не сглаживание: модули QR должны остаться квадратами с
    # резкой границей, размытые края телефон читает хуже.
    return img.resize((box_px, box_px), Image.NEAREST)


def _center(draw: ImageDraw.ImageDraw, y: int, text: str,
            font: ImageFont.FreeTypeFont, fill, width: int) -> int:
    box = draw.textbbox((0, 0), text, font=font)
    draw.text(((width - (box[2] - box[0])) / 2 - box[0], y), text, font=font, fill=fill)
    return y + (box[3] - box[1]) + int(box[1] * 0.5)


def render(url: str, page: str, fonts: Fonts,
           title: str, subtitle: str, footer: str) -> Image.Image:
    w_mm, h_mm = PAGES_MM[page]
    W, H = int(w_mm * MM), int(h_mm * MM)
    img = Image.new("RGB", (W, H), PAPER)
    draw = ImageDraw.Draw(img)

    margin = int(12 * MM)
    scale = W / (148 * MM)  # все кегли заданы для A5 и масштабируются от неё

    f_sub = fonts.at(int(46 * scale))
    f_title = fonts.at(int(86 * scale), bold=True)
    f_url = fonts.at(int(34 * scale))
    f_foot = fonts.at(int(28 * scale))

    # QR — самое крупное пятно на табличке: его ищут глазами с двух метров.
    qr_px = min(W - 2 * margin, int(H * 0.55))

    # Блок «подзаголовок + заголовок + QR + адрес» центрируется в поле над
    # подвалом целиком. Иначе на A4 (где QR упирается в высоту, а не в ширину)
    # внизу оставалась бы треть пустого листа.
    gap_after_sub, gap_after_title, gap_after_qr = int(4 * MM), int(10 * MM), int(9 * MM)
    block = (
        f_sub.size + gap_after_sub + f_title.size + gap_after_title
        + qr_px + gap_after_qr + f_url.size
    )
    footer_zone = int(26 * MM)
    y = max(int(14 * MM), (H - footer_zone - block) // 2)

    y = _center(draw, y, subtitle, f_sub, MUTED, W) + gap_after_sub
    y = _center(draw, y, title, f_title, INK, W) + gap_after_title

    qr = make_qr(url, qr_px)
    qr_x = (W - qr_px) // 2
    img.paste(qr, (qr_x, y))
    # Тонкая рамка: на белой бумаге край QR иначе теряется.
    draw.rectangle([qr_x - 8, y - 8, qr_x + qr_px + 8, y + qr_px + 8], outline=(228, 230, 234), width=3)
    y += qr_px + gap_after_qr

    # Адрес прописью — для тех, у кого камера не читает QR (старый телефон,
    # запрет камеры, треснувший экран). Печатается вместе со схемой: без
    # «http://» телефон уводит набранное в поиск, а не на стенд.
    _center(draw, y, url, f_url, MUTED, W)
    _center(draw, H - int(20 * MM), footer, f_foot, MUTED, W)
    return img


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Табличка с QR для демо-стенда SO-101",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("url", help="адрес стенда: http://<ip>:8080 или https://<домен>")
    p.add_argument("-o", "--out", default="so101-qr.pdf",
                   help="файл: .pdf для печати, .png для экрана (по умолчанию %(default)s)")
    p.add_argument("--page", choices=sorted(PAGES_MM), default="a5",
                   help="размер листа (по умолчанию %(default)s)")
    p.add_argument("--title", default=TITLE)
    p.add_argument("--subtitle", default=SUBTITLE)
    p.add_argument("--footer", default=FOOTER)
    p.add_argument("--font", help="путь к TTF с кириллицей, если автопоиск промахнулся")
    args = p.parse_args(argv)

    if "://" not in args.url:
        p.error("адрес нужен со схемой: http://192.168.1.42:8080, а не 192.168.1.42:8080")
    if args.url.startswith("http://localhost") or args.url.startswith("http://127."):
        print("ВНИМАНИЕ: localhost работает только на этой машине — телефон "
              "посетителя по такому адресу не попадёт никуда.", file=sys.stderr)

    img = render(args.url, args.page, Fonts.find(args.font),
                 args.title, args.subtitle, args.footer)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.suffix.lower() == ".pdf":
        img.save(out, "PDF", resolution=DPI)
    else:
        img.save(out, dpi=(DPI, DPI))

    print(f"{out}  {img.width}×{img.height} px  ({args.page.upper()}, {DPI} dpi)  → {args.url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
