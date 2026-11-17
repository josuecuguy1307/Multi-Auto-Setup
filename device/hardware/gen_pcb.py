"""Build the key board (KiCad 10 .kicad_pcb) from the netlist that kicad-cli exported from the schematic. DESIGN-ONLY.

Runs under KiCad's bundled Python (pcbnew, numpy). Parity with the schematic holds by construction: every footprint, its path (the
symbol's UUID), value and every pad's net come from the netlist; only placement comes from design.py.

Routing: a deterministic two-layer grid router (A* on a 0.05 mm grid). Each net is routed as a tree (Prim order); a cell is usable by a
net only if no copper of another net, inflated by half the track width plus the clearance plus a grid margin, covers it; vias need the
larger via clearance on both layers and the hole-to-hole distance. Bottom-layer moves cost more, so B.Cu stays mostly a solid GND pour.
GND is connected through pours on both layers plus a stitching via beside each GND pad. Then zones are filled and the file is saved
with deterministic UUIDs. KiCad's DRC is the judge, not this script.

    <kicad>/bin/python.exe device/hardware/gen_pcb.py --netlist kicad/mas_key.net --out kicad [--kicad-share DIR]
"""

from __future__ import annotations

import argparse
import heapq
import json
import math
import re
import sys
import uuid
from pathlib import Path

import numpy as np
import pcbnew

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import design  # noqa: E402
from sexpr import parse  # noqa: E402

PROJECT = "mas_key"
OX, OY = 100.0, 60.0                  # board origin on the page (mm)
G = 0.05                              # grid pitch (mm)
TRACK_W, CLR, MARGIN = 0.2, 0.15, 0.05
VIA_D, VIA_DRILL, HOLE_HOLE, EDGE_CLR, HOLE_CLR = 0.6, 0.3, 0.5, 0.3, 0.25
B_COST, VIA_COST = 4.0, 30.0
NS = uuid.UUID("0c0f3d2a-91a4-4c1b-8f0e-9a5d1f2e7b33")
FREE, MULTI = -1, -2

# Custom DRC rules: each scoped as tightly as possible and listed in HUMAN_REVIEW_PCB.md (tested). No exclusions anywhere.
CUSTOM_RULES = """(version 1)
(rule "J1 vendor land pattern"
\t(condition "A.Parent == 'J1' && B.Parent == 'J1'")
\t(constraint hole_clearance (min 0.19mm)))
"""


def mm(v: float) -> int:
    return pcbnew.FromMM(v)


def tomm(v: int) -> float:
    return pcbnew.ToMM(v)


# ---- netlist ---------------------------------------------------------------------------------------

def read_netlist(path: Path):
    doc = parse(path.read_text(encoding="utf-8"))
    sec = lambda x, k: [e for e in x if isinstance(e, list) and e and e[0] == k]
    comps = {}
    for c in sec(sec(doc, "components")[0], "comp"):
        f = lambda k: sec(c, k)[0][1] if sec(c, k) else ""
        fields = {}
        for fl in sec(c, "fields"):
            for fd in sec(fl, "field"):
                name = sec(fd, "name")[0][1]
                vals = [v for v in fd[1:] if not isinstance(v, list)]
                fields[name] = vals[0] if vals else ""
        comps[f("ref")] = {"value": f("value"), "footprint": f("footprint"), "uuid": f("tstamps"), "fields": fields}
    pin_net = {}
    for n in sec(sec(doc, "nets")[0], "net"):
        name = sec(n, "name")[0][1]
        for node in sec(n, "node"):
            pin_net[(sec(node, "ref")[0][1], sec(node, "pin")[0][1])] = name
    return comps, pin_net


# ---- the occupancy grid --------------------------------------------------------------------------

