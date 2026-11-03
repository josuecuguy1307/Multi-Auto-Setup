"""SVG forms of the logo and the symbols, drawn from tokens.json as crisp rectangles."""

from __future__ import annotations

from envrec_style import tokens

NS = "http://www.w3.org/2000/svg"


def _word_cells() -> list[tuple[int, int]]:
    """Lit pixels as (row, col) in the order the logo files list them: letter by letter, row by row."""
    g = tokens.load()["glyph"]
    out = []
    for i, rows in enumerate(tokens.letter_grids()):
        for r, line in enumerate(rows):
            for c, ch in enumerate(line):
                if ch == "X":
                    out.append((r, i * (g["width"] + g["gap"]) + c))
    return out


def logo_svg(variant: str = "dark", *, tagline: bool = True) -> str:
    """The logo: the shadow (the same shape, half a cell down and right) drawn first, the letters in front, then the tagline. variant: dark | transparent | light | light-b."""
    t = tokens.load()
    lg, col = t["logo"], tokens.logo_colors(variant)
    cell, off = lg["cell"], lg["shadow_offset"]
    ox, oy = lg["origin"]
    w, h = lg["canvas"]
    parts = [f'<svg xmlns="{NS}" viewBox="0 0 {w} {h}" width="{w}" height="{h}" shape-rendering="crispEdges" role="img"><title>{t["brand"]["short"]}</title>']
    if col["background"]:
        parts.append(f'<rect width="{w}" height="{h}" fill="{col["background"]}"/>')
    cells = _word_cells()
    for r, c in cells:
        parts.append(f'<rect x="{float(ox + off + c * cell)}" y="{float(oy + off + r * cell)}" width="{cell}" height="{cell}" fill="{col["shadow"]}"/>')
    for r, c in cells:
        parts.append(f'<rect x="{ox + c * cell}" y="{oy + r * cell}" width="{cell}" height="{cell}" fill="{col["letters"]}"/>')
    if tagline:
        tx, ty = lg["tagline_at"]
        parts.append(f'<text x="{tx}" y="{ty}" font-family="{lg["font_family"]}" font-size="{lg["font_size"]}" letter-spacing="{lg["letter_spacing"]}" fill="{col["tagline_color"]}">{t["brand"]["tagline"]}</text>')
    parts.append("</svg>")
    return "".join(parts)


def symbol_svg(sym_id: str, *, cell: int = 4, color: str = "currentColor", shadow: str | None = None) -> str:
    """One symbol as crisp rectangles (a run of lit pixels in a row is one rectangle). With `shadow`, the same shape half a cell down and right, drawn behind."""
    s = tokens.symbol(sym_id)
    off = cell / 2 if shadow else 0
    size = len(s["grid"]) * cell + off

    def runs(fill: str, dx: float) -> list[str]:
        out = []
        for r, line in enumerate(s["grid"]):
            c = 0
            while c < len(line):
                if line[c] != "X":
                    c += 1
                    continue
                start = c
                while c < len(line) and line[c] == "X":
                    c += 1
                out.append(f'<rect x="{start * cell + dx:g}" y="{r * cell + dx:g}" width="{(c - start) * cell}" height="{cell}" fill="{fill}"/>')
        return out

    body = (runs(shadow, off) if shadow else []) + runs(color, 0)
    return (f'<svg xmlns="{NS}" viewBox="0 0 {size:g} {size:g}" width="{size:g}" height="{size:g}" shape-rendering="crispEdges" role="img">'
            f'<title>{s["word"]}</title>' + "".join(body) + "</svg>")


def symbol_color(sym_id: str, *, dark: bool, ink: str) -> str:
    """The color a symbol is drawn in: its semantic color (teal, amber) or the surface's ink. Meaning is never in the color alone: the shape and the word say it too."""
    role = tokens.symbol(sym_id)["role"]
    return tokens.semantic(role, dark=dark) if role in ("teal", "amber") else ink


def parse_logo_pixels(svg: str, layer_color: str, *, shadow: bool) -> set[tuple[int, int]]:
    """Read the lit pixels of the word back out of a logo SVG (for the drift test): the rectangles of one color, as (row, col) on the glyph grid. `shadow` says the layer sits half a cell down and right."""
    import re
    lg = tokens.load()["logo"]
    cell, (ox, oy) = lg["cell"], lg["origin"]
    shift = lg["shadow_offset"] if shadow else 0
    found = re.findall(rf'<rect x="([\d.]+)" y="([\d.]+)" width="{cell}" height="{cell}" fill="{re.escape(layer_color)}"/>', svg)
    return {(round((float(y) - oy - shift) / cell), round((float(x) - ox - shift) / cell)) for x, y in found}
