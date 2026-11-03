"""assets/brand/gallery.html: every logo variant and every symbol on a dark and a light background, for the owner to review."""

from __future__ import annotations

import html

from envrec_style import svg, tokens


def _symbol_cards(dark: bool) -> str:
    t = tokens.load()
    ink = "#E6E1F0" if dark else "#1B1530"
    out = []
    for g in t["symbol_groups"]:
        out.append(f'<h3>{html.escape(g)}</h3><div class="row">')
        for s in (x for x in tokens.symbols() if x["group"] == g):
            col = svg.symbol_color(s["id"], dark=dark, ink=ink)
            shadow = t["brand"]["shadow"] if dark else t["light"]["A"]["shadow"]
            out.append('<figure>' + svg.symbol_svg(s["id"], cell=6, color=col, shadow=shadow).replace("<svg ", '<svg class="sym" ', 1)
                       + f'<figcaption><b>{html.escape(tokens.symbol_text(s["id"]))}</b><br><code>{html.escape(tokens.symbol_text(s["id"], unicode=False))}</code><br>{html.escape(s["meaning"])}</figcaption></figure>')
        out.append("</div>")
    return "".join(out)


def _panel(dark: bool) -> str:
    t = tokens.load()
    variants = ["dark", "transparent"] if dark else ["light", "light-b"]
    names = {"dark": "dark, with background", "transparent": "transparent", "light": "light, default (A)", "light-b": "light, alternative (B)"}
    logos = "".join(f'<figure>{svg.logo_svg(v)}<figcaption>{names[v]}</figcaption></figure>' for v in variants)
    cr = ""
    if not dark:
        for k in ("A", "B"):
            c = t["light"][k]
            cr += f'<p>light {k}: letters {c["letters"]} on white {tokens.contrast(c["letters"], "#FFFFFF"):.2f}:1, shadow {c["shadow"]} {tokens.contrast(c["shadow"], "#FFFFFF"):.2f}:1, tagline {c["tagline_color"]} {tokens.contrast(c["tagline_color"], "#FFFFFF"):.2f}:1</p>'
    return f'<section class="{"dk" if dark else "lt"}"><h2>{"On dark" if dark else "On light"}</h2>{logos}{cr}{_symbol_cards(dark)}</section>'


def gallery_html() -> str:
    css = ("body{margin:0;font:14px/1.4 system-ui,sans-serif}section{padding:24px}.dk{background:#0A0614;color:#E6E1F0}.lt{background:#fff;color:#1B1530}"
           "h2{margin:0 0 12px}h3{margin:20px 0 8px;font-size:13px;text-transform:uppercase;letter-spacing:.1em;opacity:.7}.row{display:flex;flex-wrap:wrap;gap:16px}"
           "figure{margin:0 0 12px;max-width:220px}figure>svg:not(.sym){width:420px;max-width:100%;height:auto}.sym{width:48px;height:48px;display:block;margin-bottom:6px}figcaption{font-size:12px}code{font-size:11px}")
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>MAS brand gallery</title>'
            f'<style>{css}</style></head><body>{_panel(True)}{_panel(False)}</body></html>\n')
