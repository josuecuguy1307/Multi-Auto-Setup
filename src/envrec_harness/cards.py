"""The shared card layer: self-contained HTML built once from an intent's data, wrapped per host by `host.py`.

Cards draw what a chat cannot: the live network map (each machine as its own computer icon, the coordinator in the center, lines that animate while a task runs), a machine card with the
evidence state of every fact, the options compared with symbols, the setup plan with its exact commands and undo, and a run with who decided each step. They load NOTHING from outside (no
font, no script, no image, no URL): system fonts, inline SVG, inline CSS. Every word that comes from a machine or a model is escaped. The visual language is the project deck's: navy and cream,
teal = the data stays on the machine, amber = it goes to the cloud, a lock = confidential.

Cards hold no decisions. A button only names an option the harness already offered (`data-choose`) and the host decides whether it can deliver it."""

from __future__ import annotations

import html
import math
import re

from envrec_harness import icons
from envrec_style import svg as style_svg
from envrec_style import tokens
from envrec_style.tokens import semantic
from envrec_harness.strings import STR, t

def _mix(a: str, b: str, w: float) -> str:
    """`a` with a share `w` of `b`: every surface and ink of the cards is made from the style tokens' own colors (brand, teal, amber), never typed here."""
    ca, cb = tokens.hex_to_rgb(a), tokens.hex_to_rgb(b)
    return "#" + "".join(f"{round(x * (1 - w) + y * w):02x}" for x, y in zip(ca, cb))


_BRAND, _LIGHT = tokens.logo_colors("dark"), tokens.logo_colors("light")
_BG, _WHITE = _BRAND["background"], "#ffffff"
PALETTE = {  # (name: hex). tests/test_harness_cards.py checks every text/background pair below at 4.5:1 or better
    "navy": _BG, "navy2": _mix(_BG, _BRAND["letters"], .12), "navy3": _mix(_BG, _BRAND["letters"], .32), "cream": _mix(_WHITE, _BG, .04), "cream2": _mix(_WHITE, _LIGHT["letters"], .10),
    "card": _mix(_WHITE, _LIGHT["letters"], .03), "line": _mix(_WHITE, _BG, .22), "ink": _BG, "muted": _mix(_BG, _WHITE, .34),
    "teal": semantic("teal", dark=False), "teal_bright": semantic("teal", dark=True), "amber": semantic("amber", dark=False), "amber_bright": semantic("amber", dark=True),
    "blue": _LIGHT["letters"], "blue_b": _BRAND["letters"], "mag": _LIGHT["tagline_color"], "mag_b": _BRAND["tagline_color"], "shadow": _BRAND["shadow"],
    "sub_on_navy": _mix(_WHITE, _BG, .32), "offline": _mix(_WHITE, _BG, .5),
}
PALETTE |= {"teal_ink": _mix(PALETTE["teal"], _BG, .5), "teal_lite": _mix(PALETTE["teal"], _WHITE, .86), "amber_ink": _mix(PALETTE["amber"], _BG, .5), "amber_lite": _mix(PALETTE["amber"], _WHITE, .85)}
CONTRAST_PAIRS = [("ink", "card"), ("muted", "card"), ("teal", "card"), ("amber", "card"), ("teal_ink", "teal_lite"), ("amber_ink", "amber_lite"), ("cream", "navy"), ("cream", "navy2"),
                  ("sub_on_navy", "navy"), ("sub_on_navy", "navy2"), ("teal_bright", "navy"), ("teal_bright", "navy2"), ("amber_bright", "navy"), ("amber_bright", "navy2"), ("cream", "teal"), ("ink", "cream2"),
                  ("cream", "blue"), ("blue", "card"), ("mag", "card"), ("mag_b", "navy"), ("blue_b", "navy"), ("muted", "cream2")]