class Grid:
    def __init__(self, w: float, h: float):
        self.nx, self.ny = int(round(w / G)) + 1, int(round(h / G)) + 1
        self.trk = [np.full((self.ny, self.nx), FREE, np.int32) for _ in range(2)]  # who may put a track centre here
        self.via = [np.full((self.ny, self.nx), FREE, np.int32) for _ in range(2)]  # who may put a via centre here
        self.hole = np.zeros((self.ny, self.nx), bool)                               # too close to any hole for a new via
        xs, ys = np.arange(self.nx) * G, np.arange(self.ny) * G
        self.X, self.Y = np.meshgrid(xs, ys)
        edge = np.minimum(np.minimum(self.X, w - self.X), np.minimum(self.Y, h - self.Y))
        for L in range(2):
            self.trk[L][edge < TRACK_W / 2 + EDGE_CLR + MARGIN] = MULTI
            self.via[L][edge < VIA_D / 2 + EDGE_CLR + MARGIN] = MULTI

    def _window(self, x0, y0, x1, y1, r):
        i0, i1 = max(0, int((y0 - r) / G) - 1), min(self.ny, int((y1 + r) / G) + 2)
        j0, j1 = max(0, int((x0 - r) / G) - 1), min(self.nx, int((x1 + r) / G) + 2)
        return slice(i0, i1), slice(j0, j1)

    @staticmethod
    def _mark(arr, sl, mask, net):
        sub = arr[sl]
        if net is None:
            sub[mask] = MULTI
            return
        cur = sub[mask]
        sub[mask] = np.where(cur == FREE, net, np.where(cur == net, net, MULTI))

    def rect(self, layers, x0, y0, x1, y1, net):
        """A copper rectangle (axis-aligned bounding box) of `net` on `layers`."""
        for arrs, r in ((self.trk, TRACK_W / 2 + CLR + MARGIN), (self.via, VIA_D / 2 + CLR + MARGIN)):
            sl = self._window(x0, y0, x1, y1, r)
            X, Y = self.X[sl], self.Y[sl]
            dx = np.maximum(np.maximum(x0 - X, 0), X - x1)
            dy = np.maximum(np.maximum(y0 - Y, 0), Y - y1)
            m = np.hypot(dx, dy) < r
            for L in layers:
                self._mark(arrs[L], sl, m, net)

    def seg(self, L, x0, y0, x1, y1, width, net):
        for arrs, r in ((self.trk, width / 2 + TRACK_W / 2 + CLR + MARGIN), (self.via, width / 2 + VIA_D / 2 + CLR + MARGIN)):
            sl = self._window(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1), r)
            X, Y = self.X[sl], self.Y[sl]
            vx, vy = x1 - x0, y1 - y0
            ll = vx * vx + vy * vy
            t = np.clip(((X - x0) * vx + (Y - y0) * vy) / ll, 0, 1) if ll > 0 else np.zeros_like(X)
            m = np.hypot(X - (x0 + t * vx), Y - (y0 + t * vy)) < r
            self._mark(arrs[L], sl, m, net)

    def circle_hole(self, x, y, drill):
        r = drill / 2 + HOLE_HOLE + VIA_DRILL / 2 + MARGIN
        sl = self._window(x, y, x, y, r)
        self.hole[sl] |= np.hypot(self.X[sl] - x, self.Y[sl] - y) < r

    def via_at(self, x, y, net):
        for L in range(2):
            self.seg(L, x, y, x, y, VIA_D, net)
        self.circle_hole(x, y, VIA_DRILL)

    def cell(self, x, y):
        return int(round(y / G)), int(round(x / G))


# ---- the router ----------------------------------------------------------------------------------

DIRS = [(0, 1, 1.0), (1, 0, 1.0), (0, -1, 1.0), (-1, 0, 1.0), (1, 1, math.sqrt(2)), (1, -1, math.sqrt(2)), (-1, 1, math.sqrt(2)),
        (-1, -1, math.sqrt(2))]


