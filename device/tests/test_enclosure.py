"""P5-ENCLOSURE (DESIGN-ONLY): a parametric enclosure generated from the board's own facts fits it by automatic check.

Spec: MISSION 7 Fase 5 ("carcasa"), A-1 §8.9 (parametric model with a fit check only), the board evaluator's run 2 note (the OLED module
overhang belongs here). The facts come from the real board through pcbnew (inspect_board.py); part heights and the OLED module's size
are UNVERIFIED drawings listed for the human review. Nothing was printed or fitted. Each check has a negative control: a broken
enclosure must be reported.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HW = ROOT / "device" / "hardware"
OUT = HW / "enclosure"
sys.path.insert(0, str(HW))
import design  # noqa: E402
import enclosure as enc  # noqa: E402
import hwbuild  # noqa: E402


@pytest.fixture(scope="module")
def facts():
    try:
        tools = hwbuild.tools()
    except hwbuild.HwError as exc:
        pytest.fail(str(exc))
    r = subprocess.run([str(tools["python"]), str(HW / "inspect_board.py"), str(HW / "kicad" / "mas_key.kicad_pcb")],
                       capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(r.stdout)


@pytest.fixture(scope="module")
def geometry(facts):
    return enc.build(facts)


def test_every_part_of_the_board_is_in_the_model(facts, geometry):
    assert {p["ref"] for p in geometry["parts"]} == {p.ref for p in design.PARTS}
    assert all(f["layer"] == "F.Cu" for f in facts["footprints"]), "a part on the bottom side would need its own floor clearance"


def test_the_enclosure_fits_the_board(facts, geometry):
    assert enc.check(geometry, facts) == []


def test_the_committed_enclosure_is_a_fresh_build(facts, geometry, tmp_path):
    assert json.loads((OUT / "enclosure.json").read_text(encoding="utf-8")) == json.loads(json.dumps(geometry))
    assert (OUT / "mas_key_enclosure.scad").read_text(encoding="utf-8") == enc.scad(geometry)


def test_labels_and_review(geometry):
    scad = (OUT / "mas_key_enclosure.scad").read_text(encoding="utf-8")
    assert geometry["label"] == "DESIGN-ONLY" and "DESIGN-ONLY" in scad and "NO FABRICAR" in scad
    assert set(geometry["unverified"]) >= {"HEIGHT", "OLED", "usb_overmold"}
    review = (HW / "HUMAN_REVIEW_PCB.md").read_text(encoding="utf-8")
    assert "enclosure" in review.lower() and "mas_key_enclosure.scad" in review


def _broken(geometry, fn):
    g = copy.deepcopy(geometry)
    fn(g)
    return g


NEGATIVE = {
    "cavity smaller than the board": lambda g: g["cavity"].__setitem__(2, g["cavity"][2] - 3),
    "lid too low": lambda g: g["cavity_z"].__setitem__(1, g["cavity_z"][1] - 4),
    "floor too high for J2's leads": lambda g: g["cavity_z"].__setitem__(0, -1.0),
    "USB opening smaller than an overmold": lambda g: g["usb_opening"].__setitem__("x", [g["usb_opening"]["x"][0] + 2, g["usb_opening"]["x"][1] - 2]),
    "USB opening off axis": lambda g: g["usb_opening"].__setitem__("x", [v + 1.0 for v in g["usb_opening"]["x"]]),
    "USB mouth recessed too far": lambda g: g["usb_opening"].__setitem__("face_y", g["usb_opening"]["face_y"] - 3),
    "button hole off the button": lambda g: g["button_hole"].__setitem__("at", [g["button_hole"]["at"][0] + 1, g["button_hole"]["at"][1]]),
    "plunger pressing at rest": lambda g: g["plunger"].__setitem__("z", [g["parts"][[p["ref"] for p in g["parts"]].index("SW1")]["z"][1] - 0.1, g["plunger"]["z"][1]]),
    "plunger without travel": lambda g: g["plunger"].__setitem__("z", [g["plunger"]["z"][0], g["outer_z"][1] + 0.1]),
    "display module laid back over the board": lambda g: g["oled"].__setitem__("box", [g["oled"]["box"][0], g["oled"]["box"][1] - 27, g["oled"]["box"][2], g["oled"]["box"][3] - 27]),
    "display window off the active area": lambda g: g.__setitem__("window", [v + 6 for v in g["window"]]),
}


@pytest.mark.parametrize("name", sorted(NEGATIVE))
def test_negative_control_each_broken_enclosure_is_reported(facts, geometry, name):
    assert enc.check(_broken(geometry, NEGATIVE[name]), facts), name


def test_negative_control_a_thick_front_wall_recesses_the_usb_mouth(facts):
    assert any("recessed" in v for v in enc.check(enc.build(facts, {"wall": 4.0}), facts))
