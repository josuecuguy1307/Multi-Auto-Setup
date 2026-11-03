"""Write the README images in assets/readme/ from src/envrec_style/tokens.json: the flow diagram, the status tiles and the symbols the README tables use, each in a dark and a light version. The files are committed; tests/test_readme.py fails if they drift.

    python scripts/make_readme_assets.py [--check]
"""
from __future__ import annotations

import sys
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from envrec_style import svg, tokens  # noqa: E402

OUT = ROOT / "assets" / "readme"
SYMBOLS = ["owner", "model", "guard", "script", "local", "cloud", "confidential", "paired", "approved", "undone", "done", "warning", "searching", "network", "coordinator", "node"]

# surface colors for the two README themes. The brand colors come from the tokens; these are only the box fill and the text ink.
SURFACE = {
    "dark": {"bg": tokens.load()["brand"]["background"], "box": "#150E26", "ink": "#E6E1F0", "muted": "#A79FBF"},
    "light": {"bg": None, "box": "#F4F1FA", "ink": "#1B1530", "muted": "#5B5473"},
}
FONT = tokens.load()["logo"]["font_family"]
ACTOR_WORD = {"owner": "OWNER", "model": "MODEL", "guard": "GUARD", "script": "SCRIPT"}


def _theme(theme: str) -> dict:
    t = tokens.load()
    brand = t["brand"] if theme == "dark" else t["light"][t["light"]["default"]]
    return SURFACE[theme] | {"letters": brand["letters"], "shadow": brand["shadow"], "accent": brand["tagline_color"]}


def _wrap(svg_inner: str, w: int, h: int, title: str, theme: str) -> str:
    bg = f'<rect width="{w}" height="{h}" fill="{SURFACE[theme]["bg"]}"/>' if SURFACE[theme]["bg"] else ""
    return (f'<svg xmlns="{svg.NS}" viewBox="0 0 {w} {h}" width="{w}" height="{h}" shape-rendering="crispEdges" role="img" font-family="{FONT}">'
            f"<title>{escape(title)}</title>{bg}{svg_inner}</svg>\n")


def _inline_symbol(sym_id: str, x: float, y: float, cell: int, theme: str) -> str:
    """A symbol placed inside a larger drawing: the symbol's own SVG body, shifted."""
    s = svg.symbol_svg(sym_id, cell=cell, color=svg.symbol_color(sym_id, dark=theme == "dark", ink=SURFACE[theme]["ink"]))
    body = s[s.index("</title>") + len("</title>"):-len("</svg>")]
    return f'<g transform="translate({x},{y})">{body}</g>'


