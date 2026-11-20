"""P6-REPO (MISSION 2.G): README, THIRD_PARTY.md and reproduction instructions are complete for the portable device.

"Complete" is checked, not asserted: every criterion in device/acceptance/criteria.yaml has a reproduction entry; every path the device
README points at exists; every third-party component the device vendors or runs as a tool has a THIRD_PARTY row with its license; the
honesty labels and D-010's definition of "no trace" are stated; nothing claims a result on real hardware.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
DEVICE_README = ROOT / "device" / "README.md"
TOOLS = {  # what the device's build and checks run, and the THIRD_PARTY row that must name it
    "Monocypher": "device/firmware/vendor/monocypher", "ziglang": "device/firmware/build.py", "QEMU": "device/tests/test_firmware_emu.py",
    "Renode": "device/tests/test_firmware_renode.py", "KiCad": "device/hardware/hwbuild.py", "cryptography": "src/mas_device/approval.py",
}


def readme() -> str:
    assert DEVICE_README.exists(), "device/README.md is missing"
    return DEVICE_README.read_text(encoding="utf-8")


def criteria() -> list[dict]:
    return yaml.safe_load((ROOT / "device" / "acceptance" / "criteria.yaml").read_text(encoding="utf-8"))["criteria"]


def test_the_root_readme_points_to_the_device():
    t = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "device/README.md" in t and "device/v0" in t


def test_every_criterion_has_a_reproduction_entry():
    t = readme()
    section = t.split("## Reproduce", 1)
    assert len(section) == 2, "no '## Reproduce' section"
    rows = set(re.findall(r"^\| ([A-Z0-9]+-[A-Z0-9-]+) \|", section[1], re.MULTILINE))  # each criterion's own table row, not a mention
    missing = [c["id"] for c in criteria() if c["id"] not in rows]
    assert missing == []
    assert "python scripts/device_accept.py run" in section[1] and "device_accept.py confirm" in section[1]


def test_every_path_the_device_readme_names_exists():
    paths = set(re.findall(r"`((?:device|src|scripts)/[\w./-]+)`", readme()))
    assert len(paths) >= 10
    assert sorted(p for p in paths if not (ROOT / p.rstrip("/")).exists()) == []


def test_labels_and_the_no_trace_definition_are_stated():
    t = readme()
    for label in ("SIMULATED", "EMULATED", "DESIGN-ONLY", "REAL"):
        assert re.search(rf"\*\*{label}\*\*", t), label
    assert "NO FABRICAR" in t and "ACCEPTANCE.md" in t
    assert "Nothing installed: no service, driver, autostart, PATH change or file outside a declared temp dir" in t  # D-010, verbatim
    for claim in re.finditer(r"(sin dejar rastro|without a trace|leaves no trace|zero trace)", t, re.IGNORECASE):
        nearby = t[max(0, claim.start() - 400): claim.end() + 400]
        assert "D-010" in nearby, claim.group(0)


def test_the_readme_claims_nothing_on_real_hardware():
    t = readme().lower()
    for phrase in ("tested on hardware", "works on the board", "verified on the device", "runs on the real key", "production ready"):
        assert phrase not in t, phrase
    assert re.search(r"no board has been (made|built|fabricated)", t)


def test_tools_have_versions_and_install_commands():
    t = readme()
    for tool, version in (("zig", "0.13.0"), ("Renode", "1.17.0"), ("KiCad", "10"), ("QEMU", "11")):
        assert re.search(rf"{tool}[^\n]*{re.escape(version)}", t), tool
    assert "uv pip install" in t and "winget install" in t


def test_third_party_names_every_vendored_component_and_tool_with_a_license():
    tp = (ROOT / "THIRD_PARTY.md").read_text(encoding="utf-8")
    rows = [line for line in tp.splitlines() if line.startswith("|") and not line.startswith("|---")]
    for name, evidence in TOOLS.items():
        assert (ROOT / evidence).exists(), evidence
        row = [r for r in rows if name.lower() in r.lower()]
        assert row, name
        cells = [c.strip() for c in row[0].strip("|").split("|")]
        assert any(re.search(r"(MIT|BSD|GPL|Apache|CC0|CC-BY|LGPL)", c) for c in cells), (name, cells)
        assert "to read at clone" not in row[0] and "not cloned" not in row[0], (name, "a tool in use must have its license read")
    for vendored in (ROOT / "device" / "firmware" / "vendor").iterdir():
        assert vendored.name.lower() in tp.lower(), vendored.name


def test_the_hardware_license_question_is_open_not_silently_decided():
    t = readme()
    assert "CERN-OHL-P-2.0" in t and "NEEDS_HUMAN.md" in t
