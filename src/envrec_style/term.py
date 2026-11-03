"""The banner for a terminal, drawn from the same glyph data as the SVG logo.

A pixel is two characters wide and one row tall (a character is about half as wide as a row is tall, so it is square). The shadow is the same shape half a pixel down and right: one
character right and half a row down, which is what the half blocks are for. Modes: wide (with the tagline and the motto), narrow (a pixel is one character wide and one row tall, so
the shadow is half a row down and a whole pixel right), compact (one line) and plain (ASCII only, no escape codes at all: no terminal, or one that cannot show the blocks). Color: truecolor when COLORTERM says so,
256 colors, 16 colors, and with NO_COLOR the same shape in shades (full block for the letters, light shade for the shadow)."""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Mapping
from typing import TextIO

from envrec_style import tokens

WIDE_MIN_COLUMNS = 38
NARROW_MIN_COLUMNS = 18
RESET = "\x1b[0m"
XTERM16 = [(0, 0, 0), (205, 0, 0), (0, 205, 0), (205, 205, 0), (0, 0, 238), (205, 0, 205), (0, 205, 205), (229, 229, 229),
           (127, 127, 127), (255, 0, 0), (0, 255, 0), (255, 255, 0), (92, 92, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255)]
CUBE = (0, 95, 135, 175, 215, 255)


def color_level(env: Mapping[str, str], tty: bool) -> str:
    """truecolor | 256 | 16 | none. None when NO_COLOR is set and not empty (no-color.org), when the output is not a terminal, or when TERM is dumb."""
    if env.get("NO_COLOR") or not tty or env.get("TERM", "") == "dumb":
        return "none"
    if env.get("COLORTERM", "").lower() in ("truecolor", "24bit"):
        return "truecolor"
    if "256" in env.get("TERM", ""):
        return "256"
    return "16"


def _dist(a: tuple[int, int, int], b: tuple[int, int, int]) -> int:
    return sum((x - y) ** 2 for x, y in zip(a, b))


def nearest_256(rgb: tuple[int, int, int]) -> int:
    best_cube = min(((i, j, k) for i in range(6) for j in range(6) for k in range(6)), key=lambda t: _dist(rgb, (CUBE[t[0]], CUBE[t[1]], CUBE[t[2]])))
    cube_rgb = (CUBE[best_cube[0]], CUBE[best_cube[1]], CUBE[best_cube[2]])
    gray = min(range(24), key=lambda g: _dist(rgb, (8 + 10 * g,) * 3))
    if _dist(rgb, (8 + 10 * gray,) * 3) < _dist(rgb, cube_rgb):
        return 232 + gray
    return 16 + 36 * best_cube[0] + 6 * best_cube[1] + best_cube[2]


def nearest_16(rgb: tuple[int, int, int]) -> int:
    return min(range(16), key=lambda i: _dist(rgb, XTERM16[i]))


def sgr(hex_color: str, level: str, *, background: bool = False) -> str:
    rgb = tokens.hex_to_rgb(hex_color)
    if level == "truecolor":
        return f"\x1b[{48 if background else 38};2;{rgb[0]};{rgb[1]};{rgb[2]}m"
    if level == "256":
        return f"\x1b[{48 if background else 38};5;{nearest_256(rgb)}m"
    n = nearest_16(rgb)
    base = (100 if background else 90) if n >= 8 else (40 if background else 30)
    return f"\x1b[{base + n % 8}m"


def _subcells(sx: int, sy: int, ox: int, oy: int) -> tuple[dict[tuple[int, int], str], int, int]:
    """(sub-row, sub-column) -> 'L' letter or 'S' shadow, and the size. Letters win where they overlap the shadow."""
    cells: dict[tuple[int, int], str] = {}
    for kind, dr, dc in (("S", oy, ox), ("L", 0, 0)):
        for r, c in tokens.letter_pixels():
            for i in range(sy):
                for j in range(sx):
                    cells[(r * sy + i + dr, c * sx + j + dc)] = kind
    rows = tokens.load()["glyph"]["rows"] * sy + oy
    cols = tokens.word_columns() * sx + ox
    return cells, rows, cols


