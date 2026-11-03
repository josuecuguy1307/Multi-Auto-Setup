"""Reads tokens.json, the one source of truth for the look of every surface. Nothing here draws; svg.py, term.py, css.py and gallery.py do, from what this returns."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

TOKENS_FILE = Path(__file__).with_name("tokens.json")


@lru_cache(maxsize=1)
def load() -> dict:
    return json.loads(TOKENS_FILE.read_text(encoding="utf-8"))


def hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _lin(c: int) -> float:
    s = c / 255
    return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4


def luminance(h: str) -> float:
    r, g, b = (_lin(c) for c in hex_to_rgb(h))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio of two #RRGGBB colors."""
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def logo_colors(variant: str) -> dict:
    """letters / shadow / tagline_color / background (None = transparent) for dark | transparent | light | light-b."""
    t = load()
    brand = {k: t["brand"][k] for k in ("letters", "shadow", "tagline_color")}
    if variant == "dark":
        return {**brand, "background": t["brand"]["background"]}
    if variant == "transparent":
        return {**brand, "background": None}
    if variant in ("light", "light-b"):
        key = t["light"]["default"] if variant == "light" else "B"
        return {**t["light"][key], "background": None}
    raise ValueError(f"unknown logo variant: {variant}")


def letter_grids() -> list[list[str]]:
    g = load()["glyph"]
    return [g["letters"][ch] for ch in g["word"]]


def letter_pixels() -> set[tuple[int, int]]:
    """(row, col) of every lit pixel of the word on the glyph grid, letters side by side with the gap between."""
    g = load()["glyph"]
    out = set()
    for i, rows in enumerate(letter_grids()):
        for r, line in enumerate(rows):
            for c, ch in enumerate(line):
                if ch == "X":
                    out.add((r, i * (g["width"] + g["gap"]) + c))
    return out


def word_columns() -> int:
    g = load()["glyph"]
    n = len(g["word"])
    return n * g["width"] + (n - 1) * g["gap"]


def symbols() -> list[dict]:
    return load()["symbols"]


def symbol(sym_id: str) -> dict:
    for s in symbols():
        if s["id"] == sym_id:
            return s
    raise KeyError(sym_id)


def symbol_text(sym_id: str, *, unicode: bool = True) -> str:
    """The text form of a symbol, always with its word: '☁ cloud', or '[CLOUD] cloud' where the terminal or the chat cannot be trusted with the character."""
    s = symbol(sym_id)
    return f"{s['char']} {s['word']}" if unicode else f"[{s['id'].upper()}] {s['word']}"


def semantic(name: str, *, dark: bool) -> str:
    return load()["semantic"][name]["on_dark" if dark else "on_light"]
