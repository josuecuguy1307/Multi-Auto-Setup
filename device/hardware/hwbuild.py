"""One command for the key board (DESIGN-ONLY):  python device/hardware/hwbuild.py [--out DIR]

design.py -> gen_sch.py -> kicad-cli netlist -> gen_pcb.py (KiCad's Python, board built from that netlist) -> kicad-cli fab exports, each
stamped "NO FABRICAR sin revisión humana" and suffixed _NO_FABRICAR -> bom_snapshot.json. Default output: this folder (kicad/, fab/).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import design  # noqa: E402

PROJECT = "mas_key"


class HwError(Exception):
    pass


def tools() -> dict:
    roots = [os.environ.get("KICAD_ROOT", ""), str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/KiCad/10.0"),
             "C:/Program Files/KiCad/10.0"]
    for r in roots:
        if r and (Path(r) / "bin" / "kicad-cli.exe").exists():
            b = Path(r) / "bin"
            return {"cli": b / "kicad-cli.exe", "python": b / "python.exe", "share": Path(r) / "share" / "kicad"}
    cli = shutil.which("kicad-cli")
    if cli:
        return {"cli": Path(cli), "python": Path(sys.executable), "share": Path("/usr/share/kicad")}
    raise HwError("ERROR KiCad 10 not found (kicad-cli). Install it (NEEDS_HUMAN #8) or set KICAD_ROOT")


def _run(cmd, what: str, env=None) -> str:
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, env=env)
    if r.returncode != 0:
        raise HwError(f"ERROR {what} failed ({r.returncode}):\n{(r.stdout + r.stderr)[-3000:]}")
    return r.stdout


def stamp_file(p: Path) -> Path:
    text = p.read_text(encoding="utf-8")
    suf = p.suffix.lower()
    if suf in (".gbr", ".gtl", ".gbl", ".gto", ".gbo", ".gts", ".gbs", ".gtp", ".gbp", ".gm1"):
        lines = text.split("\n", 1)
        text = lines[0] + "\nG04 " + design.STAMP + "*\n" + (lines[1] if len(lines) > 1 else "")
    elif suf == ".drl":
        text = text.replace("M48\n", "M48\n; " + design.STAMP + "\n", 1) if "M48\n" in text else "; " + design.STAMP + "\n" + text
    elif suf == ".csv":
        text = "# " + design.STAMP + "\n" + text
    else:
        raise HwError(f"ERROR no stamping rule for {p.name}")
    if design.STAMP not in text:
        raise HwError(f"ERROR stamp missing in {p.name}")
    dst = p.with_name(p.stem + "_NO_FABRICAR" + p.suffix)
    dst.write_text(text, encoding="utf-8", newline="\n")
    p.unlink()
    return dst


def bom_snapshot() -> dict:
    parts = {}
    for p in design.PARTS:
        if p.dnp:
            continue
        e = parts.setdefault(p.mpn, {"manufacturer": p.manufacturer, "refs": [], "footprint": p.footprint, "source": "none",
                                     "availability": "not looked up: no live distributor query was made for this snapshot",
                                     "lifecycle": "unknown: not looked up"})
        e["refs"].append(p.ref)
    return {"snapshot_date": design.BOM_SNAPSHOT_DATE, "note": "Availability, price and lifecycle must be checked on a current date before "
            "any purchase (HUMAN_REVIEW_PCB.md item 10).", "parts": parts}


def build(out: Path = HERE, t: dict | None = None) -> None:
    t = t or tools()
    out = Path(out)
    kc, fab = out / "kicad", out / "fab"
    kc.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, KICAD_SHARE=str(t["share"]))
    _run([sys.executable, HERE / "gen_sch.py", "--kicad-share", t["share"], "--out", kc], "schematic generation")
    sch, net = kc / f"{PROJECT}.kicad_sch", kc / f"{PROJECT}.net"
    _run([t["cli"], "sch", "export", "netlist", "--format", "kicadsexpr", "-o", net, sch], "netlist export")
    text = net.read_text(encoding="utf-8")
    text = re.sub(r'\(date "[^"]*"\)', '(date "")', text)
    text = re.sub(r'\(source "[^"]*"\)', f'(source "{PROJECT}.kicad_sch")', text)
    net.write_text(text, encoding="utf-8", newline="\n")
    _run([t["python"], HERE / "gen_pcb.py", "--netlist", net, "--out", kc, "--kicad-share", t["share"]], "board generation", env=env)
    pcb = kc / f"{PROJECT}.kicad_pcb"
    if fab.exists():
        shutil.rmtree(fab)
    fab.mkdir(parents=True)
    layers = "F.Cu,B.Cu,F.Paste,F.SilkS,B.SilkS,F.Mask,B.Mask,Edge.Cuts"
    _run([t["cli"], "pcb", "export", "gerbers", "--layers", layers, "--no-protel-ext", "-o", str(fab) + os.sep, pcb], "gerber export")
    _run([t["cli"], "pcb", "export", "drill", "--format", "excellon", "--drill-origin", "absolute", "--excellon-units", "mm",
          "-o", str(fab) + os.sep, pcb], "drill export")
    _run([t["cli"], "pcb", "export", "pos", "--format", "csv", "--units", "mm", "--side", "both", "-o", fab / f"{PROJECT}-pos.csv", pcb],
         "position export")
    _run([t["cli"], "sch", "export", "bom", "--fields", "Reference,Value,Footprint,MPN,Manufacturer,${QUANTITY},${DNP}",
          "--group-by", "Value,Footprint,MPN", "-o", fab / f"{PROJECT}-bom.csv", sch], "BOM export")
    for p in sorted(fab.iterdir()):
        if p.suffix.lower() == ".gbrjob":
            p.unlink()  # job file: not needed, and JSON cannot carry the stamp as plainly
        else:
            stamp_file(p)
    (out / "bom_snapshot.json").write_text(json.dumps(bom_snapshot(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(HERE))
    a = ap.parse_args(argv)
    try:
        build(Path(a.out))
    except HwError as exc:
        print(exc, file=sys.stderr)
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
