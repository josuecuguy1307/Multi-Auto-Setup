"""P4-SECBOOT (DESIGN-ONLY): the key's secure-boot and lock design is documented and checked against what the firmware assumes.

Spec: MISSION 7 (Fase 4: "diseño de arranque seguro documentado"), DECISIONS D-007 (STM32U585, what emulation cannot verify), A-1 §4,
A-2 §4 (key page, key_id), reviewer R-1 M6. The document is device/firmware/SECURE_BOOT.md. These tests do not and cannot show that a
chip is locked; they show the design names the mechanisms and that every assumption it lists matches the firmware source, so a firmware
change that breaks an assumption turns this criterion red.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FW = ROOT / "device" / "firmware"
DOC = FW / "SECURE_BOOT.md"
STATUSES = {"UNVERIFIED", "SIMULATED", "EMULATED", "SIMULATED+EMULATED"}


def doc() -> str:
    assert DOC.exists(), "device/firmware/SECURE_BOOT.md is missing"
    return DOC.read_text(encoding="utf-8")


def defines(path: Path, prefix: str) -> dict[str, int]:
    return {m.group(1): int(m.group(2), 0) for m in re.finditer(rf"#define ({prefix}\w*) (0x[0-9A-Fa-f]+|\d+)u?\b", path.read_text(encoding="utf-8"))}


def assumption_rows(text: str) -> list[list[str]]:
    return [[c.strip() for c in line.strip().strip("|").split("|")] for line in text.splitlines() if re.match(r"\|\s*F\d+\s*\|", line)]


def test_the_document_is_labelled_and_names_every_lock_mechanism():
    t = doc()
    assert "DESIGN-ONLY" in t and "UNVERIFIED" in t
    for must in ("RDP", "level 2", "TZEN", "SECBOOTADD0", "BOOT_LOCK", "SECWM", "HDP", "WRP", "TrustZone", "key identity", "key_id",
                 "mass erase", "RM0456"):
        assert must in t, must
    assert re.search(r"not (been )?(run|tested|verified) on (real )?hardware", t, re.IGNORECASE)


def test_every_listed_firmware_assumption_points_at_real_code():
    rows = assumption_rows(doc())
    assert len(rows) >= 9, rows
    assert len({r[0] for r in rows}) == len(rows)
    for r in rows:
        assert len(r) == 5, r
        refs = re.findall(r"`([A-Za-z_][\w]*)` in `([\w/.]+)`", r[2])
        assert refs, r
        for symbol, rel in refs:
            src = FW / rel
            assert src.exists(), (r[0], rel)
            assert re.search(rf"\b{re.escape(symbol)}\b", src.read_text(encoding="utf-8")), (r[0], symbol, rel)
        assert r[4] in STATUSES, r


def test_the_opcode_allowlist_in_the_document_is_the_firmwares():
    ops = defines(FW / "core" / "mas_core.h", "MAS_OP_")
    assert ops
    m = re.search(r"Opcode allowlist:([^\n]+)", doc())
    assert m, "no 'Opcode allowlist:' line"
    listed = {(int(v, 16), n) for v, n in re.findall(r"(0x[0-9A-Fa-f]{2}) ([A-Z_]+)", m.group(1))}
    assert listed == {(v, k.removeprefix("MAS_OP_")) for k, v in ops.items()}
    core = (FW / "core" / "mas_core.c").read_text(encoding="utf-8")
    assert re.search(r"if \(op != MAS_OP_GET_INFO && op != MAS_OP_REQUEST && op != MAS_OP_SIGN\) \{ reply\(ST_UNKNOWN_OPCODE", core)


def test_the_memory_map_in_the_document_is_the_firmwares():
    pages = defines(FW / "core" / "mas_hal.h", "MAS_PAGE")
    assert set(pages) >= {"MAS_PAGE_SIZE", "MAS_PAGE_KEY", "MAS_PAGE_CTR_A", "MAS_PAGE_CTR_B"}
    t = doc()
    for name, value in pages.items():
        assert re.search(rf"`{name}`\s*=\s*{value}\b", t), (name, value)


def test_the_only_flash_writer_is_the_nv_module_as_the_document_says():
    assert "mas_nv.c" in doc() and re.search(r"only flash writer", doc())
    for p in (FW / "core").glob("*.c"):
        calls = re.findall(r"\bhal_flash_(program|erase)\s*\(", p.read_text(encoding="utf-8"))
        assert not calls or p.name == "mas_nv.c", p.name


def test_a_trng_failure_never_yields_a_key_as_the_document_says():
    core = (FW / "core" / "mas_core.c").read_text(encoding="utf-8")
    assert re.search(r"if \(hal_trng\(seed\) != 0\) \{ fault = 1; return; \}", core)
    assert re.search(r"if \(fault\) \{ reply\(ST_NV_FAULT", core)
    assert re.search(r"TRNG[^\n]*fail[^\n]*(fault|closed)", doc(), re.IGNORECASE)


def test_key_id_is_derived_as_the_document_says():
    core = (FW / "core" / "mas_core.c").read_text(encoding="utf-8")
    assert re.search(r"memcpy\(key_id, h, 8\)", core)
    assert "sha256(pub)[:8]" in doc()


def test_only_the_board_layer_presses_the_button():
    """The design puts the button's GPIO and interrupt in the secure world; in code, mas_button_isr is called only by a target's HAL or
    driver, never from the core's receive path."""
    callers = [p for p in FW.rglob("*.c") if "vendor" not in p.parts and re.search(r"\bmas_button_isr\s*\(", p.read_text(encoding="utf-8"))]
    names = {p.relative_to(FW).as_posix() for p in callers}
    assert names and all(n.startswith(("host/", "an505/", "tests/", "core/mas_core.c")) for n in names), names  # tests/: host-only tools
    code = re.sub(r"/\*.*?\*/|//[^\n]*", "", (FW / "core" / "mas_core.c").read_text(encoding="utf-8"), flags=re.DOTALL)
    assert len(re.findall(r"\bmas_button_isr\s*\(", code)) == 1  # its definition: nothing in the core (the receive path) calls it
    assert "mas_button_isr" in doc()


def test_the_review_of_the_lock_is_left_to_a_person():
    t = (ROOT / "NEEDS_HUMAN.md").read_text(encoding="utf-8")
    assert re.search(r"SECURE_BOOT\.md", t)