def _logo_lines(level: str, *, sx: int, sy: int, ox: int, oy: int, colors: dict[str, str]) -> list[str]:
    cells, rows, cols = _subcells(sx, sy, ox, oy)
    out = []
    for tr in range((rows + 1) // 2):
        parts: list[tuple[str, str]] = []  # (character, escape sequence in front of it)
        for c in range(cols):
            top, bot = cells.get((2 * tr, c)), cells.get((2 * tr + 1, c))
            if level == "none":
                if top == "L" and bot == "L":
                    ch = "█"
                elif top == "L":
                    ch = "▀"
                elif bot == "L":
                    ch = "▄"
                elif top or bot:
                    ch = "░"
                else:
                    ch = " "
                parts.append((ch, ""))
                continue
            fg = lambda k: sgr(colors["letters" if k == "L" else "shadow"], level)  # noqa: E731
            bg = lambda k: sgr(colors["letters" if k == "L" else "shadow"], level, background=True)  # noqa: E731
            if not top and not bot:
                parts.append((" ", ""))
            elif top == bot:
                parts.append(("█", fg(top)))
            elif top and not bot:
                parts.append(("▀", fg(top)))
            elif bot and not top:
                parts.append(("▄", fg(bot)))
            else:
                parts.append(("▀", fg(top) + bg(bot)))
        while parts and parts[-1][0] == " ":
            parts.pop()
        line, last = "", ""
        for ch, esc in parts:
            if esc != last:
                line += (RESET if last else "") + esc
                last = esc
            line += ch
        out.append(line + (RESET if last else ""))
    return out


def _paint(text: str, hex_color: str, level: str) -> str:
    return text if level == "none" else f"{sgr(hex_color, level)}{text}{RESET}"


def compact_line(level: str = "none", *, unicode: bool = True) -> str:
    b = tokens.load()["brand"]
    sep = "·" if unicode else "-"
    if level == "none":
        return f"{b['short']} {sep} {b['name']}"
    return f"{_paint(b['short'], b['letters'], level)} {sep} {b['name']}"


def plain_text() -> str:
    """No escape codes, ASCII only: for a pipe, a file, a log, or a terminal that cannot show the blocks."""
    b = tokens.load()["brand"]
    return f"{compact_line('none', unicode=False)}\n{b['motto']}"


def banner_mode(columns: int, *, tty: bool, utf8: bool = True) -> str:
    if not tty or not utf8:
        return "plain"
    if columns >= WIDE_MIN_COLUMNS:
        return "wide"
    return "narrow" if columns >= NARROW_MIN_COLUMNS else "compact"


def render(*, columns: int, tty: bool, env: Mapping[str, str] | None = None, utf8: bool = True) -> str:
    """The banner for this terminal. Never wider than `columns` (in compact mode a one-liner longer than that is still one line)."""
    env = os.environ if env is None else env
    level = color_level(env, tty)
    mode = banner_mode(columns, tty=tty, utf8=utf8)
    b = tokens.load()["brand"]
    if mode == "plain":
        return plain_text()
    if mode == "compact":
        return compact_line(level)
    colors = tokens.logo_colors("dark")
    if mode == "wide":
        lines = ["  " + ln for ln in _logo_lines(level, sx=2, sy=2, ox=1, oy=1, colors=colors)]
        tag = " ".join(b["tagline"])
        lines += ["", "  " + _paint(tag, b["tagline_color"], level), "  " + b["motto"]]
    else:
        lines = _logo_lines(level, sx=1, sy=2, ox=1, oy=1, colors=colors)
        lines += ["", _paint(b["tagline"], b["tagline_color"], level)]
    return "\n".join(lines)


def for_stream(stream: TextIO | None = None, env: Mapping[str, str] | None = None) -> str:
    """The banner for a real stream: terminal or not, width, and whether it can show the blocks."""
    stream = stream or sys.stdout
    tty = bool(getattr(stream, "isatty", lambda: False)())
    enc = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
    cols = shutil.get_terminal_size((80, 24)).columns if tty else 80
    return render(columns=cols, tty=tty, env=env, utf8=enc.startswith("utf"))