def astar(grid: Grid, net: int, starts, targets, box, allow_via=True, b_cost: float = B_COST):
    """starts: [(L,i,j)], targets: bool arrays per layer. Returns the cell path or None."""
    (i0, i1), (j0, j1) = box
    tpts = np.argwhere(targets[0] | targets[1])
    if len(tpts) == 0:
        return None
    ti0, tj0 = (int(v) for v in tpts.min(axis=0))
    ti1, tj1 = (int(v) for v in tpts.max(axis=0))

    def h(i, j):  # octile distance to the targets' bounding box: cheap and admissible
        di = max(ti0 - i, 0, i - ti1)
        dj = max(tj0 - j, 0, j - tj1)
        return max(di, dj) + (math.sqrt(2) - 1) * min(di, dj)

    trk, via = grid.trk, grid.via
    ok = lambda L, i, j: trk[L][i, j] in (FREE, net)
    openh, came, gbest = [], {}, {}
    for s in starts:
        gbest[s] = 0.0
        heapq.heappush(openh, (h(s[1], s[2]), 0.0, s))
        came[s] = None
    hcache = {}
    expanded = 0
    while openh:
        f, gc, cur = heapq.heappop(openh)
        if gc > gbest.get(cur, 1e18):
            continue
        L, i, j = cur
        if targets[L][i, j]:
            path = []
            while cur is not None:
                path.append(cur)
                cur = came[cur]
            return path[::-1]
        expanded += 1
        if expanded > 600000:
            return None
        nbrs = []
        for di, dj, c in DIRS:
            ni, nj = i + di, j + dj
            if i0 <= ni < i1 and j0 <= nj < j1 and ok(L, ni, nj):
                if di and dj and not (ok(L, i + di, j) or ok(L, i, j + dj)):
                    continue
                nbrs.append(((L, ni, nj), c * (b_cost if L == 1 else 1.0)))
        if allow_via and via[0][i, j] in (FREE, net) and via[1][i, j] in (FREE, net) and not grid.hole[i, j]:
            nbrs.append(((1 - L, i, j), VIA_COST))
        for nxt, c in nbrs:
            ng = gc + c
            if ng < gbest.get(nxt, 1e18):
                gbest[nxt] = ng
                came[nxt] = cur
                key = (nxt[1], nxt[2])
                hv = hcache.get(key)
                if hv is None:
                    hv = hcache[key] = h(*key)
                heapq.heappush(openh, (ng + hv * 0.999, ng, nxt))
    return None


def path_to_items(path):
    """Cell path -> (segments [(L, (i,j), (i,j))], vias [(i,j)]) with collinear moves merged."""
    segs, vias = [], []
    run_start, prev, dirn = path[0], path[0], None
    for cur in path[1:]:
        if cur[0] != prev[0]:
            if prev[1:] != run_start[1:]:
                segs.append((prev[0], run_start[1:], prev[1:]))
            vias.append(prev[1:])
            run_start, prev, dirn = cur, cur, None
            continue
        d = (cur[1] - prev[1], cur[2] - prev[2])
        if dirn is not None and d != dirn:
            segs.append((prev[0], run_start[1:], prev[1:]))
            run_start = prev
        dirn = d
        prev = cur
    if prev[1:] != run_start[1:]:
        segs.append((prev[0], run_start[1:], prev[1:]))
    return segs, vias


# ---- board construction -------------------------------------------------------------------------------

def project_json() -> str:
    rules = {"min_clearance": CLR, "min_track_width": TRACK_W, "min_via_diameter": VIA_D, "min_via_annular_width": 0.1,
             "min_through_hole_diameter": VIA_DRILL, "min_hole_to_hole": HOLE_HOLE, "min_hole_clearance": HOLE_CLR,
             "min_copper_edge_clearance": EDGE_CLR, "min_silk_clearance": 0.0, "min_text_height": 0.8, "min_text_thickness": 0.08,
             "min_microvia_diameter": 0.2, "min_microvia_drill": 0.1, "min_resolved_spokes": 2, "min_groove_width": 0.0,
             "solder_mask_to_copper_clearance": 0.0, "allow_blind_buried_vias": False, "allow_microvias": False,
             "use_height_for_length_calcs": True, "max_error": 0.005}
    pro = {
        "board": {"design_settings": {"rules": rules, "track_widths": [0.0, TRACK_W], "via_dimensions": [{"diameter": 0.0, "drill": 0.0},
                                                                                                        {"diameter": VIA_D, "drill": VIA_DRILL}]},
                  "drc_exclusions": []},
        "erc": {"erc_exclusions": []},
        "meta": {"filename": f"{PROJECT}.kicad_pro", "version": 3},
        "net_settings": {"classes": [{"name": "Default", "clearance": CLR, "track_width": TRACK_W, "via_diameter": VIA_D,
                                      "via_drill": VIA_DRILL, "microvia_diameter": 0.3, "microvia_drill": 0.1, "diff_pair_width": TRACK_W,
                                      "diff_pair_gap": 0.25, "diff_pair_via_gap": 0.25, "wire_width": 6, "bus_width": 12, "line_style": 0,
                                      "priority": 2147483647}],
                         "meta": {"version": 4}},
    }
    return json.dumps(pro, indent=2, sort_keys=True) + "\n"


