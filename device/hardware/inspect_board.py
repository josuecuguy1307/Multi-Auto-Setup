"""Dump the facts the P5-PCB tests check, read from the board itself with KiCad's own pcbnew (not by grepping the file). DESIGN-ONLY.

    <kicad>/bin/python.exe device/hardware/inspect_board.py <board.kicad_pcb>   -> JSON on stdout

texts:  every PCB_TEXT: layer, text, visible, x, y, height, thickness, bbox
tracks: every track segment: net, layer, start, end, width        vias: net, x, y
pads:   every pad: ref, number, net, x, y, layers                   outline: Edge.Cuts bounding box
All coordinates in mm.
"""

from __future__ import annotations

import json
import sys

import pcbnew


def mm(v):
    return round(pcbnew.ToMM(v), 4)


def main(path: str) -> None:
    b = pcbnew.LoadBoard(path)
    out = {"texts": [], "tracks": [], "vias": [], "pads": [], "outline": None}
    for d in b.GetDrawings():
        if isinstance(d, pcbnew.PCB_TEXT) or d.GetClass() == "PCB_TEXT":
            t = d.Cast() if hasattr(d, "Cast") else d
            bb = t.GetBoundingBox()
            out["texts"].append({"layer": b.GetLayerName(t.GetLayer()), "text": t.GetText(), "visible": bool(t.IsVisible()),
                                 "x": mm(t.GetPosition().x), "y": mm(t.GetPosition().y), "height": mm(t.GetTextHeight()),
                                 "thickness": mm(t.GetTextThickness()),
                                 "bbox": [mm(bb.GetX()), mm(bb.GetY()), mm(bb.GetRight()), mm(bb.GetBottom())]})
    for t in b.GetTracks():
        layer = b.GetLayerName(t.GetLayer())
        if t.GetClass() == "PCB_VIA":
            out["vias"].append({"net": t.GetNetname(), "x": mm(t.GetPosition().x), "y": mm(t.GetPosition().y)})
        else:
            out["tracks"].append({"net": t.GetNetname(), "layer": layer, "start": [mm(t.GetStart().x), mm(t.GetStart().y)],
                                  "end": [mm(t.GetEnd().x), mm(t.GetEnd().y)], "width": mm(t.GetWidth())})
    for fp in b.GetFootprints():
        for p in fp.Pads():
            out["pads"].append({"ref": fp.GetReference(), "number": p.GetNumber(), "net": p.GetNetname(),
                                "x": mm(p.GetPosition().x), "y": mm(p.GetPosition().y),
                                "layers": [b.GetLayerName(l) for l in (pcbnew.F_Cu, pcbnew.B_Cu) if p.IsOnLayer(l)]})
    ebb = b.GetBoardEdgesBoundingBox()
    out["outline"] = [mm(ebb.GetX()), mm(ebb.GetY()), mm(ebb.GetRight()), mm(ebb.GetBottom())]
    json.dump(out, sys.stdout, sort_keys=True)


if __name__ == "__main__":
    main(sys.argv[1])