CSS = """
:root{--navy:%(navy)s;--navy2:%(navy2)s;--navy3:%(navy3)s;--cream:%(cream)s;--cream2:%(cream2)s;--card:%(card)s;--line:%(line)s;--ink:%(ink)s;--muted:%(muted)s;--teal:%(teal)s;--teal-ink:%(teal_ink)s;
--blue:%(blue)s;--blue-b:%(blue_b)s;--mag:%(mag)s;--mag-b:%(mag_b)s;--shadow:%(shadow)s;--teal-lite:%(teal_lite)s;--teal-b:%(teal_bright)s;--amber:%(amber)s;--amber-ink:%(amber_ink)s;--amber-lite:%(amber_lite)s;--amber-b:%(amber_bright)s;--sub:%(sub_on_navy)s;--off:%(offline)s;
--font:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
*{box-sizing:border-box}html,body{margin:0;background:transparent;font:14px/1.45 var(--font);color:var(--ink)}
.mas{margin:0 0 12px}.mas:last-child{margin-bottom:0}
.card{background:var(--card);border:2px solid var(--ink);padding:14px 16px;box-shadow:4px 4px 0 var(--shadow);margin-right:4px}
.card h3{margin:0 0 2px;font-size:16px}.card .sub{margin:0 0 10px;color:var(--muted);font-size:13px}
.k{font:600 11px/1 var(--mono);letter-spacing:.2em;text-transform:uppercase;color:var(--mag)}
.dark{background:var(--navy);border-color:var(--blue-b);color:var(--cream)}.dark .k{color:var(--mag-b)}.dark .sub{color:var(--sub)}
.pill{display:inline-block;padding:1px 8px;border-radius:0;box-shadow:inset 0 0 0 1px currentColor;font-size:12px;font-weight:600;background:var(--teal-lite);color:var(--teal-ink);white-space:nowrap}
.pill.st-unknown,.pill.st-hypothesis,.pill.st-none{background:var(--amber-lite);color:var(--amber-ink)}.pill.st-inferred,.pill.st-declared{background:var(--cream2);color:var(--ink)}
.pill.cloud{background:var(--amber-lite);color:var(--amber-ink)}.pill.local{background:var(--teal-lite);color:var(--teal-ink)}
.rows{width:100%%;border-collapse:collapse}.rows td{padding:5px 0;border-top:1px solid var(--line);vertical-align:top}.rows tr:first-child td{border-top:0}
.rows td:first-child{color:var(--muted);width:38%%;padding-right:10px}.rows td:last-child{text-align:right}.val{font-weight:600;word-break:break-word}
.grp{margin:12px 0 4px}
.ic{display:block}.ic .body,.map .body{fill:var(--cream);stroke:var(--navy);stroke-width:2;stroke-linejoin:round;stroke-linecap:round}.ic .old,.map .old{fill:#e8dcc0}
.ic .scr,.map .scr{fill:var(--navy)}.ic .txt,.map .txt{stroke:var(--teal-b);stroke-width:1.6;stroke-linecap:round;fill:none}.ic .dot,.map .dot{fill:var(--teal)}.ic .line,.map .line{fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.ic .ring,.map .ring{fill:var(--navy2);stroke:var(--teal-b);stroke-width:2}.ic .bars,.map .bars{stroke:var(--teal-b);stroke-width:3;stroke-linecap:round;fill:none}
.sym{display:inline-flex;align-items:center;justify-content:center;width:44px;height:44px;border:2px solid;flex:none}.px{fill:currentColor}.pxs{fill:var(--shadow)}.pix{display:inline-block;vertical-align:-3px;flex:none}.logo{float:right;width:84px;height:auto}.sym.local{color:var(--teal);background:var(--teal-lite);border-color:var(--teal)}
.sym.cloud{color:var(--amber);background:var(--amber-lite);border-color:var(--amber)}
.map{display:block;width:100%%;height:auto}.mas-node{cursor:pointer;outline:none}.mas-node:focus-visible .halo,.mas-node:hover .halo{stroke:var(--teal-b);stroke-width:2}
.halo{fill:transparent;stroke:transparent}.nm{fill:var(--cream);font:600 13px var(--font);text-anchor:middle}.os{fill:var(--sub);font:12px var(--font);text-anchor:middle}.act{fill:var(--teal-b);font:600 11px var(--mono);text-anchor:middle}.act .who{fill:var(--cream)}
.mas-link{fill:none;stroke-width:2;stroke:var(--teal-b);opacity:.55}.mas-link.route-cloud{stroke:var(--amber-b);stroke-dasharray:3 5}.mas-link.offline{stroke:var(--off);stroke-dasharray:2 6;opacity:.5}
.mas-link.active{opacity:1;stroke-dasharray:7 7;animation:flow 1s linear infinite}@keyframes flow{to{stroke-dashoffset:-14}}
.dev{color:var(--blue-b)}.mas-node:hover .dev,.mas-node:focus-visible .dev{color:var(--mag-b)}.dev .pxs{fill:var(--shadow)}.lockg .px{color:var(--cream)}.st-dot{stroke:var(--navy);stroke-width:2}.st-dot.online{fill:var(--teal-b)}.st-dot.busy{fill:var(--cream);animation:pulse 1.2s ease-in-out infinite}.st-dot.offline{fill:var(--navy);stroke:var(--off)}
@keyframes pulse{50%%{opacity:.35}}@media (prefers-reduced-motion:reduce){.mas-link.active,.st-dot.busy{animation:none}}
.lock-badge{fill:var(--navy);stroke:var(--cream);stroke-width:2}.cloudc .px{color:var(--amber-b)}.lock-ic{stroke:var(--cream);fill:none;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}.lockl{display:inline-block;vertical-align:-3px;color:var(--cream)}
.legend{display:flex;flex-wrap:wrap;gap:6px 18px;margin-top:8px;font-size:12px;color:var(--sub)}.legend i{display:inline-block;width:18px;height:0;border-top:3px solid var(--teal-b);vertical-align:middle;margin-right:6px}
.legend i.c{border-top:3px dotted var(--amber-b)}.lg-local .px{color:var(--teal-b)}.lg-cloud .px{color:var(--amber-b)}.lg-lock .px{color:var(--cream)}.legend .pix{margin-right:6px;vertical-align:-2px}
.mas-detail{margin-top:12px;background:var(--navy2);border:2px solid var(--blue-b);box-shadow:3px 3px 0 var(--shadow);padding:10px 14px;color:var(--cream)}.mas-detail h4{margin:0 0 4px;font-size:14px}
.mas-detail .rows td:first-child{color:var(--sub)}.mas-detail .rows td{border-color:var(--navy3)}.mas-detail .pill{background:var(--teal-b);color:var(--navy)}
.mas-detail .pill.st-unknown{background:var(--amber-b);color:var(--navy)}.mas-detail .pill.st-inferred,.mas-detail .pill.st-declared{background:var(--navy3);color:var(--cream)}
.opt{display:flex;gap:12px;padding:12px 0;border-top:1px solid var(--line)}.opt:first-of-type{border-top:0}.opt.picked{background:var(--cream2);margin:0 -16px;padding:12px 16px;border-left:4px solid var(--blue)}
.opt .b{flex:1;min-width:0}.opt h4{margin:0 0 3px;font-size:14px}.opt p{margin:3px 0 0;color:var(--muted);font-size:13px}.opt .tags{display:flex;gap:6px;flex-wrap:wrap;margin-top:4px}.opt .tags .pill{white-space:normal}
.btn{font:600 13px var(--font);padding:6px 14px;border-radius:0;border:2px solid var(--ink);background:var(--blue);color:var(--cream);cursor:pointer;margin:6px 3px 0 0;box-shadow:3px 3px 0 var(--shadow)}.btn:hover{transform:translate(1px,1px);box-shadow:2px 2px 0 var(--shadow)}.btn:active{transform:translate(3px,3px);box-shadow:none}.btn:focus-visible{outline:3px solid var(--mag);outline-offset:2px}
.step{display:grid;grid-template-columns:28px 1fr;gap:4px 10px;padding:10px 0;border-top:1px solid var(--line)}.step:first-of-type{border-top:0}
.step .n{width:24px;height:24px;border-radius:0;background:var(--blue);color:var(--cream);font:700 12px/24px var(--mono);text-align:center}.step.needs .n{background:var(--mag)}
.step h4{margin:0;font-size:14px}pre{margin:5px 0 0;padding:8px 10px;background:var(--navy);color:var(--cream);border-radius:0;border-left:4px solid var(--blue-b);font:12px/1.4 var(--mono);white-space:pre-wrap;word-break:break-all}
.step .meta{color:var(--muted);font-size:12px;margin-top:3px}.step .undo{color:var(--amber-ink);font-size:12px;margin-top:2px}
.ev{display:flex;gap:8px;align-items:baseline;padding:4px 0;border-top:1px solid var(--line);font-size:13px}.ev span:last-child{min-width:0;overflow-wrap:anywhere}.ev:first-of-type{border-top:0}
.tag{font:700 10px/1 var(--mono);letter-spacing:.06em;padding:3px 6px;border-radius:0;flex:none;min-width:58px;text-align:center;background:var(--navy);color:var(--cream)}
.tag.MODEL{background:var(--teal);color:var(--cream)}.tag.OWNER{background:var(--teal-lite);color:var(--teal-ink)}.tag.SCRIPT,.tag.CLAUDE-CODE{background:var(--amber-lite);color:var(--amber-ink)}
.bar{display:block;width:100%%;height:10px;border-radius:0;margin:8px 0 4px}.bar .bg{fill:var(--cream2)}
.bar .MODEL{fill:var(--teal)}.bar .GUARD{fill:var(--navy)}.bar .SCRIPT{fill:var(--amber)}.bar .OWNER{fill:var(--teal-b)}.bar .CLAUDE-CODE{fill:var(--amber-b)}.tight{margin:4px 0}.cloudc{color:var(--amber-b)}
.note{margin-top:10px;padding:8px 12px;border-radius:0;border-left:4px solid currentColor;background:var(--cream2);color:var(--ink);border-left-color:var(--blue);font-size:13px}.note.warn{background:var(--cream2);color:var(--ink);border-left-color:var(--mag)}
.framed{overflow:hidden}.fit{margin:6px 0 0;font-size:13px}.fit b{font-weight:700}.fitrow{display:flex;gap:6px;align-items:baseline;flex-wrap:wrap}.live{display:inline-block;width:8px;height:8px;border-radius:0;background:var(--teal-b);margin-right:6px;animation:pulse 1.2s ease-in-out infinite}.nested{margin-top:8px}.nested .card{background:var(--cream);color:var(--ink)}.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
""" % PALETTE

