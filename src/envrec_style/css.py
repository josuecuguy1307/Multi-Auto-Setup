"""The tokens as CSS custom properties, for the panel, the cards and any page."""

from __future__ import annotations

from envrec_style import tokens


def css_variables() -> str:
    t = tokens.load()
    b = t["brand"]
    sem = t["semantic"]
    lines = [f"--mas-letters:{b['letters']}", f"--mas-shadow:{b['shadow']}", f"--mas-tagline:{b['tagline_color']}", f"--mas-bg:{b['background']}"]
    for name, v in sem.items():
        if name.startswith("_"):
            continue
        lines += [f"--mas-{name}:{v['on_light']}", f"--mas-{name}-on-dark:{v['on_dark']}"]
    return ":root{" + ";".join(lines) + "}"
