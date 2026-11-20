"""Parametric enclosure for the MAS key board (DESIGN-ONLY; MISSION 7 Fase 5, A-1 §8.9: "parametric model with a fit check only").

Input: the board's facts read through pcbnew (`inspect_board.py`: outline, footprints with courtyards). Output: `enclosure/enclosure.json`
(every dimension, for the check and for people) and `enclosure/mas_key_enclosure.scad` (OpenSCAD source for a base, a lid and a button
plunger; not rendered here). `check()` is the fit check. Nothing here was printed or fitted to a real board.

Frame: x across the board, y along it (USB-C mouth at y = 0), z up from the board's bottom face. Millimetres.

Part heights and the OLED module's dimensions are from manufacturers' drawings as remembered, not re-read: UNVERIFIED (listed for the
human review). The OLED module plugs into J2 and overhangs past the board's far end: lying back over the board it would cover the button.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

PARAMS = {
    "wall": 1.6,            # side and end walls
    "floor_t": 1.2,         # base floor
    "lid_t": 1.6,           # lid plate
    "clear_xy": 0.4,        # gap between any part's courtyard and an inner wall
    "clear_z": 0.6,         # gap above the tallest part, below the lid
    "board_t": 1.6,         # PCB thickness (A-3 stackup)
    "floor_gap": 4.0,       # space under the board's bottom face for through-hole leads
    "rim_min": 1.0,         # material left around any opening
    "usb_overmold": [12.35, 6.5],  # USB-C plug overmold envelope (w, h) the opening must admit (UNVERIFIED: USB-C spec maximum)
    "usb_clear": 0.15,
    "usb_recess_max": 2.5,  # outer face to receptacle mouth, with an overmold-sized opening (UNVERIFIED rule of thumb)
    "button_cap_d": 3.5,    # PTS645 actuator diameter (UNVERIFIED)
    "button_travel": 0.25,  # PTS645 travel to actuate (UNVERIFIED)
    "plunger_d": 3.0,
    "plunger_gap": 0.2,     # plunger bottom above the actuator at rest: never pre-pressed
    "plunger_proud": 1.0,   # plunger head above the lid's outer face: the press travel available
    "hole_clear": 0.2,      # lid hole around the plunger
    "window_border": 0.5,   # display window margin around the active area
}

# Height above the board's top face (top-side parts) and protrusion below its bottom face, by reference (UNVERIFIED drawings).
HEIGHT = {"J1": 3.31, "SW1": 4.3, "U1": 1.6, "U2": 1.45, "U3": 1.45, "J3": 3.5, "J2": 8.5, "D1": 0.8}
PASSIVE_HEIGHT = 1.0
BELOW = {"J2": 3.0, "J1": 1.0}
# 0.96" SSD1306 I2C module (UNVERIFIED): PCB w x h, header row along one short edge, active area and its centre from the header row.
OLED = {"w": 27.3, "h": 27.8, "pcb_t": 1.2, "glass_t": 1.6, "header_inset": 1.3, "active": [21.74, 10.86], "active_from_header": 14.0}


def _rel(facts):
    x0, y0, _, _ = facts["outline"]
    return lambda x, y: (x - x0, y - y0)


def parts(facts) -> list[dict]:
    """Every footprint as a box: plan-view courtyard (board-relative) and z range."""
    rel, out, bt = _rel(facts), [], PARAMS["board_t"]
    for f in facts["footprints"]:
        a, b = rel(*f["courtyard"][:2]), rel(*f["courtyard"][2:])
        h = HEIGHT.get(f["ref"], PASSIVE_HEIGHT)
        out.append({"ref": f["ref"], "box": [a[0], a[1], b[0], b[1]], "z": [-BELOW.get(f["ref"], 0.0), bt + h], "at": list(rel(f["x"], f["y"]))})
    return out


def oled_module(facts) -> dict:
    """The module hangs on J2's header row and extends away from the board (past its far end)."""
    j2 = next(p for p in parts(facts) if p["ref"] == "J2")
    cx, hy = (j2["box"][0] + j2["box"][2]) / 2, j2["at"][1]  # the row's centre from its courtyard; the row runs across the board
    y0 = hy - OLED["header_inset"]
    z0 = PARAMS["board_t"] + HEIGHT["J2"]
    box = [cx - OLED["w"] / 2, y0, cx + OLED["w"] / 2, y0 + OLED["h"]]
    acx, acy = cx, y0 + OLED["header_inset"] + OLED["active_from_header"]
    aw, ah = OLED["active"]
    return {"box": box, "z": [z0, z0 + OLED["pcb_t"] + OLED["glass_t"]], "active": [acx - aw / 2, acy - ah / 2, acx + aw / 2, acy + ah / 2]}


