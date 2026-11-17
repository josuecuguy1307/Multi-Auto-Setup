"""P5-PCB (DESIGN-ONLY): the key board's schematic and PCB are generated from device/hardware/design.py and pass KiCad's own checks.

Spec: DECISIONS.md A-3 (advisor, Fable 5.2), A-1 §6. Passing these checks proves the files are self-consistent and manufacturable by the
rules given; it proves nothing electrical: no circuit simulation, no prototype, no power-up. Fab outputs say NO FABRICAR sin revisión humana.

Needs KiCad 10 (`kicad-cli`, KiCad's bundled Python with pcbnew). A missing KiCad is a FAIL, never a skip."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HW = ROOT / "device" / "hardware"
KICAD = HW / "kicad"
FAB = HW / "fab"
STAMP = "NO FABRICAR sin revisión humana"
sys.path.insert(0, str(HW))
import design  # noqa: E402
import hwbuild  # noqa: E402
from sexpr import parse  # noqa: E402


@pytest.fixture(scope="module")
def tools():
    try:
        return hwbuild.tools()
    except hwbuild.HwError as exc:
        pytest.fail(str(exc))


@pytest.fixture(scope="module")
def rebuilt(tools, tmp_path_factory):
    out = tmp_path_factory.mktemp("hw")
    try:
        hwbuild.build(out, tools)
    except hwbuild.HwError as exc:
        pytest.fail(str(exc))
    return out


def run(cmd, **kw):
    return subprocess.run([str(c) for c in cmd], capture_output=True, text=True, **kw)


def erc(tools, sch: Path, report: Path) -> tuple[int, list]:
    r = run([tools["cli"], "sch", "erc", "--severity-all", "--exit-code-violations", "--format", "json", "-o", report, sch])
    data = json.loads(report.read_text(encoding="utf-8"))
    return r.returncode, [v for s in data["sheets"] for v in s["violations"]]


def drc(tools, pcb: Path, report: Path) -> tuple[int, dict]:
    r = run([tools["cli"], "pcb", "drc", "--schematic-parity", "--severity-all", "--exit-code-violations", "--format", "json",
             "-o", report, pcb])
    return r.returncode, json.loads(report.read_text(encoding="utf-8"))


def netlist(path: Path) -> dict[str, set[tuple[str, str]]]:
    """net name -> {(ref, pin)} from a KiCad s-expression netlist."""
    doc = parse(path.read_text(encoding="utf-8"))
    nets = {}
    for n in [e for e in doc if isinstance(e, list) and e[0] == "nets"][0][1:]:
        name = [e for e in n if isinstance(e, list) and e[0] == "name"][0][1]
        nodes = {([x for x in e if isinstance(x, list) and x[0] == "ref"][0][1], [x for x in e if isinstance(x, list) and x[0] == "pin"][0][1])
                 for e in n if isinstance(e, list) and e[0] == "node"}
        nets[name] = nodes
    return nets


# ---- generated, not hand-edited ---------------------------------------------------------------

def test_committed_design_files_are_exactly_what_the_generators_produce(rebuilt):
    for name in ("mas_key.kicad_sch", "mas_key.kicad_pcb", "mas_key.kicad_pro", "mas_key.kicad_dru", "mas_key.net", "sym-lib-table",
                 "fp-lib-table"):
        assert (KICAD / name).read_bytes() == (rebuilt / "kicad" / name).read_bytes(), f"{name} differs from a fresh generation"


def test_generation_is_deterministic(tools, rebuilt):
    """The committed files came from an earlier run; the test above already requires a fresh run to reproduce them byte for byte. Here the
    schematic generator alone runs twice more (cheap), so a nondeterminism in it shows up even before anything is committed."""
    import gen_sch
    a, _ = gen_sch.generate(tools["share"])
    b, _ = gen_sch.generate(tools["share"])
    assert a == b == (rebuilt / "kicad" / "mas_key.kicad_sch").read_text(encoding="utf-8")


# ---- KiCad's own checks ------------------------------------------------------------------------

def test_erc_is_clean_with_every_severity(tools, tmp_path):
    code, violations = erc(tools, KICAD / "mas_key.kicad_sch", tmp_path / "erc.json")
    assert violations == [] and code == 0, [(v["type"], v["description"]) for v in violations][:20]


def test_drc_is_clean_with_schematic_parity_and_nothing_unconnected(tools, tmp_path):
    code, rep = drc(tools, KICAD / "mas_key.kicad_pcb", tmp_path / "drc.json")
    assert rep["violations"] == [], [(v["type"], v["description"]) for v in rep["violations"]][:20]
    assert rep["unconnected_items"] == [], len(rep["unconnected_items"])
    assert rep["schematic_parity"] == [], [(v["type"], v["description"]) for v in rep["schematic_parity"]][:20]
    assert code == 0


def test_no_exclusions_and_no_lowered_severities_and_rules_at_or_above_the_fab(tools):
    pro = json.loads((KICAD / "mas_key.kicad_pro").read_text(encoding="utf-8"))
    board = pro.get("board", {})
    assert not board.get("drc_exclusions") and not pro.get("erc", {}).get("erc_exclusions")
    for sev in list(board.get("design_settings", {}).get("rule_severities", {}).values()) + \
            list(pro.get("erc", {}).get("rule_severities", {}).values()):
        assert sev != "ignore"
    rules = board["design_settings"]["rules"]
    assert rules["min_clearance"] >= 0.127 and rules["min_track_width"] >= 0.127
    assert rules["min_through_hole_diameter"] >= 0.3 and rules["min_copper_edge_clearance"] >= 0.3
    assert rules["min_hole_to_hole"] >= 0.5
    sch = (KICAD / "mas_key.kicad_sch").read_text(encoding="utf-8")
    assert sch.count("(no_connect") <= 40, "no-connect flags are capped: unused pins are a list, not an escape hatch"
    pcb = (KICAD / "mas_key.kicad_pcb").read_text(encoding="utf-8")
    assert "exclusion" not in pcb.lower()


# ---- the circuit, from the netlist KiCad exported ------------------------------------------------

@pytest.fixture(scope="module")
def nets():
    return netlist(KICAD / "mas_key.net")


def refs_on(nets, net, prefix=""):
    return {r for r, _ in nets.get(net, set()) if r.startswith(prefix)}


def two_pin_between(nets, ref, a, b) -> bool:
    return ref in refs_on(nets, a) and ref in refs_on(nets, b)


def value_of(ref):
    return next(p.value for p in design.PARTS if p.ref == ref)


def supply_check(nets) -> list[str]:
    """Every supply pin of U1 is on a net with capacitors to GND, at least one per supply pin on that net."""
    errors = []
    u1 = next(p for p in design.PARTS if p.ref == "U1")
    supply = [n for k, n in u1.pins.items() if k.startswith(("VDD", "VBAT", "VCAP"))]
    for net in sorted(set(supply)):
        pins = len([r for r in nets.get(net, set()) if r[0] == "U1"])
        caps = {r for r in refs_on(nets, net, "C") if two_pin_between(nets, r, net, "GND")}
        needed = pins + (2 if net == "+3V3" else 0)  # + the regulator's output capacitors
        if len(caps) < needed:
            errors.append(f"{net}: {len(caps)} capacitors to GND for {pins} supply pins")
    return errors


def test_every_supply_pin_has_its_decoupling(nets):
    assert supply_check(nets) == []


def test_button_net_reaches_exactly_one_mcu_pin_and_no_other_ic(nets):
    nodes = nets["BTN"]
    assert len([n for n in nodes if n[0] == "U1"]) == 1
    assert {r for r, _ in nodes if r[0] in "UJ" and r != "U1"} == set()
    assert two_pin_between(nets, "R4", "+3V3", "BTN_RAW") and value_of("R4") == "10k"
    assert refs_on(nets, "BTN_RAW", "SW") == {"SW1"} and two_pin_between(nets, "SW1", "BTN_RAW", "GND")


def test_usb_c_cc_pins_each_have_their_own_5k1_to_ground(nets):
    assert "CC1" in nets and "CC2" in nets and nets["CC1"].isdisjoint(nets["CC2"])
    for cc in ("CC1", "CC2"):
        rs = [r for r in refs_on(nets, cc, "R") if two_pin_between(nets, r, cc, "GND")]
        assert len(rs) == 1 and value_of(rs[0]) == "5.1k", cc


def test_usb_data_lines_go_connector_esd_mcu_only(nets):
    for n in ("USB_DP", "USB_DM"):
        assert {r for r, _ in nets[n]} == {"J1", "U2", "U1"}, n


def test_straps_resets_and_pullups(nets):
    assert any(two_pin_between(nets, r, "BOOT0", "GND") and value_of(r) == "10k" for r in refs_on(nets, "BOOT0", "R"))
    assert any(two_pin_between(nets, c, "NRST", "GND") and value_of(c) == "100nF" for c in refs_on(nets, "NRST", "C"))
    for n in ("I2C_SCL", "I2C_SDA"):
        assert len([r for r in refs_on(nets, n, "R") if two_pin_between(nets, r, n, "+3V3")]) == 1, n


def test_vbus_capacitance_stays_under_the_usb_limit(nets):
    caps = [c for c in refs_on(nets, "VBUS", "C") if two_pin_between(nets, c, "VBUS", "GND")]
    total_uf = sum(float(value_of(c).replace("uF", "")) if "uF" in value_of(c) else float(value_of(c).replace("nF", "")) / 1000 for c in caps)
    assert 0 < total_uf <= 10.0


def test_no_crystal_pins_are_connected(nets):
    for pin in design.EXPECTED_NO_CONNECT:
        assert pin not in design.MCU_PINS


# ---- fabrication outputs, BOM, human review ----------------------------------------------------

def test_fab_outputs_exist_and_every_file_is_stamped(rebuilt):
    for fab in (FAB, rebuilt / "fab"):
        files = [p for p in fab.rglob("*") if p.is_file()]
        kinds = {p.suffix.lower() for p in files}
        assert {".gbr", ".drl", ".csv"} <= kinds or ({".gtl", ".gbl"} & kinds and ".drl" in kinds), kinds
        assert len([p for p in files if p.suffix.lower() in (".gbr", ".gtl", ".gbl", ".gto", ".gbo", ".gts", ".gbs", ".gm1")]) >= 7
        for p in files:
            assert "_NO_FABRICAR" in p.name, p.name
            assert STAMP in p.read_text(encoding="utf-8", errors="strict"), p.name
        assert not [p for p in files if p.suffix.lower() in (".zip", ".7z", ".rar")]


def test_board_carries_the_stamp_on_both_silkscreens():
    pcb = (KICAD / "mas_key.kicad_pcb").read_text(encoding="utf-8")
    for layer in ("F.SilkS", "B.SilkS"):
        assert re.search(r'\(gr_text "NO FABRICAR[^"]*"[^)]*\)?\s*\(at [^)]*\)\s*\(layer "' + re.escape(layer), pcb) or \
            re.search(r'NO FABRICAR[\s\S]{0,200}\(layer "' + re.escape(layer) + '"', pcb), layer


def test_bom_snapshot_is_dated_honest_and_complete():
    snap = json.loads((HW / "bom_snapshot.json").read_text(encoding="utf-8"))
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", snap["snapshot_date"])
    mpns = {p.mpn for p in design.PARTS if not p.dnp}
    assert mpns <= set(snap["parts"]), mpns - set(snap["parts"])
    for mpn, entry in snap["parts"].items():
        assert entry["manufacturer"] and entry["source"] and entry["availability"] and entry["lifecycle"], mpn
        if entry["source"] == "none":
            assert "not looked up" in entry["availability"], mpn  # no invented stock numbers


def test_every_custom_drc_rule_is_on_the_human_review_list():
    dru = (KICAD / "mas_key.kicad_dru").read_text(encoding="utf-8")
    names = re.findall(r'\(rule "([^"]+)"', dru)
    review = (HW / "HUMAN_REVIEW_PCB.md").read_text(encoding="utf-8")
    assert names, "the rule file is generated with its rules"
    for n in names:
        assert n in review, n


def test_human_review_list_is_complete():
    text = (HW / "HUMAN_REVIEW_PCB.md").read_text(encoding="utf-8")
    rows = re.findall(r"^\| (\d+) \|", text, re.M)
    assert rows == [str(i) for i in range(1, 11)]
    assert "DESIGN-ONLY" in text and STAMP in text
    for item in design.UNVERIFIED:
        assert item in text, item


# ---- saboteurs: a damaged board or circuit must fail -------------------------------------------

def _copy_project(tmp_path) -> Path:
    dst = tmp_path / "kicad"
    shutil.copytree(KICAD, dst)
    return dst


def test_saboteur_deleted_track_is_caught_by_drc(tools, tmp_path):
    proj = _copy_project(tmp_path)
    pcb = proj / "mas_key.kicad_pcb"
    text = pcb.read_text(encoding="utf-8")
    # every track of one signal net (a single short stub can lie inside a pad and change nothing; GND stubs can be redundant with the pours)
    pat = re.compile(r'\n\t\(segment\n(?:(?!\n\t\)).)*?\(net "I2C_SDA"\)(?:(?!\n\t\)).)*?\n\t\)', re.S)
    new, n = pat.subn("", text)
    assert n >= 2, "the board has no I2C_SDA tracks"
    pcb.write_text(new, encoding="utf-8", newline="\n")
    code, rep = drc(tools, pcb, tmp_path / "drc.json")
    assert code != 0 and rep["unconnected_items"], "deleting a track must leave something unconnected"


def test_saboteur_changed_pad_net_is_caught_by_parity(tools, tmp_path):
    proj = _copy_project(tmp_path)
    pcb = proj / "mas_key.kicad_pcb"
    text = pcb.read_text(encoding="utf-8")
    new = re.sub(r'(\(net "?)I2C_SDA("?\))', r"\1I2C_SCL\2", text, count=1)
    assert new != text
    pcb.write_text(new, encoding="utf-8", newline="\n")
    code, rep = drc(tools, pcb, tmp_path / "drc.json")
    assert code != 0 and (rep["schematic_parity"] or rep["violations"] or rep["unconnected_items"])


def test_saboteur_missing_decoupling_cap_is_caught_by_the_net_check(nets):
    weakened = {n: {node for node in nodes if node[0] != "C4"} for n, nodes in nets.items()}
    assert supply_check(weakened) != []



# ---- evaluator run 1 (device/evals/evaluator/P5-PCB.md): checks read from the board and the netlist, pin by pin ------------

import math


@pytest.fixture(scope="module")
def board(tools):
    r = run([tools["python"], HW / "inspect_board.py", KICAD / "mas_key.kicad_pcb"])
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(r.stdout)


def nodes(path: Path):
    """[(net, ref, pin, pinfunction, pintype)] and {ref: value} from the KiCad netlist."""
    doc = parse(path.read_text(encoding="utf-8"))
    sec = lambda x, k: [e for e in x if isinstance(e, list) and e and e[0] == k]
    val = lambda x, k: sec(x, k)[0][1] if sec(x, k) else ""
    out = []
    for n in sec(sec(doc, "nets")[0], "net"):
        for node in sec(n, "node"):
            pin, func = val(node, "pin"), val(node, "pinfunction")
            func = func[: -len(pin) - 1] if func.endswith("_" + pin) else func  # KiCad 10 suffixes the pin number: PA12_33 -> PA12
            out.append((val(n, "name"), val(node, "ref"), pin, func, val(node, "pintype")))
    values = {val(c, "ref"): val(c, "value") for c in sec(sec(doc, "components")[0], "comp")}
    return out, values


@pytest.fixture(scope="module")
def pins():
    return nodes(KICAD / "mas_key.net")


def net_of(pins, ref, func=None, number=None):
    found = [n for n, r, p, f, _ in pins[0] if r == ref and (func is None or f == func) and (number is None or p == number)]
    assert found, (ref, func, number)
    return found


def farads(v: str) -> float:
    m = re.fullmatch(r"([\d.]+)\s*([pnu]?)F", v)
    assert m, v
    return float(m.group(1)) * {"p": 1e-12, "n": 1e-9, "u": 1e-6, "": 1}[m.group(2)]


def ohms(v: str) -> float:
    m = re.fullmatch(r"([\d.]+)([kM]?)", v)
    assert m, v
    return float(m.group(1)) * {"k": 1e3, "M": 1e6, "": 1}[m.group(2)]


def test_the_silk_stamp_is_whole_visible_legible_and_on_the_board_both_sides(board):
    x0, y0, x1, y1 = board["outline"]
    for layer in ("F.Silkscreen", "B.Silkscreen"):
        ok = [t for t in board["texts"] if t["layer"] == layer and t["text"] == STAMP and t["visible"] and t["height"] >= 0.8
              and x0 <= t["bbox"][0] and t["bbox"][2] <= x1 and y0 <= t["bbox"][1] and t["bbox"][3] <= y1]
        assert ok, layer


def test_usb_data_lines_are_top_layer_only_with_no_vias(board):
    usb = [t for t in board["tracks"] if t["net"] in ("USB_DP", "USB_DM")]
    assert usb and all(t["layer"] == "F.Cu" for t in usb)
    assert not [v for v in board["vias"] if v["net"] in ("USB_DP", "USB_DM")]


def _seg_dist(a, b, c, d):
    def pt(p, q, r):
        vx, vy = r[0] - q[0], r[1] - q[1]
        ll = vx * vx + vy * vy
        t = 0 if ll == 0 else max(0, min(1, ((p[0] - q[0]) * vx + (p[1] - q[1]) * vy) / ll))
        return math.hypot(p[0] - q[0] - t * vx, p[1] - q[1] - t * vy)
    def inter(p1, p2, p3, p4):
        def o(a, b, c):
            return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        return o(p1, p2, p3) * o(p1, p2, p4) < 0 and o(p3, p4, p1) * o(p3, p4, p2) < 0
    if inter(a, b, c, d):
        return 0.0
    return min(pt(a, c, d), pt(b, c, d), pt(c, a, b), pt(d, a, b))


def test_nothing_but_ground_runs_under_the_usb_data_lines(board):
    usb = [t for t in board["tracks"] if t["net"] in ("USB_DP", "USB_DM")]
    under = [t for t in board["tracks"] if t["layer"] == "B.Cu" and t["net"] != "GND"]
    close = [(u["net"], b["net"]) for u in usb for b in under if _seg_dist(u["start"], u["end"], b["start"], b["end"]) < 0.5]
    assert close == [], close[:5]


def test_every_mcu_supply_pin_has_its_own_capacitor_within_4_mm(board, pins):
    pads = {(p["ref"], p["number"]): (p["x"], p["y"]) for p in board["pads"]}
    supply = [(n, p, f) for n, r, p, f, t in pins[0] if r == "U1" and t in ("power_in", "power_out") and n != "GND"]
    assert len(supply) >= 6, supply  # VBAT, VDDA, VCAP, VDD x3
    caps = {}
    for n, r, p, f, t in pins[0]:
        if r.startswith("C"):
            caps.setdefault(r, {})[p] = n
    used, missing = set(), []
    for net, pin, func in sorted(supply, key=lambda s: s[1]):
        need = 4.7e-6 if func == "VCAP" else 100e-9
        cands = []
        for ref, pn in caps.items():
            if ref in used or set(pn.values()) != {net, "GND"} or farads(pins[1][ref]) < need * 0.999:
                continue
            pad = next(k for k, v in pn.items() if v == net)
            d = math.dist(pads[("U1", pin)], pads[(ref, pad)])
            cands.append((d, ref))
        if not cands or min(cands)[0] > 4.0:
            missing.append((pin, func, min(cands) if cands else None))
            continue
        used.add(min(cands)[1])
    assert missing == [], missing


def test_the_netlist_is_right_pin_by_pin(pins):
    assert net_of(pins, "U1", "PA12") == ["USB_DP"] and net_of(pins, "U1", "PA11") == ["USB_DM"]
    for p in ("A6", "B6"):
        assert net_of(pins, "J1", number=p) == ["USB_DP"], p
    for p in ("A7", "B7"):
        assert net_of(pins, "J1", number=p) == ["USB_DM"], p
    assert set(net_of(pins, "U2", "I/O1")) == {"USB_DP"} and set(net_of(pins, "U2", "I/O2")) == {"USB_DM"}
    assert net_of(pins, "U1", "PB6") == ["I2C_SCL"] and net_of(pins, "U1", "PB7") == ["I2C_SDA"]
    assert net_of(pins, "U1", "PA0") == ["BTN"] and net_of(pins, "U1", "PH3") == ["BOOT0"]
    for f in ("PH0", "PH1", "PC14", "PC15"):
        assert net_of(pins, "U1", f)[0].startswith("unconnected-"), f


def test_values_come_from_the_netlist_and_are_right(pins):
    nets_by_ref = {}
    for n, r, p, f, t in pins[0]:
        nets_by_ref.setdefault(r, set()).add(n)
    values = pins[1]
    vcap = [r for r, ns in nets_by_ref.items() if r.startswith("C") and ns == {"VCAP", "GND"}]
    assert vcap and all(math.isclose(farads(values[r]), 4.7e-6) for r in vcap)
    for n in ("I2C_SCL", "I2C_SDA"):
        pull = [r for r, ns in nets_by_ref.items() if r.startswith("R") and ns == {n, "+3V3"}]
        assert len(pull) == 1 and math.isclose(ohms(values[pull[0]]), 4.7e3), n
    for n in ("CC1", "CC2"):
        rr = [r for r, ns in nets_by_ref.items() if r.startswith("R") and ns == {n, "GND"}]
        assert len(rr) == 1 and math.isclose(ohms(values[rr[0]]), 5.1e3), n
    dec = [r for r, ns in nets_by_ref.items() if r.startswith("C") and ns == {"+3V3", "GND"} and math.isclose(farads(values[r]), 100e-9)]
    assert len(dec) >= 5, dec


def test_the_custom_rule_file_holds_exactly_the_declared_rule():
    dru = (KICAD / "mas_key.kicad_dru").read_text(encoding="utf-8")
    rules = re.findall(r'\(rule "([^"]+)"', dru)
    assert rules == ["J1 vendor land pattern"]
    assert "(condition \"A.Parent == 'J1' && B.Parent == 'J1'\")" in dru
    assert "(constraint hole_clearance (min 0.19mm))" in dru
    assert "severity" not in dru and "true" not in dru.lower()


def test_the_human_review_lists_every_deviation_and_cites_no_missing_file():
    text = (HW / "HUMAN_REVIEW_PCB.md").read_text(encoding="utf-8")
    for must in ("20 × 54 mm", "0.2 mm", "J1 vendor land pattern", "grid router", "reference designators", "past the board edge",
                 "under the USB-C", "ATECC608B", "J3"):
        assert must in text, must
    for cited in re.findall(r"`([\w./-]+\.md)`", text):
        assert (HW / cited).exists() or (ROOT / cited).exists(), cited


def _without_dates(text: str) -> str:
    return "\n".join(l for l in text.splitlines() if not re.search(r"CreationDate|[Dd]ate|\d{4}-\d{2}-\d{2}T", l))


def test_the_committed_fab_files_are_a_fresh_build(rebuilt):
    mine = {p.name: _without_dates(p.read_text(encoding="utf-8")) for p in FAB.iterdir()}
    fresh = {p.name: _without_dates(p.read_text(encoding="utf-8")) for p in (rebuilt / "fab").iterdir()}
    assert mine.keys() == fresh.keys()
    for name in mine:
        assert mine[name] == fresh[name], name