def add_line(board, layer, x0, y0, x1, y1, w=0.1):
    s = pcbnew.PCB_SHAPE(board)
    s.SetShape(pcbnew.SHAPE_T_SEGMENT)
    s.SetStart(pcbnew.VECTOR2I(mm(OX + x0), mm(OY + y0)))
    s.SetEnd(pcbnew.VECTOR2I(mm(OX + x1), mm(OY + y1)))
    s.SetLayer(layer)
    s.SetWidth(mm(w))
    board.Add(s)


def add_text(board, layer, text, x, y, angle, size=0.8, thick=0.12, mirror=False):
    t = pcbnew.PCB_TEXT(board)
    t.SetText(text)
    t.SetLayer(layer)
    t.SetPosition(pcbnew.VECTOR2I(mm(OX + x), mm(OY + y)))
    t.SetTextAngleDegrees(angle)
    t.SetTextSize(pcbnew.VECTOR2I(mm(size), mm(size)))
    t.SetTextThickness(mm(thick))
    t.SetMirrored(mirror)
    board.Add(t)


def add_zone(board, layer, net, inset=0.0):
    z = pcbnew.ZONE(board)
    z.SetLayer(layer)
    z.SetNet(net)
    o = z.Outline()
    o.NewOutline()
    for x, y in ((0, 0), (design.BOARD_W, 0), (design.BOARD_W, design.BOARD_H), (0, design.BOARD_H)):
        o.Append(mm(OX + x), mm(OY + y))
    z.SetPadConnection(pcbnew.ZONE_CONNECTION_FULL)
    z.SetMinThickness(mm(0.2))
    z.SetLocalClearance(mm(0.2))
    z.SetIslandRemovalMode(pcbnew.ISLAND_REMOVAL_MODE_ALWAYS)
    z.SetAssignedPriority(0)
    board.Add(z)
    return z


def pad_box(pad):
    bb = pad.GetBoundingBox()
    return tomm(bb.GetX()) - OX, tomm(bb.GetY()) - OY, tomm(bb.GetRight()) - OX, tomm(bb.GetBottom()) - OY


def pad_layers(pad) -> list[int]:
    out = []
    if pad.IsOnLayer(pcbnew.F_Cu):
        out.append(0)
    if pad.IsOnLayer(pcbnew.B_Cu):
        out.append(1)
    return out


def build(netlist: Path, share: Path, route: bool = True):
    pcbnew.KIID.SeedGenerator(20261117)  # KiCad saves items ordered by UUID: a seeded generator makes the whole file deterministic
    comps, pin_net = read_netlist(netlist)
    by_ref = {p.ref: p for p in design.PARTS}
    if set(comps) != set(by_ref):
        raise SystemExit(f"ERROR netlist and design.py disagree on parts: {sorted(set(comps) ^ set(by_ref))}")
    board = pcbnew.BOARD()
    board.SetCopperLayerCount(2)
    tb = board.GetTitleBlock()
    tb.SetTitle("MAS key board")
    tb.SetRevision("v0")
    tb.SetComment(0, design.STAMP)
    tb.SetComment(1, "DESIGN-ONLY: generated from device/hardware/design.py")
    nets = {}
    for name in sorted(set(pin_net.values())):
        ni = pcbnew.NETINFO_ITEM(board, name)
        board.Add(ni)
        nets[name] = ni
    codes = {name: i + 1 for i, name in enumerate(sorted(nets))}
    fps = []
    for ref in sorted(comps, key=lambda r: (re.sub(r"\d", "", r), int(re.sub(r"\D", "", r) or 0))):
        c, p = comps[ref], by_ref[ref]
        lib, name = c["footprint"].split(":")
        fp = pcbnew.FootprintLoad(str(share / "footprints" / f"{lib}.pretty"), name)
        if fp is None:
            raise SystemExit(f"ERROR footprint {c['footprint']} not found")
        fp.SetFPID(pcbnew.LIB_ID(lib, name))
        fp.SetReference(ref)
        fp.SetValue(c["value"])
        fp.SetPath(pcbnew.KIID_PATH("/" + c["uuid"]))
        fp.Reference().SetLayer(pcbnew.F_Fab)
        for fname, fval in sorted(c["fields"].items()):
            if fname in ("Reference", "Value", "Footprint"):
                continue
            fp.SetField(fname, fval)
            fld = fp.GetField(fname)
            fld.SetLayer(pcbnew.F_Fab)
            fld.SetVisible(False)
        x, y, rot = p.at
        fp.SetPosition(pcbnew.VECTOR2I(mm(OX + x), mm(OY + y)))
        fp.SetOrientationDegrees(rot)
        board.Add(fp)
        for pad in fp.Pads():
            num = pad.GetNumber()
            if num and (ref, num) in pin_net:
                pad.SetNet(nets[pin_net[(ref, num)]])
            elif num:
                raise SystemExit(f"ERROR pad {ref}.{num} has no node in the netlist")
        fps.append(fp)
    W, H = design.BOARD_W, design.BOARD_H
    for x0, y0, x1, y1 in ((0, 0, W, 0), (W, 0, W, H), (W, H, 0, H), (0, H, 0, 0)):
        add_line(board, pcbnew.Edge_Cuts, x0, y0, x1, y1, 0.1)
    add_text(board, pcbnew.F_SilkS, design.STAMP, design.SILK_TEXT_X, H / 2, 90)
    add_text(board, pcbnew.B_SilkS, design.STAMP, W - design.SILK_TEXT_X, H / 2, 90, mirror=True)

    report = {"routed": [], "failed": []}
    if route:
        report = route_board(board, fps, nets, codes)
    gnd = nets["GND"]
    add_zone(board, pcbnew.F_Cu, gnd)
    add_zone(board, pcbnew.B_Cu, gnd)
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    return board, report