def _box(x: int, y: int, w: int, h: int, c: dict) -> str:
    """Flat rectangle with the half-cell offset shadow behind it."""
    return (f'<rect x="{x + 6}" y="{y + 6}" width="{w}" height="{h}" fill="{c["shadow"]}"/>'
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{c["box"]}" stroke="{c["letters"]}" stroke-width="2"/>')


def _arrow(x: int, y: int, c: dict) -> str:
    """A pixel arrowhead pointing right, six 6-pixel cells."""
    cells = [(0, 0), (0, 1), (0, 2), (1, 1), (2, 0), (2, 1), (2, 2)]
    return "".join(f'<rect x="{x + col * 6}" y="{y + r * 6 - 6 + (0 if col == 0 else 0)}" width="6" height="6" fill="{c["accent"]}"/>' for r, col in [(0, 0), (1, 1), (2, 2), (3, 1), (4, 0)])


STAGES = [
    ("1", "Discover", "read-only probes of the machine and the local network", ["model", "guard", "script"]),
    ("2", "Pair", "a one-time code, a key exchange, one yes", ["owner", "guard"]),
    ("3", "Analyze", "typed facts with evidence: what the machine can run", ["model", "guard", "script"]),
    ("4", "Plan, with approval", "exact commands and an undo, approved before any write", ["model", "owner", "guard"]),
    ("5", "Route", "a written rule picks the model and machine the level allows", ["guard", "owner"]),
    ("6", "Log", "a hash-chained record of who did what", ["guard"]),
]


def flow_svg(theme: str) -> str:
    c = _theme(theme)
    bw, bh, gap, top, left = 172, 236, 34, 40, 30
    w = left * 2 + 6 * bw + 5 * gap
    h = top + bh + 90
    parts = []
    for i, (num, name, what, actors) in enumerate(STAGES):
        x = left + i * (bw + gap)
        parts.append(_box(x, top, bw, bh, c))
        parts.append(f'<text x="{x + 14}" y="{top + 30}" font-size="15" fill="{c["accent"]}" letter-spacing="2">{num}</text>')
        name_lines = ["Plan, with", "approval"] if name == "Plan, with approval" else [name]
        for j, line in enumerate(name_lines):
            parts.append(f'<text x="{x + 14}" y="{top + 56 + j * 20}" font-size="18" font-weight="bold" fill="{c["ink"]}">{escape(line)}</text>')
        words, line, lines = what.split(), "", []
        for wd in words:
            if len(line) + len(wd) + 1 > 18:
                lines.append(line)
                line = wd
            else:
                line = f"{line} {wd}".strip()
        lines.append(line)
        y0 = top + 56 + (len(name_lines) - 1) * 20 + 26
        for j, ln in enumerate(lines):
            parts.append(f'<text x="{x + 14}" y="{y0 + j * 16}" font-size="12" fill="{c["muted"]}">{escape(ln)}</text>')
        ay = top + bh - 18 - 24 * (len(actors) - 1) - 12
        for j, a in enumerate(actors):
            yy = ay + j * 24
            parts.append(_inline_symbol(a, x + 14, yy - 11, 2, theme))
            parts.append(f'<text x="{x + 42}" y="{yy + 3}" font-size="13" fill="{c["ink"]}">{ACTOR_WORD[a]}</text>')
        if i < 5:
            parts.append(_arrow(x + bw + 10, top + bh // 2, c))
    for j, ln in enumerate(["Every step of a run is tagged with who took it: OWNER, MODEL, GUARD or SCRIPT.", "The model chooses its own action from a closed menu; code only checks, runs and records."]):
        parts.append(f'<text x="{left}" y="{top + bh + 44 + j * 20}" font-size="13" fill="{c["muted"]}">{ln}</text>')
    return _wrap("".join(parts), w, h, "How it works: discover, pair, analyze, plan with approval, route, log", theme)


TILES = [
    ("macos", "macOS", "Apple Silicon", "WORKS", "done", "tested for real"),
    ("windows", "Windows 11", "x86_64", "WORKS", "done", "tested for real"),
    ("linux", "Linux", "Debian, Fedora, Alpine", "WORKS", "done", "tested in containers"),
    ("claude-desktop", "Claude Desktop", "chat, through MCP", "WORKS", "done", "tested for real"),
    ("open-webui", "Open WebUI", "chat, through MCP", "CONNECTED ONCE", "searching", "connected once by the owner"),
]


def tile_svg(tile: tuple, theme: str) -> str:
    _, name, sub, level, sym, note = tile
    c = _theme(theme)
    w, h = 272, 112
    inner = _box(0, 0, w - 8, h - 8, c)
    inner += f'<text x="16" y="34" font-size="19" font-weight="bold" fill="{c["ink"]}">{escape(name)}</text>'
    inner += f'<text x="16" y="54" font-size="12" fill="{c["muted"]}">{escape(sub)}</text>'
    inner += _inline_symbol(sym, 16, 66, 2, theme)
    inner += f'<text x="42" y="80" font-size="13" font-weight="bold" fill="{c["accent"]}" letter-spacing="1">{level}</text>'
    inner += f'<text x="16" y="98" font-size="10.5" fill="{c["muted"]}">{escape(note)}</text>'
    return _wrap(inner, w, h, f"{name}: {level.lower()}", theme)


def files() -> dict[str, str]:
    out = {}
    for theme in ("dark", "light"):
        out[f"flow-{theme}.svg"] = flow_svg(theme)
        for tile in TILES:
            out[f"tile-{tile[0]}-{theme}.svg"] = tile_svg(tile, theme)
        for sid in SYMBOLS:
            col = svg.symbol_color(sid, dark=theme == "dark", ink=SURFACE[theme]["ink"])
            out[f"symbols/{sid}-{theme}.svg"] = svg.symbol_svg(sid, cell=3, color=col) + "\n"
    return out


def main() -> int:
    fs = files()
    if "--check" in sys.argv:
        bad = [n for n, t in fs.items() if not (OUT / n).is_file() or (OUT / n).read_text(encoding="utf-8") != t]
        print("drift: " + ", ".join(bad) if bad else "assets match the generator")
        return 1 if bad else 0
    for name, text in fs.items():
        p = OUT / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    print(f"written: {len(fs)} files in assets/readme/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