ACTOR_ORDER = ("MODEL", "GUARD", "SCRIPT", "OWNER", "CLAUDE-CODE")
ACTOR_SYMBOL = {"MODEL": "model", "GUARD": "guard", "SCRIPT": "script", "OWNER": "owner"}
MODE_SYMBOL = {"local": "chip", "api": "key", "cli": "terminal"}
DEVICE_SYMBOL = {"laptop": "laptop", "desktop": "desktop", "server": "server", "oldpc": "oldpc", "computer": "desktop"}  # (a kind we were not told is drawn as a plain desktop, never guessed)


def pix(sym_id: str, cell: int = 3, shadow: bool = True) -> str:
    """A symbol of the style set as flat rectangles with the half-cell shadow, for an HTML page: no namespace (the cards hold no URL at all) and no inline color (a class takes it, so the panel's strict policy holds)."""
    raw = style_svg.symbol_svg(sym_id, cell=cell, color="#000001", shadow="#000002" if shadow else None)
    raw = raw.replace(f' xmlns="{style_svg.NS}"', "").replace('fill="#000001"', 'class="px"').replace('fill="#000002"', 'class="pxs"')
    return raw.replace("<svg ", '<svg class="pix" ', 1)  # (its <title> is the word, so a screen reader reads the shape too)



def esc(v) -> str:
    return html.escape(str(v), quote=True)