MAX_PASSES = 8
USB_NETS = ("USB_DP", "USB_DM")
BOTTOM_FIRST = ("SWDIO", "SWCLK")  # routed before USB, preferring B.Cu, because they must cross it


def build_grid(fps, codes):
    W, H = design.BOARD_W, design.BOARD_H
    grid = Grid(W, H)
    pads_by_net: dict[str, list] = {}
    for fp in fps:
        for pad in fp.Pads():
            net = pad.GetNetname()
            layers = pad_layers(pad)
            x0, y0, x1, y1 = pad_box(pad)
            real = bool(net) and not net.startswith("unconnected-")
            if layers:
                grid.rect(layers, x0, y0, x1, y1, codes[net] if real else None)
            if pad.GetDrillSize().x > 0:
                d = tomm(max(pad.GetDrillSize().x, pad.GetDrillSize().y))
                cx, cy = tomm(pad.GetPosition().x) - OX, tomm(pad.GetPosition().y) - OY
                grid.circle_hole(cx, cy, d)
                if not layers:  # NPTH: track edge >= HOLE_CLR from the hole edge (rect + track inflation covers the rest)
                    r = d / 2 + HOLE_CLR - CLR
                    grid.rect([0, 1], cx - r, cy - r, cx + r, cy + r, None)
            if real and layers:
                pads_by_net.setdefault(net, []).append(pad)
    for xs in (design.SILK_TEXT_X, W - design.SILK_TEXT_X):  # no vias under the stamp text
        sl = grid._window(xs - 0.6, 2, xs + 0.6, H - 2, 0)
        grid.hole[sl] = True
    return grid, pads_by_net


def mark(grid, code, path):
    segs, vias = path_to_items(path)
    for L, a, b in segs:
        grid.seg(L, a[1] * G, a[0] * G, b[1] * G, b[0] * G, TRACK_W, code)
    for i, j in vias:
        grid.via_at(j * G, i * G, code)


