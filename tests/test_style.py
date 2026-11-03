"""The style system: one source of truth, and every surface fails if it drifts from it."""
import re
from html import escape as html_escape
import struct
from pathlib import Path

import pytest

from envrec_harness import cards
from envrec_style import css, gallery, svg, tokens

ROOT = Path(__file__).resolve().parents[1]
BRAND = ROOT / "assets" / "brand"


def test_palette_is_the_locked_one():
    b = tokens.load()["brand"]
    assert (b["letters"], b["shadow"], b["tagline_color"], b["background"]) == ("#3B82F6", "#86198F", "#E879F9", "#0A0614")
    assert b["motto"] == "Your computers, working as a team." and b["tagline"] == "MULTI AUTO SETUP"


def test_glyph_is_the_locked_one():
    g = tokens.load()["glyph"]["letters"]
    assert g["M"] == ["X...X", "XX.XX", "X.X.X", "X...X", "X...X"]
    assert g["A"] == [".XXX.", "X...X", "XXXXX", "X...X", "X...X"]
    assert g["S"] == [".XXXX", "X....", ".XXX.", "....X", "XXXX."]
    assert tokens.word_columns() == 17


@pytest.mark.parametrize("name", ["dark", "transparent"])
def test_committed_logo_is_byte_identical_to_what_is_generated(name):
    assert (BRAND / f"logo-{name}.svg").read_text(encoding="utf-8") == svg.logo_svg(name)


def test_committed_assets_match_tokens():
    for name in ("logo-dark", "logo-transparent", "logo-light", "logo-light-alt"):
        variant = {"logo-dark": "dark", "logo-transparent": "transparent", "logo-light": "light", "logo-light-alt": "light-b"}[name]
        assert (BRAND / f"{name}.svg").read_text(encoding="utf-8") == svg.logo_svg(variant), name
    assert (BRAND / "gallery.html").read_text(encoding="utf-8") == gallery.gallery_html()


def test_png_assets_exist_at_two_sizes():
    for name in ("logo-dark", "logo-light", "logo-transparent"):
        sizes = []
        for f in (f"{name}.png", f"{name}@2x.png"):
            data = (BRAND / f).read_bytes()
            assert data[:8] == b"\x89PNG\r\n\x1a\n"
            sizes.append(struct.unpack(">II", data[16:24]))
        assert sizes == [(1000, 440), (2000, 880)]


@pytest.mark.parametrize("variant", ["dark", "transparent", "light", "light-b"])
def test_svg_glyph_equals_token_glyph(variant):
    col = tokens.logo_colors(variant)
    text = svg.logo_svg(variant)
    assert svg.parse_logo_pixels(text, col["letters"], shadow=False) == tokens.letter_pixels()
    assert svg.parse_logo_pixels(text, col["shadow"], shadow=True) == tokens.letter_pixels()  # the same shape, half a cell down and right
    assert 'shape-rendering="crispEdges"' in text
    assert ("<rect width=" in text) == (col["background"] is not None)


def test_light_variants_keep_contrast_on_white():
    for key in ("A", "B"):
        c = tokens.load()["light"][key]
        assert tokens.contrast(c["letters"], "#FFFFFF") >= 3.0
        assert tokens.contrast(c["tagline_color"], "#FFFFFF") >= 4.5
    default = tokens.load()["light"]["default"]
    assert tokens.contrast(tokens.load()["light"][default]["shadow"], "#FFFFFF") >= 4.5


def test_dark_brand_contrast_on_its_background():
    b = tokens.load()["brand"]
    assert tokens.contrast(b["letters"], b["background"]) >= 3.0
    assert tokens.contrast(b["tagline_color"], b["background"]) >= 4.5


def test_semantic_colors_are_the_ones_the_panel_cards_use():
    assert (cards.PALETTE["teal"], cards.PALETTE["teal_bright"]) == (tokens.semantic("teal", dark=False), tokens.semantic("teal", dark=True))
    assert (cards.PALETTE["amber"], cards.PALETTE["amber_bright"]) == (tokens.semantic("amber", dark=False), tokens.semantic("amber", dark=True))
    assert "--mas-teal:" + tokens.semantic("teal", dark=False) in css.css_variables()


def test_every_symbol_has_shape_word_meaning_and_nothing_relies_on_color():
    syms = tokens.symbols()
    assert len(syms) >= 27
    assert len({s["id"] for s in syms}) == len(syms)
    assert len({tuple(s["grid"]) for s in syms}) == len(syms), "two symbols share one shape"
    assert len({s["char"] for s in syms}) == len(syms)
    assert len({s["word"] for s in syms}) == len(syms)
    for s in syms:
        assert len(s["grid"]) == 8 and all(len(r) == 8 and set(r) <= {"X", "."} for r in s["grid"]), s["id"]
        assert sum(r.count("X") for r in s["grid"]) >= 10, s["id"]
        assert s["word"] and s["meaning"] and s["char"] and s["group"] in tokens.load()["symbol_groups"]
        assert s["word"] in tokens.symbol_text(s["id"]) and s["word"] in tokens.symbol_text(s["id"], unicode=False)
        assert s["role"] in ("teal", "amber", "neutral")
    assert {s["id"] for s in syms if s["role"] != "neutral"} == {"local", "cloud"}


def test_required_symbols_exist():
    need = {"local", "cloud", "confidential", "chip", "key", "terminal", "coordinator", "node", "laptop", "desktop", "server", "oldpc", "searching", "waiting", "paired", "approved", "denied",
            "running", "done", "failed", "undone", "warning", "owner", "model", "guard", "script", "network"}
    assert need <= {s["id"] for s in tokens.symbols()}