def fmt_value(v, cap: int = 120) -> str:
    if v is None or v == "":
        return "—"
    if isinstance(v, bool):
        return t("en", "yes") if v else t("en", "no")
    if isinstance(v, float):
        v = round(v, 2)
    if isinstance(v, (list, tuple)):
        parts = [fmt_value(x.get("name") or x.get("id") or x, 40) if isinstance(x, dict) else fmt_value(x, 40) for x in v[:6]]  # a list of things reads as their names
        return ", ".join(parts) + (f" +{len(v) - 6}" if len(v) > 6 else "") if parts else "—"
    if isinstance(v, dict):
        parts = [f"{k}: {fmt_value(x, 30)}" for k, x in list(v.items())[:4]]
        return "; ".join(parts) + ("…" if len(v) > 4 else "")
    s = "".join(c for c in str(v) if c.isprintable())
    return s if len(s) <= cap else s[: cap - 1] + "…"


def pill(status: str, lang: str) -> str:
    s = status if status in ("tested", "inferred", "declared", "verified", "hypothesis", "unknown") else "none"
    return f'<span class="pill st-{s}">{esc(t(lang, s) if s != "none" else status)}</span>'


def size_text(n, kind, lang: str) -> str:
    """A size as the step really uses it: a move says "moved, not downloaded" (a step that moves a folder downloads nothing)."""
    s = human_bytes(n)
    return t(lang, "size_moved").format(size=s) if kind == "move" and s != "—" else s


def human_bytes(n) -> str:
    if not isinstance(n, (int, float)) or n <= 0:
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return "—"


# ---- the network map ---------------------------------------------------------------------------------------------------------------------------

def network(data: dict, lang: str = "en", actions: bool = True) -> str:
    nodes = data.get("nodes") or []
    W, H = (640, 380) if len(nodes) >= 3 else (440, 290)  # a small network is drawn small: nothing is lost in empty space on a narrow chat
    cx, cy = W // 2, H // 2 - 14
    parts = [f'<div class="mas card dark" data-card="network"><div class="k">{esc(t(lang, "network"))}</div>']
    if not nodes:
        parts.append(f'<p class="sub">{esc(t(lang, "no_nodes"))}</p></div>')
        return "".join(parts)
    label = f'{t(lang, "network")}: ' + ", ".join(f'{n.get("name", "?")} ({t(lang, n.get("status", "offline"))})' for n in nodes)
    svg = [f'<svg class="map" viewBox="0 0 {W} {H}" role="group" aria-label="{esc(label)}">']  # (the icons are drawn 1.25x so they read on a phone-width chat)
    pos = []
    for i, n in enumerate(nodes):
        a = -math.pi / 2 + 2 * math.pi * i / len(nodes) + (math.pi / len(nodes) if len(nodes) > 1 else math.pi / 2)
        rx, ry = 0.34 * W, 0.30 * H
        pos.append((cx + rx * math.cos(a), cy + ry * math.sin(a)) if len(nodes) > 1 else (cx + 0.32 * W, cy))
    for n, (x, y) in zip(nodes, pos):
        cls = ["mas-link", "route-cloud" if n.get("route") == "cloud" else "route-local"]
        if n.get("status") == "offline":
            cls.append("offline")
        elif n.get("active"):
            cls.append("active")
        svg.append(f'<path class="{" ".join(cls)}" d="M{cx} {cy}L{x:.1f} {y:.1f}"/>')
    if data.get("clouds"):
        names = ", ".join(str(c.get("name", "")) for c in data["clouds"])
        svg.append(f'<g class="cloudc" transform="translate({W - 80} 8)"><rect x="-6" y="-2" width="76" height="52" fill="none" stroke="var(--amber-b)" stroke-width="2" stroke-dasharray="4 4"/>'
                   f'<g transform="translate(18 2)">{pix("cloud", 3)}</g><text class="os" x="32" y="46">{esc(names[:14])}</text></g>')
    svg.append(f'<g transform="translate({cx - 28} {cy - 32})" role="img" aria-label="{esc(t(lang, "coordinator"))}">{pix("coordinator", 7).replace("<svg ", '<svg x="0" y="0" ', 1)}</g><text class="nm" x="{cx}" y="{cy + 54}">{esc(data.get("coordinator", {}).get("name") or t(lang, "coordinator"))}</text>')
    for n, (x, y) in zip(nodes, pos):
        st = n.get("status") if n.get("status") in ("online", "busy", "offline") else "offline"
        kind = n.get("kind") if n.get("kind") in DEVICE_SYMBOL else "computer"
        tab = ' tabindex="0" role="button"'
        svg.append(f'<g class="mas-node" data-node="{esc(n.get("id", ""))}"{tab} aria-label="{esc(n.get("name", "?"))}, {esc(t(lang, st))}" transform="translate({x - 40:.1f} {y - 30:.1f}) scale(1.25)">'
                   f'<rect class="halo" x="-8" y="-6" width="80" height="84"/><g transform="translate(8 -4)" class="dev {esc(kind)}">{pix(DEVICE_SYMBOL[kind], 6)}</g>'
                   f'<rect class="st-dot {st}" x="56" y="-2" width="12" height="12"><title>{esc(t(lang, st))}</title></rect>'
                   + (f'<g transform="translate(-4 26)" class="lockg"><rect class="lock-badge" x="-3" y="-3" width="22" height="22"/><g transform="translate(0 0)">{pix("confidential", 2, False)}</g></g>' if n.get("confidential") else "")
                   + f'<text class="nm" x="32" y="62">{esc(str(n.get("name", "?"))[:22])}</text><text class="os" x="32" y="77">{esc(str(n.get("os", ""))[:22])}</text>'
                   + (f'<text class="act" x="32" y="93"><tspan class="who">{esc(str(n["activity"].get("actor", ""))[:11])}</tspan> {esc(str(n["activity"].get("text", ""))[:20])}</text>' if n.get("activity") else "") + "</g>")
    svg.append("</svg>")
    parts.append("".join(svg))
    parts.append(f'<div class="legend"><span class="lg-local">{pix("local", 2, False)}<i></i>{esc(t(lang, "legend_local"))}</span><span class="lg-cloud">{pix("cloud", 2, False)}<i class="c"></i>{esc(t(lang, "legend_cloud"))}</span>'
                 f'<span class="lg-lock">{pix("confidential", 2, False)} {esc(t(lang, "legend_lock"))}</span><span>{esc(t(lang, "click"))}</span></div>')
    for n in nodes:
        rows = "".join(f'<tr><td>{esc(f.get("label", ""))}</td><td><span class="val">{esc(fmt_value(f.get("value")))}</span> {pill(str(f.get("status", "unknown")), lang)}</td></tr>' for f in (n.get("facts") or [])[:8])
        seen = f' · {esc(t(lang, "last_seen"))}: {esc(n["last_seen"])}' if n.get("last_seen") else ""
        act = n.get("activity")
        now = (f'<div class="sub"><span class="live"></span>{esc(t(lang, "doing"))}: <b>{esc(fmt_value(act.get("text"), 120))}</b> · {esc(t(lang, "actor"))}: <span class="tag {esc(act.get("actor", "GUARD")) if act.get("actor") in ACTOR_ORDER else "GUARD"}">{esc(act.get("actor", ""))}</span></div>'
               if act else f'<div class="sub">{esc(t(lang, "idle"))}</div>')
        nested = f'<div class="nested">{machine(n["machine"], lang, False)}</div>' if n.get("machine") else ""
        parts.append(f'<section class="mas-detail" data-for="{esc(n.get("id", ""))}" hidden><h4>{esc(n.get("name", "?"))}</h4><div class="sub">{esc(n.get("os", ""))} · {esc(t(lang, n.get("status", "offline")))}{seen}</div>{now}'
                     + (f'<table class="rows">{rows}</table>' if rows and not nested else "") + (nested if nested else ("" if rows else f'<div class="sub">{esc(t(lang, "unknown"))}</div>')) + "</section>")
    parts.append('<ul class="sr">' + "".join(f'<li>{esc(n.get("name", "?"))}: {esc(t(lang, n.get("status", "offline")))}</li>' for n in nodes) + "</ul></div>")
    return "".join(parts)


