"""Generate the key board's schematic (KiCad 10 .kicad_sch) from design.py. DESIGN-ONLY. Deterministic: same input, same bytes.

Every stock symbol named in design.py must exist in the installed KiCad libraries (the build fails otherwise). Each connected pin gets a
global label with its net name at the pin's end; each unused pin gets a no_connect flag; VBUS and GND get a PWR_FLAG.

    python device/hardware/gen_sch.py [--kicad-share DIR] [--out DIR]
"""

from __future__ import annotations

import argparse
import copy
import os
import sys
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import design  # noqa: E402
from sexpr import Q, dump, find, first, parse  # noqa: E402

NS = uuid.UUID("5b1f3a52-6d2e-4c43-9a3e-6f0c6b9d0a11")
PROJECT = "mas_key"
GRID = 2.54


class GenError(Exception):
    pass


def uid(*parts) -> Q:
    return Q(str(uuid.uuid5(NS, "/".join(map(str, parts)))))


def kicad_share() -> Path:
    env = os.environ.get("KICAD_SHARE")
    cands = [Path(env)] if env else []
    cands += [Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/KiCad/10.0/share/kicad", Path("C:/Program Files/KiCad/10.0/share/kicad"),
              Path("/usr/share/kicad")]
    for c in cands:
        if (c / "symbols").is_dir():
            return c
    raise GenError("KiCad libraries not found; set KICAD_SHARE")


_libs: dict[str, list] = {}


def load_symbol(share: Path, lib_id: str) -> list:
    lib, name = lib_id.split(":")
    if lib not in _libs:
        p = share / "symbols" / f"{lib}.kicad_sym"
        if not p.exists():
            raise GenError(f"ERROR symbol library {lib} not installed")
        _libs[lib] = parse(p.read_text(encoding="utf-8"))
    syms = {s[1]: s for s in find(_libs[lib], "symbol")}
    if name not in syms:
        raise GenError(f"ERROR symbol {lib_id} not in the installed library")
    s = copy.deepcopy(syms[name])
    ext = first(s, "extends")
    if ext is not None:  # flatten: the parent's graphics and pins, the child's properties
        parent = copy.deepcopy(load_symbol(share, f"{lib}:{ext[1]}"))
        props = {p[1]: p for p in find(s, "property")}
        out = [e for e in parent if not (isinstance(e, list) and e and e[0] == "property" and e[1] in props)]
        out[1] = Q(name)
        for e in out:
            if isinstance(e, list) and e and e[0] == "symbol":
                e[1] = Q(e[1].replace(ext[1] + "_", name + "_", 1))
        idx = max(i for i, e in enumerate(out) if isinstance(e, list) and e and e[0] in ("property", "in_bom", "on_board", "exclude_from_sim"))
        s = out[:idx + 1] + list(props.values()) + out[idx + 1:]
    return s


def pins_of(sym: list) -> list[dict]:
    out = []

    def rec(x):
        for e in x:
            if isinstance(e, list) and e:
                if e[0] == "pin":
                    at = first(e, "at")
                    out.append({"type": e[1], "x": float(at[1]), "y": float(at[2]), "angle": float(at[3]) if len(at) > 3 else 0.0,
                                "name": first(e, "name")[1], "number": first(e, "number")[1], "hidden": "hide" in e or ["hide", "yes"] in e})
                else:
                    rec(e)
    rec(sym)
    return out


def r(v: float) -> float:
    return round(v, 4)


def effects(justify=None, hide=False):
    e = ["effects", ["font", ["size", 1.27, 1.27]]]
    if justify:
        e.append(["justify", justify])
    if hide:
        e.append(["hide", "yes"])
    return e


def prop(name, value, x, y, hide=False):
    return ["property", Q(name), Q(value), ["at", r(x), r(y), 0], effects(hide=hide)]


def net_for(pin: dict, mapping: dict) -> str | None:
    if pin["number"] in mapping:
        return mapping[pin["number"]]
    if pin["name"] in mapping:
        return mapping[pin["name"]]
    return None