def route_board(board, fps, nets, codes):
    grid, pads_by_net = build_grid(fps, codes)
    usb = [n for n in USB_NETS if n in pads_by_net]
    under_usb = [n for n in BOTTOM_FIRST if n in pads_by_net]
    signal = [n for n in pads_by_net if n != "GND" and n not in usb and n not in under_usb and len(pads_by_net[n]) > 1]
    order = sorted(signal, key=lambda n: (n in ("+3V3", "VBUS"), hpwl(pads_by_net[n]), n))
    best = None
    for attempt in range(MAX_PASSES):
        grid, pads_by_net = build_grid(fps, codes)
        plan, failed = [], []
        gnd = pads_by_net.get("GND", [])
        mcu = [p for p in gnd if p.GetParentFootprint().GetReference() == "U1"]
        ux, uy = (v for v in next(p for p in design.PARTS if p.ref == "U1").at[:2])
        under = (ux - 2.6, uy - 2.6, ux + 2.6, uy + 2.6)  # inside the LQFP pad ring: vias here block no escape route
        failed += stitch_gnd(grid, codes["GND"], mcu, plan, region=under)  # the MCU's ground pins first, with their vias under the chip
        # then USB D+/D-, top layer only, no vias (A-3). Other nets may cross under them on B.Cu: with the USB pins on the MCU's right
        # side every net beyond them must cross somewhere (declared deviation, bounded by test_pcb, listed in HUMAN_REVIEW_PCB.md)
        for name in under_usb:  # the debug lines must cross the USB pair: they take the bottom layer before USB takes the top
            if not route_net(grid, codes[name], pads_by_net[name], plan, name, b_cost=0.5):
                failed.append(name)
        for name in usb:
            if not route_net(grid, codes[name], pads_by_net[name], plan, name, top_only=True):
                failed.append(name)
        for name in order:
            if not route_net(grid, codes[name], pads_by_net[name], plan, name):
                failed.append(name)
        failed += stitch_gnd(grid, codes["GND"], [p for p in gnd if p not in mcu], plan)
        if best is None or len(failed) < len(best[1]):
            best = (plan, failed, attempt)
        if not failed:
            break
        bad = [n for n in failed if n in order]
        order = bad + [n for n in order if n not in bad] if bad else order[::-1]
        if any(n in usb for n in failed):
            usb = usb[::-1]
    plan, failed, attempt = best
    for kind, name, *rest in plan:
        if kind == "path":
            emit_path(board, nets[name], rest[0])
        else:
            emit_via(board, nets[name], *rest)
    return {"routed": sorted(set(n for _, n, *_ in plan)), "failed": failed, "passes": attempt + 1}


def hpwl(pads):
    xs = [p.GetPosition().x for p in pads]
    ys = [p.GetPosition().y for p in pads]
    return (max(xs) - min(xs)) + (max(ys) - min(ys))


def pad_cell(grid, pad):
    x, y = tomm(pad.GetPosition().x) - OX, tomm(pad.GetPosition().y) - OY
    return grid.cell(x, y)


def emit_path(board, net, path):
    segs, vias = path_to_items(path)
    for L, a, b in segs:
        t = pcbnew.PCB_TRACK(board)
        t.SetStart(pcbnew.VECTOR2I(mm(OX + a[1] * G), mm(OY + a[0] * G)))
        t.SetEnd(pcbnew.VECTOR2I(mm(OX + b[1] * G), mm(OY + b[0] * G)))
        t.SetWidth(mm(TRACK_W))
        t.SetLayer(pcbnew.F_Cu if L == 0 else pcbnew.B_Cu)
        t.SetNet(net)
        board.Add(t)
    for i, j in vias:
        emit_via(board, net, j * G, i * G)


def emit_via(board, net, x, y):
    v = pcbnew.PCB_VIA(board)
    v.SetPosition(pcbnew.VECTOR2I(mm(OX + x), mm(OY + y)))
    v.SetViaType(pcbnew.VIATYPE_THROUGH)
    v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
    v.SetDrill(mm(VIA_DRILL))
    v.SetWidth(mm(VIA_D))
    v.SetNet(net)
    board.Add(v)


def box_around(grid, pts, margin):
    m = int(margin / G)
    ys, xs = pts[:, 0], pts[:, 1]
    return ((max(0, int(ys.min()) - m), min(grid.ny, int(ys.max()) + m + 1)), (max(0, int(xs.min()) - m), min(grid.nx, int(xs.max()) + m + 1)))


def route_net(grid, code, pads, plan, name, top_only: bool = False, b_cost: float = B_COST) -> bool:
    pads = sorted(pads, key=lambda p: (p.GetParentFootprint().GetReference(), p.GetNumber(), p.GetPosition().x, p.GetPosition().y))
    tree = [np.zeros((grid.ny, grid.nx), bool) for _ in range(2)]
    i, j = pad_cell(grid, pads[0])
    for L in pad_layers(pads[0]):
        tree[L][i, j] = True
    todo, ok = pads[1:], True
    while todo:
        pts = np.argwhere(tree[0] | tree[1])
        todo.sort(key=lambda p: (float(np.min(np.abs(pts - pad_cell(grid, p)).sum(axis=1))), p.GetParentFootprint().GetReference(), p.GetNumber()))
        p = todo.pop(0)
        i, j = pad_cell(grid, p)
        if any(tree[L][i, j] for L in pad_layers(p)):
            continue
        starts = [(L, i, j) for L in pad_layers(p) if not (top_only and L == 1)]
        allp = np.vstack([pts, [[i, j]]])
        path = None
        for margin in (2.0, 6.0, 100.0):
            path = astar(grid, code, starts, tree, box_around(grid, allp, margin), allow_via=not top_only, b_cost=b_cost)
            if path:
                break
        if not path:
            ok = False
            continue
        plan.append(("path", name, path))
        mark(grid, code, path)
        for L, a, b in path:
            tree[L][a, b] = True
        for L in pad_layers(p):
            tree[L][i, j] = True
    return ok