# ---- machine ------------------------------------------------------------------------------------------------------------------------------------

def machine(data: dict, lang: str = "en", actions: bool = True) -> str:
    out = [f'<div class="mas card" data-card="machine"><div class="k">{esc(t(lang, "machine"))}</div><h3>{pix("node", 3)} {esc(data.get("name", "?"))}</h3><p class="sub">{esc(data.get("os", ""))}'
           + (f' · {esc(data["label"])}' if data.get("label") else "") + "</p>"]
    for g in ("system", "hardware", "ai"):
        rows = (data.get("groups") or {}).get(g) or []
        if not rows:
            continue
        out.append(f'<div class="k grp">{esc(t(lang, g))}</div><table class="rows">' + "".join(
            f'<tr><td>{esc(r.get("label", ""))}</td><td><span class="val">{esc(fmt_value(r.get("value")))}</span> {pill(str(r.get("status", "unknown")), lang)}</td></tr>' for r in rows) + "</table>")
    if data.get("findings"):
        out.append(f'<div class="k grp">{esc(t(lang, "findings"))}</div>' + "".join(f'<p class="sub tight">{esc(fmt_value(f.get("text"), 300))} {pill(str(f.get("status", "hypothesis")), lang)}</p>' for f in data["findings"][:5]))
    out.append("</div>")
    return "".join(out)


# ---- options --------------------------------------------------------------------------------------------------------------------------------------

def fit_rows(o: dict, lang: str) -> list[dict]:
    """The numbers that let the owner compare: per model name, quantization, file size, what it needs, what may be used and whether it fits. Cloud options say they do not apply."""
    f = o.get("fit") or {}
    if not f:
        return []
    if o.get("kind") != "local":
        return [{"model": f.get("model"), "note": f.get("note")}]
    if f.get("models") is not None:
        return [{"model": m["name"], "quant": m.get("quantization"), "size": m.get("size_gib"), "needs": m.get("size_gib"), "fits": m.get("fits")} for m in f["models"]] or [{"model": f.get("model"), "note": None}]
    return [{"model": f.get("model"), "quant": f.get("quantization"), "size": f.get("size_gib"), "needs": f.get("needed_gib"), "fits": f.get("fits")}]


