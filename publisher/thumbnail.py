"""note の見出し画像（アイキャッチ）を作る。

publisher/assets/thumbnail_base.jpg（TRADING のフクロウのポスター）を右側に置き、
左側に記事タイトルを「タグ・日付・見出し」に分けて大きく描く。
note の推奨サイズ 1280x670 の PNG を出力する。

    python -m publisher.thumbnail "【米国株】2026/10/04 今週の振り返りと週明けの展望" out.png
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

WIDTH, HEIGHT = 1280, 670
BASE_IMAGE = Path(__file__).parent / "assets" / "thumbnail_base.jpg"

CREAM = (253, 247, 235)
RED = (196, 30, 36)
INK = (24, 22, 20)
GRAY = (120, 112, 104)

# GitHub Actions では fonts-noto-cjk を入れて使う。手元の Mac 等で試すとき用の候補も並べておく
FONT_CANDIDATES = [
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc", 0),
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", 0),
    ("/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc", 0),
    ("/System/Library/Fonts/ヒラギノ角ゴシック W8.ttc", 0),
    ("/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc", 0),
    ("/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf", 0),
]

# 行頭に来てはいけない文字・行末で切りやすい位置（助詞や読点の後ろ）
NO_LINE_START = set("、。，．・：；？！）」』】〉》ー～…%")
BREAK_AFTER = set("、・ とのをにでへはがもや　")


class ThumbnailError(RuntimeError):
    pass


def find_font() -> tuple[str, int]:
    for path, index in FONT_CANDIDATES:
        if Path(path).exists():
            # .ttc の 0 番目が日本語（JP）でない場合があるので名前で探す
            if path.endswith(".ttc"):
                index = _jp_index(path)
            return path, index
    raise ThumbnailError("日本語フォントが見つかりません（fonts-noto-cjk をインストールしてください）")


def _jp_index(path: str) -> int:
    from PIL import ImageFont

    for i in range(12):
        try:
            name = " ".join(ImageFont.truetype(path, 10, index=i).getname())
        except OSError:
            break
        if "JP" in name and "Mono" not in name:
            return i
    return 0


def split_title(title: str) -> tuple[str, str, str]:
    """「【米国株】2026/10/04 今週の振り返りと週明けの展望」→（"米国株", "2026/10/04", "今週の振り返りと週明けの展望"）"""
    title = title.strip()
    tag = date = ""
    m = re.match(r"^【(.+?)】\s*", title)
    if m:
        tag, title = m.group(1), title[m.end():]
    m = re.match(r"^(\d{4}[/.-]\d{1,2}[/.-]\d{1,2})\s*", title)
    if m:
        date, title = m.group(1).replace("-", "/").replace(".", "/"), title[m.end():]
    return tag, date, title.strip() or tag or date


def _wrap_words(text: str, font, max_width: int) -> list[str]:
    """欧文は単語の切れ目で折り返す（1語が長すぎるときだけ文字で切る）。"""
    lines: list[str] = []
    for word in text.split():
        if lines and font.getlength(lines[-1] + " " + word) <= max_width:
            lines[-1] += " " + word
        elif font.getlength(word) <= max_width:
            lines.append(word)
        else:
            lines += _wrap_chars(word, font, max_width)
    return lines


def wrap(text: str, font, max_width: int) -> list[str]:
    """日本語は 1 文字単位、欧文（仏語など）は単語単位で折り返す。"""
    cjk = sum(1 for c in text if ord(c) > 0x2E80)
    if " " in text.strip() and cjk < len(text) * 0.3:
        return _wrap_words(text, font, max_width)
    return _wrap_chars(text, font, max_width)


def _wrap_chars(text: str, font, max_width: int) -> list[str]:
    """日本語を 1 文字単位で折り返す。きりのいい位置（助詞・読点の後）があればそこで切る。"""
    lines: list[str] = []
    rest = text
    while rest:
        if font.getlength(rest) <= max_width:
            lines.append(rest)
            break
        n = 1
        while n < len(rest) and font.getlength(rest[: n + 1]) <= max_width:
            n += 1
        cut = n
        for j in range(n, max(1, int(n * 0.55)) - 1, -1):
            if rest[j - 1] in BREAK_AFTER:
                cut = j
                break
        while cut < len(rest) and rest[cut] in NO_LINE_START and cut > 1:
            cut -= 1
        lines.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    return lines


def _fit_headline(draw, text: str, font_path: str, index: int, max_width: int, max_height: int):
    from PIL import ImageFont

    # 大きい字で 2 行に収まるならそれを優先し、だめなら 3 行まで許す
    for max_lines, min_size in ((2, 64), (3, 40), (4, 40)):
        for size in range(92, min_size - 1, -4):
            font = ImageFont.truetype(font_path, size, index=index)
            lines = wrap(text, font, max_width)
            height = len(lines) * int(size * 1.28)
            if len(lines) <= max_lines and height <= max_height:
                return font, lines, size
    font = ImageFont.truetype(font_path, 40, index=index)
    lines = wrap(text, font, max_width)[:4]
    return font, lines, 40


def _poster(height: int):
    """ポスターから「TRADING」とフクロウの部分を切り出し、左端を背景色になじませる。"""
    from PIL import Image

    base = Image.open(BASE_IMAGE).convert("RGB")
    w, h = base.size
    crop = base.crop((0, int(h * 0.09), w, int(h * 0.80)))
    scale = height / crop.height
    crop = crop.resize((int(crop.width * scale), height), Image.LANCZOS)

    fade = Image.new("L", crop.size, 255)
    px = fade.load()
    edge = 70
    for x in range(edge):
        v = int(255 * x / edge)
        for y in range(crop.height):
            px[x, y] = v
    return crop, fade


def make_thumbnail(title: str, out: Path, *, kind: str = "", tag: str | None = None,
                   date: str | None = None) -> Path:
    """tag / date を渡したときは title をそのまま見出しにする（Substack の仏語記事など）。"""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as e:  # pragma: no cover
        raise ThumbnailError("Pillow が入っていません") from e

    font_path, index = find_font()
    if tag is None and date is None:
        tag, date, headline = split_title(title)
    else:
        tag, date, headline = tag or "", date or "", title.strip()

    img = Image.new("RGB", (WIDTH, HEIGHT), CREAM)
    poster, mask = _poster(HEIGHT)
    img.paste(poster, (WIDTH - poster.width, 0), mask)
    draw = ImageDraw.Draw(img)

    left, text_w = 64, WIDTH - poster.width - 64 - 24
    font = lambda size: ImageFont.truetype(font_path, size, index=index)  # noqa: E731

    # 上部: ポスターと同じ英字の飾り
    small = font(20)
    draw.text((left, 44), "LIVE RATES  ·  VOLATILE MARKETS", font=small, fill=INK)

    y = 104
    if tag:
        f = font(34)
        tw = int(f.getlength(tag))
        draw.rectangle((left, y, left + tw + 36, y + 58), fill=RED)
        draw.text((left + 18, y + 29), tag, font=f, fill=CREAM, anchor="lm")
        y += 58 + 22
    if date:
        draw.text((left, y), date, font=font(60), fill=INK)
        y += 60 + 26
    draw.rectangle((left, y, left + 96, y + 8), fill=RED)
    y += 8 + 30

    footer_h = 70
    hfont, lines, size = _fit_headline(draw, headline, font_path, index, text_w, HEIGHT - y - footer_h)
    line_h = int(size * 1.28)
    for i, line in enumerate(lines):
        draw.text((left, y + i * line_h), line, font=hfont, fill=INK)

    label = kind or ("WEEKLY REVIEW" if "今週" in title or "週明け" in title else "MARKET CLOSE")
    draw.text((left, HEIGHT - 50), f"{label}  ·  Gold  Crypto  Stock", font=small, fill=GRAY)

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "PNG", optimize=True)
    return out


if __name__ == "__main__":
    print(make_thumbnail(sys.argv[1], Path(sys.argv[2] if len(sys.argv) > 2 else "eyecatch.png")))