def stitch_gnd(grid, code, pads, plan, region=None) -> list[str]:
    """B.Cu is the GND plane. Each SMD GND pad gets a short track to a via of its own; a pad with no room for a via joins GND copper that
    already reaches a via (never another unstitched pad, so no isolated GND islands). THT GND pads reach B.Cu directly."""
    grounded = np.zeros((grid.ny, grid.nx), bool)  # F.Cu cells of GND copper known to reach the plane
    smd = []
    for p in sorted(pads, key=lambda p: (p.GetParentFootprint().GetReference(), p.GetNumber(), p.GetPosition().x)):
        i, j = pad_cell(grid, p)
        if 1 in pad_layers(p):
            grounded[i, j] = True
        else:
            smd.append(p)
    failed = []
    for second in (False, True):
        retry = []
        for p in (smd if not second else failed_pads):
            i, j = pad_cell(grid, p)
            r = int((2.5 if region is None else 4.5) / G) if not second else int(8.0 / G)
            box = ((max(0, i - r), min(grid.ny, i + r + 1)), (max(0, j - r), min(grid.nx, j + r + 1)))
            target = [np.zeros((grid.ny, grid.nx), bool), np.zeros((grid.ny, grid.nx), bool)]
            sl = (slice(*box[0]), slice(*box[1]))
            if not second:
                ok_via = np.isin(grid.via[0][sl], (FREE, code)) & np.isin(grid.via[1][sl], (FREE, code)) & ~grid.hole[sl]
                if region is not None:
                    x0, y0, x1, y1 = region
                    ok_via &= (grid.X[sl] >= x0) & (grid.X[sl] <= x1) & (grid.Y[sl] >= y0) & (grid.Y[sl] <= y1)
                target[0][sl] = ok_via
            else:
                target[0][sl] = grounded[sl]
            path = astar(grid, code, [(0, i, j)], target, box, allow_via=False)
            if not path:
                retry.append(p)
                continue
            plan.append(("path", "GND", path))
            mark(grid, code, path)
            if not second:
                L, a, b = path[-1]
                plan.append(("via", "GND", b * G, a * G))
                grid.via_at(b * G, a * G, code)
            for L, a, b in path:
                grounded[a, b] = True
        failed_pads = retry
    return [f"GND:{p.GetParentFootprint().GetReference()}.{p.GetNumber()}" for p in failed_pads]


def deterministic_uuids(text: str) -> str:
    seen: dict[str, str] = {}

    def sub(m):
        old = m.group(1)
        if old not in seen:
            seen[old] = str(uuid.uuid5(NS, str(len(seen))))
        return f'(uuid "{seen[old]}")'

    return re.sub(r'\(uuid "([0-9a-fA-F-]{36})"\)', sub, text)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--netlist", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--kicad-share", required=True)
    ap.add_argument("--no-route", action="store_true")
    a = ap.parse_args(argv)
    out = Path(a.out)
    board, report = build(Path(a.netlist), Path(a.kicad_share), route=not a.no_route)
    pcb = out / f"{PROJECT}.kicad_pcb"
    pcbnew.SaveBoard(str(pcb), board)
    pcb.write_text(deterministic_uuids(pcb.read_text(encoding="utf-8")), encoding="utf-8", newline="\n")
    (out / f"{PROJECT}.kicad_pro").write_text(project_json(), encoding="utf-8", newline="\n")
    (out / f"{PROJECT}.kicad_dru").write_text(CUSTOM_RULES, encoding="utf-8", newline="\n")
    for extra in out.glob(f"{PROJECT}.kicad_prl"):
        extra.unlink()
    print(json.dumps(report))
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