def fit_line(r: dict, f: dict, lang: str) -> str:
    if r.get("note"):
        return f'{r.get("model")}: {r["note"]}'
    bits = [str(r.get("model") or "—"), f'{r["quant"]}' if r.get("quant") else t(lang, "quant_unknown"), f'{t(lang, "size")} {r["size"]} GiB' if r.get("size") is not None else f'{t(lang, "size")} —']
    if r.get("needs") is not None:
        bits.append(f'{t(lang, "needs")} {r["needs"]} GiB')
    bits.append(f'{t(lang, "budget")} {f.get("budget_gib")} GiB ({f.get("available_gib")} {t(lang, "free")}, {f.get("total_gib")} {t(lang, "total")})' if f.get("budget_gib") is not None else "")
    bits.append(t(lang, "fits") if r.get("fits") else t(lang, "nofit") if r.get("fits") is False else "")
    return " · ".join(b for b in bits if b)


def options(data: dict, lang: str = "en", actions: bool = True) -> str:
    out = [f'<div class="mas card" data-card="options"><div class="k">{esc(t(lang, "options"))}</div><div class="note">{esc(t(lang, "no_reco"))}</div>']
    intent = esc(data.get("intent_id", ""))
    for o in data.get("options") or []:
        mode = o.get("kind") if o.get("kind") in icons.MODE_ICON else "local"
        cloud = bool(o.get("cloud"))
        sym = pix(MODE_SYMBOL[mode], 4)
        tags = [f'<span class="pill {"cloud" if cloud else "local"}">{pix("cloud" if cloud else "local", 2, False)} {esc(t(lang, mode))} · {esc(t(lang, "legend_cloud" if cloud else "legend_local"))}</span>']
        if o.get("provisional", True):
            tags.append(f'<span class="pill st-unknown">{esc(t(lang, "provisional"))}</span>')
        if o.get("label"):
            tags.append(f'<span class="pill st-inferred">{esc(fmt_value(o["label"], 70))}</span>')
        f = o.get("fit") or {}
        fits = "".join(f'<p class="fit">{esc(fit_line(r, f, lang))}</p>' for r in fit_rows(o, lang))
        expl = (f'<p>{esc(fmt_value(o["explanation"], 500))} <span class="pill st-hypothesis">{esc(t(lang, "expl"))}</span></p>' if o.get("explanation") else "")
        lim = "; ".join(map(str, o.get("limits") or []))
        btn = f'<button class="btn" data-intent="{intent}" data-choose="{esc(o.get("id", ""))}">{esc(t(lang, "pick"))}</button>' if actions and data.get("pickable") and not data.get("picked") else ""
        out.append(f'<div class="opt{" picked" if data.get("picked") == o.get("id") else ""}"><span class="sym {"cloud" if cloud else "local"}">{sym}</span><div class="b"><h4>{esc(o.get("title", o.get("id", "?")))}</h4>'
                   f'<div class="tags">{"".join(tags)}</div>{fits}{expl}' + (f'<p>{esc(fmt_value(lim, 400))}</p>' if lim else "") + btn + "</div></div>")
    out.append(f'<div class="note">{esc(t(lang, "install_note"))}</div></div>')
    return "".join(out)


# ---- the setup plan (T3a's `setup_plan`) ---------------------------------------------------------------------------------------------------------

def plan(data: dict, lang: str = "en", actions: bool = True) -> str:
    fit = data.get("fit") or {}
    out = [f'<div class="mas card" data-card="plan"><div class="k">{esc(t(lang, "plan"))}</div><h3>{esc(data.get("option_id", ""))}</h3><p class="sub">{esc(data.get("machine_id", ""))} · {esc(data.get("sensitivity", ""))}'
           + (f' · <span class="pill {"local" if fit.get("fits") else "cloud"}">{esc(t(lang, "fits" if fit.get("fits") else "nofit"))}</span>' if fit else "") + "</p>"]
    for s in data.get("steps") or []:
        needs = bool(s.get("needs_owner_approval"))
        status = str(s.get("status", "pending"))
        out.append(f'<div class="step{" needs" if needs else ""}"><span class="n">{esc(s.get("n", ""))}</span><div><h4>{esc(s.get("title", ""))} <span class="pill st-{"inferred" if status in ("pending", "approved") else "tested"}">'
                   f'{esc(t(lang, "status_" + status) if ("status_" + status) in STR["en"] else status)}</span></h4>'
                   f'<pre aria-label="{esc(t(lang, "command"))}">{esc(s.get("exact_command", ""))}</pre>'
                   f'<div class="meta">{esc(t(lang, "writes"))}: {esc(", ".join(map(str, s.get("writes") or [])) or "—")} · {esc(t(lang, "size"))}: {esc(size_text(s.get("bytes"), s.get("size_kind"), lang))}</div>'
                   f'<div class="undo">{esc(t(lang, "undo"))}: {esc(s.get("undo", ""))}</div></div></div>')
    v = data.get("verification") or {}
    if v:
        out.append(f'<div class="note{"" if v.get("ok") else " warn"}">{esc(t(lang, "verify"))}: {esc(v.get("endpoint", ""))} · {esc(t(lang, "ok" if v.get("ok") else "not_ok"))}</div>')
    if not data.get("can_approve_here"):
        out.append(f'<div class="note warn">{esc(t(lang, "approve_panel"))}</div>')
    out.append("</div>")
    return "".join(out)


# ---- a run, with who decided each step -----------------------------------------------------------------------------------------------------------