def generate(share: Path) -> tuple[str, dict]:
    root = uid("root")
    lib_symbols, placed, items = {}, [], []
    report = {"no_connect": 0, "labels": 0, "nets": set()}
    parts = list(design.PARTS)
    flags = [design.Part(f"#FLG{i + 1:02d}", "power:PWR_FLAG", "", "PWR_FLAG", "", "", {"1": net}) for i, net in enumerate(design.POWER_FLAGS)]
    # layout: shelf packing on a large sheet, sizes from the pins' extent
    x0, y0, row_h, maxw = 30.48, 30.48, 0.0, 1100.0
    cx, cy = x0, y0
    for p in parts + flags:
        sym = load_symbol(share, p.lib_id)
        lib_symbols[p.lib_id] = sym
        pins = pins_of(sym)
        if not pins:
            raise GenError(f"ERROR {p.lib_id} has no pins")
        mapping = dict(p.pins)
        known = {q["number"] for q in pins} | {q["name"] for q in pins}
        missing = [k for k in mapping if k not in known]
        if missing:
            raise GenError(f"ERROR {p.ref}: pins {missing} not on symbol {p.lib_id}")
        for nc in (design.EXPECTED_NO_CONNECT if p.ref == "U1" else []):
            if nc not in known:
                raise GenError(f"ERROR U1 has no pin {nc}")
        minx, maxx = min(q["x"] for q in pins), max(q["x"] for q in pins)
        miny, maxy = min(q["y"] for q in pins), max(q["y"] for q in pins)
        w, h = (maxx - minx) + 50.8, (maxy - miny) + 25.4
        if cx + w > maxw:
            cx, cy, row_h = x0, cy + row_h, 0.0
        ox = round((cx - minx + 25.4) / GRID) * GRID
        oy = round((cy + maxy + 12.7) / GRID) * GRID
        cx += w
        row_h = max(row_h, h)
        placed.append((p, sym, pins, mapping, ox, oy))
    for p, sym, pins, mapping, ox, oy in placed:
        is_flag = p.ref.startswith("#")
        inst = ["symbol", ["lib_id", Q(p.lib_id)], ["at", r(ox), r(oy), 0], ["unit", 1], ["exclude_from_sim", "no"],
                ["in_bom", "no" if is_flag else "yes"], ["on_board", "no" if is_flag else "yes"], ["dnp", "yes" if p.dnp else "no"],
                ["uuid", uid("sym", p.ref)],
                prop("Reference", p.ref, ox, oy - 5.08, hide=is_flag), prop("Value", p.value, ox, oy + 5.08, hide=is_flag),
                prop("Footprint", p.footprint, ox, oy, hide=True), prop("Datasheet", "", ox, oy, hide=True),
                prop("Description", "", ox, oy, hide=True)]
        if not is_flag:
            inst += [prop("MPN", p.mpn, ox, oy, hide=True), prop("Manufacturer", p.manufacturer, ox, oy, hide=True)]
        for q in sorted({q["number"] for q in pins}, key=lambda s: (len(s), s)):
            inst.append(["pin", Q(q), ["uuid", uid("pin", p.ref, q)]])
        inst.append(["instances", ["project", Q(PROJECT), ["path", Q("/" + root), ["reference", Q(p.ref)], ["unit", 1]]]])
        items.append(inst)
        for q in pins:
            px, py = r(ox + q["x"]), r(oy - q["y"])
            net = net_for(q, mapping)
            if net is None:
                if q["type"] in ("power_in", "power_out") and not is_flag:
                    raise GenError(f"ERROR {p.ref} power pin {q['name']}/{q['number']} left unconnected")
                items.append(["no_connect", ["at", px, py], ["uuid", uid("nc", p.ref, q["number"])]])
                report["no_connect"] += 1
                continue
            report["nets"].add(net)
            ang = int((q["angle"] + 180) % 360)
            items.append(["global_label", Q(net), ["shape", "passive"], ["at", px, py, ang], ["fields_autoplaced", "yes"],
                          effects("right" if ang == 180 else "left"), ["uuid", uid("gl", p.ref, q["number"])]])
            report["labels"] += 1
    libsyms = []
    for lib_id in sorted(lib_symbols):
        s = copy.deepcopy(lib_symbols[lib_id])
        s[1] = Q(lib_id)
        libsyms.append(s)
    doc = ["kicad_sch", ["version", 20250114], ["generator", Q("mas_gen_sch")], ["generator_version", Q("10.0")], ["uuid", root],
           ["paper", "A0"],
           ["title_block", ["title", Q("MAS key board")], ["rev", Q("v0")], ["company", Q("MAS portable device — DESIGN-ONLY")],
            ["comment", 1, Q(design.STAMP)], ["comment", 2, Q("Generated from device/hardware/design.py; do not edit by hand")]],
           ["lib_symbols"] + libsyms] + items + [["sheet_instances", ["path", Q("/"), ["page", Q("1")]]], ["embedded_fonts", "no"]]
    report["nets"] = sorted(report["nets"])
    return dump(doc) + "\n", report


def write_lib_tables(out: Path) -> None:
    """Project library tables naming only the stock libraries the design uses, through KiCad's own path variables (no machine paths)."""
    syms = sorted({p.lib_id.split(":")[0] for p in design.PARTS} | {"power"})
    fps = sorted({p.footprint.split(":")[0] for p in design.PARTS if p.footprint})
    s = ["sym_lib_table", ["version", 7]] + [["lib", ["name", Q(n)], ["type", Q("KiCad")], ["uri", Q(f"${{KICAD10_SYMBOL_DIR}}/{n}.kicad_sym")],
                                              ["options", Q("")], ["descr", Q("")]] for n in syms]
    f = ["fp_lib_table", ["version", 7]] + [["lib", ["name", Q(n)], ["type", Q("KiCad")], ["uri", Q(f"${{KICAD10_FOOTPRINT_DIR}}/{n}.pretty")],
                                             ["options", Q("")], ["descr", Q("")]] for n in fps]
    (out / "sym-lib-table").write_text(dump(s) + "\n", encoding="utf-8", newline="\n")
    (out / "fp-lib-table").write_text(dump(f) + "\n", encoding="utf-8", newline="\n")
    pro = out / f"{PROJECT}.kicad_pro"
    if not pro.exists():  # gen_pcb.py writes the full project (rules, net classes); this keeps ERC runnable on its own
        pro.write_text('{\n  "meta": {\n    "filename": "mas_key.kicad_pro",\n    "version": 3\n  }\n}\n', encoding="utf-8", newline="\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kicad-share")
    ap.add_argument("--out", default=str(HERE / "kicad"))
    a = ap.parse_args(argv)
    try:
        share = Path(a.kicad_share) if a.kicad_share else kicad_share()
        text, rep = generate(share)
    except GenError as exc:
        print(exc, file=sys.stderr)
        return 1
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{PROJECT}.kicad_sch").write_text(text, encoding="utf-8", newline="\n")
    write_lib_tables(out)
    print(f"{out / (PROJECT + '.kicad_sch')}: {rep['labels']} labels, {rep['no_connect']} no-connects, {len(rep['nets'])} nets")
    return 0


if __name__ == "__main__":
    sys.exit(main())
