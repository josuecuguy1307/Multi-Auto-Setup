"""Inline SVG icons in the project deck's drawing style: outlined computers with a dark screen and teal text lines, a coordinator disc, and the three ways to get an AI (chip, key, terminal).

Every function returns an SVG fragment for an HTML page (no namespace attribute needed, so the cards contain no URL at all). Colors come from CSS variables the card layer defines."""

from __future__ import annotations

DEVICE_KINDS = ("laptop", "desktop", "server", "oldpc", "computer")


def _screen(x: int, y: int, w: int, h: int) -> str:
    return (f'<rect class="scr" x="{x}" y="{y}" width="{w}" height="{h}" rx="2"/>'
            f'<path class="txt" d="M{x + 4} {y + 5}h{min(w - 8, 14)}M{x + 4} {y + 10}h{min(w - 8, 22)}M{x + 4} {y + 15}h{min(w - 8, 10)}"/>')


def device(kind: str) -> str:
    """A computer, 64 x 48 units, by kind: laptop, desktop, server, oldpc. Anything else is the generic desktop monitor (we never guess a kind we were not told)."""
    if kind == "laptop":
        return ('<rect class="body" x="12" y="4" width="40" height="28" rx="3"/>' + _screen(15, 7, 34, 22) + '<path class="body" d="M4 36h56l-4 6H8z"/>')
    if kind == "server":
        return ('<rect class="body" x="8" y="5" width="48" height="17" rx="3"/><rect class="body" x="8" y="26" width="48" height="17" rx="3"/>'
                '<circle class="dot" cx="16" cy="13.5" r="1.8"/><path class="body" d="M23 13.5h26"/><circle class="dot" cx="16" cy="34.5" r="1.8"/><path class="body" d="M23 34.5h26"/>')
    if kind == "oldpc":
        return ('<rect class="body old" x="3" y="6" width="38" height="30" rx="3"/>' + _screen(6, 9, 32, 24) + '<path class="body" d="M16 36v4h12v-4M12 42h20"/>'
                '<rect class="body old" x="45" y="8" width="14" height="34" rx="2"/><path class="body" d="M48 14h8M48 18h8"/><circle class="dot" cx="52" cy="35" r="1.8"/>')
    return ('<rect class="body" x="8" y="3" width="48" height="32" rx="3"/>' + _screen(11, 6, 42, 26) + '<path class="body" d="M28 35v5h8v-5M20 43h24"/>')


def coordinator() -> str:
    """The coordinator disc with the board's three columns (the deck's 'tablero compartido'), 40 x 40 units."""
    return '<circle class="ring" cx="20" cy="20" r="18"/><path class="bars" d="M12 12v16M20 12v10M28 12v13"/>'


def chip() -> str:
    return '<rect class="line" x="8" y="8" width="16" height="16" rx="2"/><path class="line" d="M12 4v4M16 4v4M20 4v4M12 24v4M16 24v4M20 24v4M4 12h4M4 16h4M4 20h4M24 12h4M24 16h4M24 20h4"/><rect class="line" x="12" y="12" width="8" height="8" rx="1"/>'


def key() -> str:
    return '<circle class="line" cx="10" cy="16" r="5"/><path class="line" d="M15 16h13M24 16v4M28 16v3"/>'


def terminal() -> str:
    return '<rect class="line" x="3" y="6" width="26" height="20" rx="3"/><path class="line" d="M8 12l5 4-5 4M16 21h7"/>'


def lock() -> str:
    return '<rect class="line" x="6" y="13" width="16" height="12" rx="2"/><path class="line" d="M9 13v-3a5 5 0 0 1 10 0v3"/>'


def cloud() -> str:
    return '<path class="line" d="M9 24a6 6 0 0 1 0-12 8 8 0 0 1 15 2 5 5 0 0 1 0 10z"/>'


def svg(inner: str, w: int, h: int, cls: str = "", label: str = "") -> str:
    a11y = f'role="img" aria-label="{label}"' if label else 'aria-hidden="true"'
    return f'<svg class="ic {cls}" viewBox="0 0 {w} {h}" width="{w}" height="{h}" {a11y}>{inner}</svg>'


MODE_ICON = {"local": chip, "api": key, "cli": terminal}