def run(data: dict, lang: str = "en", actions: bool = True) -> str:
    r = data.get("run") or {}
    actors = data.get("actors") or {}
    total = sum(int(actors.get(a, 0)) for a in ACTOR_ORDER) or 1
    out = [f'<div class="mas card" data-card="run"><div class="k">{esc(t(lang, "run"))}</div><h3>{esc(r.get("kind", ""))} · {esc(r.get("node", ""))}</h3><p class="sub">{esc(r.get("status", ""))}'
           + (f' — {esc(fmt_value(r["reason"], 160))}' if r.get("reason") else "") + f'</p><svg class="bar" viewBox="0 0 100 10" preserveAspectRatio="none" role="img" aria-label="{esc(t(lang, "actors"))}"><rect class="bg" width="100" height="10"/>']
    x = 0.0
    for a in ACTOR_ORDER:
        w = 100 * int(actors.get(a, 0)) / total if actors.get(a) else 0
        if w:
            out.append(f'<rect class="{a}" x="{x:.2f}" width="{w:.2f}" height="10"/>')
            x += w
    out.append("</svg><div class=\"sub\">" + " · ".join(f"{a} {int(actors.get(a, 0))}" for a in ACTOR_ORDER) + "</div>")
    for e in (data.get("events") or [])[-12:]:
        tag = e.get("tag") if e.get("tag") in ACTOR_ORDER else "GUARD"
        out.append(f'<div class="ev"><span class="tag {tag}">{pix(ACTOR_SYMBOL[tag], 2, False) if tag in ACTOR_SYMBOL else ""} {esc(tag)}</span><span>{esc(fmt_value(e.get("text"), 200))}</span></div>')
    out.append("</div>")
    return "".join(out)


RENDER = {"network": network, "machine": machine, "options": options, "plan": plan, "run": run}


def render(data_kind: str, data: dict, lang: str = "en", actions: bool = True) -> str:
    return RENDER[data_kind](data, lang, actions)


# ---- the text every chat can show --------------------------------------------------------------------------------------------------------------

def fallback_text(data_kind: str, data: dict, lang: str = "en") -> str:
    if data_kind == "network":
        return network_text(data, lang)
    if data_kind == "machine":
        lines = [f'{t(lang, "machine")}: {fmt_value(data.get("name"))} ({fmt_value(data.get("os"))})']
        for g in ("system", "hardware", "ai"):
            for r in (data.get("groups") or {}).get(g) or []:
                lines.append(f'- {r.get("label")}: {fmt_value(r.get("value"))} [{t(lang, r.get("status", "unknown")) if r.get("status") else t(lang, "unknown")}]')
        for f in (data.get("findings") or [])[:5]:
            lines.append(f'* {fmt_value(f.get("text"), 300)} [{f.get("status", "hypothesis")}]')
        return "\n".join(lines)
    if data_kind == "options":
        lines = [t(lang, "options") + " — " + t(lang, "no_reco")]
        for o in data.get("options") or []:
            f = o.get("fit") or {}
            lines.append(f'- [id: {o.get("id")}] {fmt_value(o.get("title"), 100)} ({t(lang, o.get("kind", "local"))}, {t(lang, "provisional")})')
            lines += [f"    {fit_line(r, f, lang)}" for r in fit_rows(o, lang)]
            if o.get("explanation"):
                lines.append(f'    {fmt_value(o["explanation"], 500)} [{t(lang, "expl")}]')
        lines.append(t(lang, "install_note"))
        return "\n".join(lines)
    if data_kind == "plan":
        return plan_text(data, lang)
    r = data.get("run") or {}
    lines = [f'{t(lang, "run")} {fmt_value(r.get("kind"))} · {fmt_value(r.get("node"))}: {fmt_value(r.get("status"))}' + (f' — {fmt_value(r["reason"], 200)}' if r.get("reason") else ""),
             f'{t(lang, "actors")}: ' + ", ".join(f"{a} {int((data.get('actors') or {}).get(a, 0))}" for a in ACTOR_ORDER)]
    lines += [f'- [{e.get("tag") if e.get("tag") in ACTOR_ORDER else "GUARD"}] {fmt_value(e.get("text"), 200)}' for e in (data.get("events") or [])[-8:]]
    return "\n".join(lines)


def legend_lines(text: str, lang: str = "en") -> list[str]:
    """The words a reader needs for the stand-ins this text uses: `MAS_HOME` (the machine's Multi Auto Setup folder) and `~` (your home folder)."""
    out = []
    if "MAS_HOME" in text:
        out.append(t(lang, "mas_home_legend"))
    if re.search(r"(^|[\s\"'(\[])~/", text):
        out.append(t(lang, "home_legend"))
    return out