def test_symbol_svg_draws_exactly_the_grid():
    s = tokens.symbol("confidential")
    text = svg.symbol_svg("confidential", cell=4, color="#111111")
    lit = sum(r.count("X") for r in s["grid"])
    area = sum(int(w) * int(h) for w, h in re.findall(r'width="(\d+)" height="(\d+)" fill="#111111"', text))
    assert area == lit * 16
    assert "crispEdges" in text


def test_gallery_shows_every_symbol_and_logo_on_dark_and_light():
    html = gallery.gallery_html()
    for s in tokens.symbols():
        assert html.count(f"<b>{html_escape(s['char'] + ' ' + s['word'])}</b>") == 2, s["id"]
    assert html.count("<title>MAS</title>") == 4


# ---- the terminal banner ------------------------------------------------------------------------------------------------------------------------

import re as _re

from envrec_style import term

ANSI = _re.compile(r"\x1b\[[0-9;]*m")
TRUE = {"COLORTERM": "truecolor", "TERM": "xterm-256color"}


def _decode_letters(text: str, sx: int, sy: int, indent: int) -> set[tuple[int, int]]:
    """Read the letter pixels back out of the NO_COLOR banner (full and half blocks are letters; the light shade is shadow)."""
    lines = text.split("\n")[: (5 * sy + 1 + 1) // 2]
    sub = set()
    for tr, ln in enumerate(lines):
        for c, ch in enumerate(ln[indent:]):
            if ch in "█▀":
                sub.add((2 * tr, c))
            if ch in "█▄":
                sub.add((2 * tr + 1, c))
    return {(r, c) for r in range(5) for c in range(tokens.word_columns()) if all((r * sy + i, c * sx + j) in sub for i in range(sy) for j in range(sx))}


def test_wide_banner_letters_equal_the_glyph():
    assert _decode_letters(term.render(columns=80, tty=True, env={"NO_COLOR": "1"}), 2, 2, 2) == tokens.letter_pixels()


def test_narrow_banner_letters_equal_the_glyph():
    assert _decode_letters(term.render(columns=24, tty=True, env={"NO_COLOR": "1"}), 1, 2, 0) == tokens.letter_pixels()


def test_wide_truecolor_uses_the_brand_colors_and_the_text():
    out = term.render(columns=80, tty=True, env=TRUE)
    assert "\x1b[38;2;59;130;246m" in out and "\x1b[38;2;134;25;143m" in out and "\x1b[38;2;232;121;249m" in out
    plain = ANSI.sub("", out)
    assert "M U L T I   A U T O   S E T U P" in plain and "Your computers, working as a team." in plain
    assert max(len(ln) for ln in plain.split("\n")) <= 80
    assert "\x1b[48;2;10;6;20m" not in out, "the terminal keeps the user's background"


def test_256_and_16_color_fallbacks_are_nearest_matches():
    out256 = term.render(columns=80, tty=True, env={"TERM": "xterm-256color"})
    assert "\x1b[38;5;69m" in out256 and "\x1b[38;2;" not in out256  # #3B82F6 is nearest to 69 in the 6x6x6 cube
    out16 = term.render(columns=80, tty=True, env={"TERM": "xterm"})
    assert "\x1b[94m" in out16 and "\x1b[35m" in out16 and "38;5" not in out16 and "38;2" not in out16


def test_no_color_has_no_escape_codes_but_keeps_the_shape():
    out = term.render(columns=80, tty=True, env={"NO_COLOR": "1", **TRUE})
    assert "\x1b" not in out and "█" in out and "░" in out
    assert "MULTI" not in out or "M U L T I" in out
    assert "Your computers, working as a team." in out


def test_empty_no_color_does_not_switch_color_off():
    assert term.color_level({"NO_COLOR": "", **TRUE}, True) == "truecolor"


def test_not_a_tty_is_plain_ascii_without_any_escape_code():
    out = term.render(columns=200, tty=False, env=TRUE)
    assert "\x1b" not in out and out.isascii()
    assert out == "MAS - Multi Auto Setup\nYour computers, working as a team."


def test_a_terminal_that_cannot_show_blocks_gets_plain():
    assert term.render(columns=80, tty=True, env=TRUE, utf8=False).isascii()


def test_modes_follow_the_width():
    assert term.banner_mode(80, tty=True) == "wide" and term.banner_mode(37, tty=True) == "narrow"
    assert term.banner_mode(18, tty=True) == "narrow" and term.banner_mode(17, tty=True) == "compact"
    narrow = ANSI.sub("", term.render(columns=20, tty=True, env=TRUE))
    assert max(len(ln) for ln in narrow.split("\n")) <= 20 and "Your computers" not in narrow
    compact = term.render(columns=12, tty=True, env={"NO_COLOR": "1"})
    assert compact == "MAS · Multi Auto Setup" and "\n" not in compact


def test_dumb_terminal_gets_no_color():
    assert term.color_level({**TRUE, "TERM": "dumb"}, True) == "none"


def test_for_stream_on_a_pipe_is_plain():
    import io
    assert term.for_stream(io.StringIO(), env=TRUE) == term.plain_text()


def test_a_half_block_with_letter_above_and_shadow_below_sets_both_colors():
    lines = term._logo_lines("truecolor", sx=1, sy=1, ox=1, oy=1, colors=tokens.logo_colors("dark"))  # pixels half a row tall: a letter pixel can sit over a shadow pixel
    assert any("\x1b[38;2;59;130;246m\x1b[48;2;134;25;143m▀" in ln for ln in lines)