def build(facts, params=None) -> dict:
    P = dict(PARAMS, **(params or {}))
    ps, m = parts(facts), oled_module(facts)
    W, H = facts["outline"][2] - facts["outline"][0], facts["outline"][3] - facts["outline"][1]
    boxes = [[0, 0, W, H]] + [p["box"] for p in ps] + [m["box"]]
    c = P["clear_xy"]
    cav = [min(b[0] for b in boxes) - c, min(b[1] for b in boxes) - c, max(b[2] for b in boxes) + c, max(b[3] for b in boxes) + c]
    top = max([p["z"][1] for p in ps] + [m["z"][1]]) + P["clear_z"]
    bottom = -P["floor_gap"]
    w = P["wall"]
    outer = [cav[0] - w, cav[1] - w, cav[2] + w, cav[3] + w]
    z_outer = [bottom - P["floor_t"], top + P["lid_t"]]
    j1 = next(p for p in ps if p["ref"] == "J1")
    ow, oh = (d + 2 * P["usb_clear"] for d in P["usb_overmold"])
    usb_cx, usb_cz = (j1["box"][0] + j1["box"][2]) / 2, PARAMS["board_t"] + HEIGHT["J1"] / 2
    sw = next(p for p in ps if p["ref"] == "SW1")
    hole_d = P["plunger_d"] + 2 * P["hole_clear"]
    act_top = sw["z"][1]
    plunger = {"at": sw["at"], "d": P["plunger_d"], "z": [act_top + P["plunger_gap"], z_outer[1] + P["plunger_proud"]]}
    win = [m["active"][0] - P["window_border"], m["active"][1] - P["window_border"], m["active"][2] + P["window_border"],
           m["active"][3] + P["window_border"]]
    return {"params": P, "parts": ps, "oled": m, "cavity": cav, "cavity_z": [bottom, top], "outer": outer, "outer_z": z_outer,
            "usb_opening": {"x": [usb_cx - ow / 2, usb_cx + ow / 2], "z": [usb_cz - oh / 2, usb_cz + oh / 2], "face_y": outer[1],
                            "mouth_y": j1["box"][1]},
            "button_hole": {"at": sw["at"], "d": hole_d}, "plunger": plunger, "window": win,
            "units": "mm", "label": "DESIGN-ONLY", "unverified": ["HEIGHT", "BELOW", "OLED", "usb_overmold", "usb_recess_max",
                                                                 "button_cap_d", "button_travel"]}