def plan_text(data: dict, lang: str = "en") -> str:
    """The whole plan in words, before the first approval: what is installed and where, every step with its exact command, what it writes, its size and its undo, the memory numbers, the plan hash and who may approve."""
    steps = data.get("steps") or []
    if data.get("undo_plan"):
        lines = [t(lang, "ud_head").format(machine=fmt_value(data.get("machine")), option=fmt_value(data.get("option_title") or data.get("option_id"), 160)), t(lang, "ud_only")]
        for s in steps:
            lines += ["", t(lang, "ud_step").format(n=s.get("n"), of=data.get("of", len(steps)), title=fmt_value(s.get("title"), 160)) + f' [{t(lang, "pl_needs")}]',
                      f'  {t(lang, "ud_does")}: {fmt_value(s.get("undo"), 2000)}', f'  {t(lang, "ud_removes")}: {", ".join(map(str, s.get("writes") or [])) or t(lang, "pl_nothing")}']
        lines += ["", t(lang, "ud_rule")]
        lines += legend_lines("\n".join(lines), lang)
        if data.get("plan_hash"):
            lines.append(f'{t(lang, "pl_hash")}: {data["plan_hash"]}')
        return "\n".join(lines)
    fit = data.get("fit") or {}
    lines = [data["plan_title"] if data.get("plan_title") else t(lang, "cn_head" if data.get("connect") else "pl_head").format(machine=fmt_value(data.get("machine") or data.get("machine_id")), option=fmt_value(data.get("option_title") or data.get("option_id"), 160))]
    lines += [fmt_value(n, 700) for n in data.get("notes") or []]
    if fit.get("needed_gib") is not None:
        lines.append(t(lang, "pl_fit").format(needed=fit.get("needed_gib"), budget=fit.get("budget_gib"), basis=fmt_value(fit.get("basis"), 120), fit=t(lang, "fits") if fit.get("fits") else t(lang, "nofit")))
    total = data.get("total_download_bytes")
    if isinstance(total, (int, float)) and total > 0:
        lines.append(t(lang, "pl_total").format(size=human_bytes(total)))
    moved = data.get("total_moved_bytes")
    if isinstance(moved, (int, float)) and moved > 0:
        lines.append(t(lang, "pl_total_moved").format(size=human_bytes(moved)))
    for s in steps:
        lines.append("")
        lines.append(t(lang, "pl_step").format(n=s.get("n"), of=len(steps), title=fmt_value(s.get("title"), 160)) + f' [{t(lang, "pl_needs") if s.get("needs_owner_approval") else t(lang, "pl_no_approval")}]')
        lines.append(f'  {t(lang, "pl_cmd")}: {fmt_value(s.get("exact_command"), 4000)}')
        lines.append(f'  {t(lang, "pl_writes")}: {", ".join(map(str, s.get("writes") or [])) or t(lang, "pl_nothing")} · {t(lang, "size")}: {size_text(s.get("bytes"), s.get("size_kind"), lang)}')
        lines.append(f'  {t(lang, "pl_undo")}: {fmt_value(s.get("undo"), 2000)}')
    v = data.get("verification") or {}
    verify = t(lang, "cn_verify").format(what=fmt_value(v["what"])) + "." if data.get("connect") and v.get("what") else f'{t(lang, "pl_verify")}' + (f' ({fmt_value(v.get("endpoint"))})' if v.get("endpoint") else "") + "."
    lines += ["", verify, t(lang, "pl_rule"), t(lang, "undo_note")]
    lines += legend_lines("\n".join(lines), lang)
    if data.get("plan_hash"):
        lines.append(f'{t(lang, "pl_hash")}: {data["plan_hash"]}')
    return "\n".join(lines)


def network_text(data: dict, lang: str = "en") -> str:
    """The map in words, complete enough to stand alone: a chat may show no card at all. Each machine says its role, system, state, what is running and, when an analysis is
    stored, its resources and the AI it has, each value with how it is known. File paths stay out of this text (it can reach a cloud chat)."""
    nodes = data.get("nodes") or []
    coord = (data.get("coordinator") or {}).get("name") or t(lang, "coordinator")
    lines = [f'{t(lang, "network")}: {len(nodes)} · {t(lang, "coordinator")} «{fmt_value(coord)}» ({t(lang, "role_coord")})']
    if not nodes:
        return "\n".join([*lines, t(lang, "no_nodes")])
    paths = False
    for n in nodes:
        system = " ".join(x for x in (fmt_value(n.get("os")) if n.get("os") else "", f'({n["arch"]})' if n.get("arch") else "") if x)
        seen = (f' · {t(lang, "last_seen")}: {(t(lang, "ago") + " ") if t(lang, "ago") else ""}{n["seen_s"]} {t(lang, "secs")}' if isinstance(n.get("seen_s"), int) else f' · {t(lang, "never_seen")}')
        lines.append(f'\n■ {fmt_value(n.get("name"))} — {t(lang, "role_node")} · {system or t(lang, "unknown")} · {t(lang, n.get("status", "offline"))}{seen}' + (f' · {t(lang, "legend_lock")}' if n.get("confidential") else ""))
        act = n.get("activity")
        lines.append(f'  {t(lang, "now")}: ' + (f'{fmt_value(act.get("text"), 160)} ({t(lang, "actor")}: {act.get("actor", "")})' if act else t(lang, "idle")))
        m = n.get("machine")
        if not m:
            lines.append(f'  {t(lang, "no_analysis")}')
            continue
        for g in ("system", "hardware", "ai"):
            rows = []
            for r in (m.get("groups") or {}).get(g) or []:
                if "path" in str(r.get("key", "")):
                    paths = True
                    continue
                rows.append(f'{r.get("label")} = {fmt_value(r.get("value"), 120)} [{t(lang, r.get("status", "unknown")) if r.get("status") else t(lang, "unknown")}]')
            if rows:
                lines.append(f'  {t(lang, g)}: ' + "; ".join(rows))
        for f in (m.get("findings") or [])[:3]:
            lines.append(f'  * {fmt_value(f.get("text"), 240)} [{t(lang, f.get("status", "hypothesis"))}]')
        if n.get("analysis_at"):
            lines.append(f'  ({t(lang, "analysis_from")}: {n["analysis_at"]})')
    if paths:
        lines.append(t(lang, "paths_withheld"))
    return "\n".join(lines)