def _overlap(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _circle_box(c, r, b) -> bool:
    dx = max(b[0] - c[0], 0, c[0] - b[2])
    dy = max(b[1] - c[1], 0, c[1] - b[3])
    return math.hypot(dx, dy) < r


def check(g, facts) -> list[str]:
    """Every way the enclosure could fail to fit the board, as text; empty if it fits."""
    v, P = [], g["params"]
    W, H = facts["outline"][2] - facts["outline"][0], facts["outline"][3] - facts["outline"][1]
    cav, cz = g["cavity"], g["cavity_z"]
    inside = lambda b, m=0.0: cav[0] + m <= b[0] and cav[1] + m <= b[1] and b[2] <= cav[2] - m and b[3] <= cav[3] - m
    if not inside([0, 0, W, H], P["clear_xy"] - 1e-6):
        v.append("the board does not fit the cavity with its clearance")
    for p in g["parts"] + [dict(g["oled"], ref="OLED module")]:
        if not inside(p["box"], P["clear_xy"] - 1e-6):
            v.append(f"{p['ref']} courtyard is closer than {P['clear_xy']} mm to a wall")
        if p["z"][1] > cz[1] - P["clear_z"] + 1e-6:
            v.append(f"{p['ref']} reaches the lid")
        if p["z"][0] < cz[0] - 1e-6:
            v.append(f"{p['ref']} reaches the floor")
    # USB-C: the opening admits a plug overmold, sits on the receptacle's axis, keeps a rim, and the mouth is not recessed too far
    u, o, oz = g["usb_opening"], g["outer"], g["outer_z"]
    j1 = next(p for p in g["parts"] if p["ref"] == "J1")
    ow, oh = (d + 2 * P["usb_clear"] for d in P["usb_overmold"])
    if u["x"][1] - u["x"][0] < ow - 1e-6 or u["z"][1] - u["z"][0] < oh - 1e-6:
        v.append("the USB-C opening is smaller than a plug overmold")
    if abs((u["x"][0] + u["x"][1]) / 2 - (j1["box"][0] + j1["box"][2]) / 2) > 0.1:
        v.append("the USB-C opening is off the receptacle's axis")
    if abs((u["z"][0] + u["z"][1]) / 2 - (P["board_t"] + HEIGHT["J1"] / 2)) > 0.1:
        v.append("the USB-C opening is off the receptacle's height")
    if min(u["x"][0] - o[0], o[2] - u["x"][1], u["z"][0] - oz[0], oz[1] - u["z"][1]) < P["rim_min"] - 1e-6:
        v.append("the USB-C opening leaves less than the minimum rim")
    if j1["box"][1] - u["face_y"] > P["usb_recess_max"] + 1e-6:
        v.append(f"the USB-C mouth is recessed {j1['box'][1] - u['face_y']:.2f} mm behind the outer face")
    # the button: a plunger through the lid, on the actuator, with nothing in its column, never pre-pressed, with enough travel
    pl, bh = g["plunger"], g["button_hole"]
    sw = next(p for p in g["parts"] if p["ref"] == "SW1")
    if math.dist(pl["at"], sw["at"]) > 0.2 or math.dist(bh["at"], sw["at"]) > 0.2:
        v.append("the plunger or the lid hole is off the button")
    if pl["d"] > P["button_cap_d"] + 1e-6 or bh["d"] < pl["d"] + 2 * P["hole_clear"] - 1e-6:
        v.append("the plunger is wider than the actuator or the lid hole too tight")
    if pl["z"][0] - sw["z"][1] <= 0:
        v.append("the plunger presses the button at rest")
    if pl["z"][1] - oz[1] < (pl["z"][0] - sw["z"][1]) + P["button_travel"] + 0.3 - 1e-6:
        v.append("pressing the plunger flush does not travel far enough to actuate the button")
    r = pl["d"] / 2 + P["hole_clear"]
    for p in g["parts"] + [dict(g["oled"], ref="OLED module")]:
        if p.get("ref") != "SW1" and p["z"][1] > sw["z"][1] and _circle_box(pl["at"], r, p["box"]):
            v.append(f"{p['ref']} is in the plunger's column")
    # the display: a window over the active area, inside the lid with a rim, clear of the button hole
    win = g["window"]
    act = g["oled"]["active"]
    if not (win[0] <= act[0] and win[1] <= act[1] and act[2] <= win[2] and act[3] <= win[3]):
        v.append("the display window does not show the whole active area")
    if min(win[0] - o[0], win[1] - o[1], o[2] - win[2], o[3] - win[3]) < P["rim_min"] - 1e-6:
        v.append("the display window leaves less than the minimum rim")
    if _circle_box(bh["at"], bh["d"] / 2 + P["rim_min"], win):
        v.append("the display window runs into the button hole")
    return v


def scad(g) -> str:
    """OpenSCAD source (base, lid, plunger) generated from the same numbers. Not rendered here."""
    f = lambda xs: "[" + ", ".join(f"{x:.3f}" for x in xs) + "]"
    o, oz, c, cz, u, w = g["outer"], g["outer_z"], g["cavity"], g["cavity_z"], g["usb_opening"], g["window"]
    pl, bh = g["plunger"], g["button_hole"]
    split = cz[1]
    return f"""// MAS key enclosure, generated by device/hardware/enclosure.py. DESIGN-ONLY: never printed or fitted. NO FABRICAR sin revisión humana.
// Frame: x across, y along (USB-C at y = 0), z up from the board's bottom face. mm.
$fn = 48;
module shell() {{
  difference() {{
    translate({f([o[0], o[1], oz[0]])}) cube({f([o[2] - o[0], o[3] - o[1], oz[1] - oz[0]])});
    translate({f([c[0], c[1], cz[0]])}) cube({f([c[2] - c[0], c[3] - c[1], cz[1] - cz[0]])});
    translate({f([u["x"][0], o[1] - 1, u["z"][0]])}) cube({f([u["x"][1] - u["x"][0], c[1] - o[1] + 2, u["z"][1] - u["z"][0]])});
    translate({f([bh["at"][0], bh["at"][1], cz[1] - 1])}) cylinder(d = {bh["d"]:.3f}, h = {oz[1] - cz[1] + 2:.3f});
    translate({f([w[0], w[1], cz[1] - 1])}) cube({f([w[2] - w[0], w[3] - w[1], oz[1] - cz[1] + 2])});
  }}
}}
module base() {{ intersection() {{ shell(); translate({f([o[0] - 1, o[1] - 1, oz[0] - 1])}) cube({f([o[2] - o[0] + 2, o[3] - o[1] + 2, split - oz[0] + 1])}); }} }}
module lid() {{ intersection() {{ shell(); translate({f([o[0] - 1, o[1] - 1, split])}) cube({f([o[2] - o[0] + 2, o[3] - o[1] + 2, oz[1] - split + 1])}); }} }}
module plunger() {{ translate({f([pl["at"][0], pl["at"][1], pl["z"][0]])}) cylinder(d = {pl["d"]:.3f}, h = {pl["z"][1] - pl["z"][0]:.3f}); }}
base();
translate([{o[2] - o[0] + 5:.3f}, 0, 0]) lid();
translate([{2 * (o[2] - o[0]) + 10:.3f}, 0, 0]) plunger();
"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build the enclosure from the board's facts and check the fit (DESIGN-ONLY).")
    ap.add_argument("--board", required=True, help="JSON from inspect_board.py")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    facts = json.loads(Path(a.board).read_text(encoding="utf-8"))
    g = build(facts)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "enclosure.json").write_bytes((json.dumps(g, indent=1, sort_keys=True) + "\n").encode("utf-8"))
    (out / "mas_key_enclosure.scad").write_bytes(scad(g).encode("utf-8"))
    problems = check(g, facts)
    for p in problems:
        print("ERROR enclosure:", p, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
