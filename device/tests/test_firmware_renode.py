"""P4-FW-RENODE (EMULATED): the very same ELF as P4-FW-EMU (QEMU mps2-an505), run in a second, independent emulator: Renode 1.17.0 with
its stock ARMv8-M TrustZone MPS2 platform (Cortex-M33, CMSDK UART0 at 0x50200000, secure SSRAM at 0x10000000). Transcripts must be
identical to the host build. Renode starts in ~20 s, so each instance runs a long script instead of one instance per short script.

Same limits as P4-FW-EMU: no secure boot, RDP, TrustZone partitioning, real flash or button. Spec: DECISIONS.md D-007, D-018, D-019."""

from __future__ import annotations

import os
import random
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

from mas_device import approval as ap
from test_firmware_emu import SEED, EmuFw, Tick, run_script
from test_firmware_host import FW, ROOT, SESSION, Died, Fw, a_request, fwb, req_payload

RENODE_DIR = ROOT / "runs" / "toolchain" / "renode" / "renode_1.17.0-portable"
REPL_REL = Path("tests") / "platforms" / "ARMv8M-TrustZone_mps2.repl"


def renode_path() -> Path | None:
    for c in (os.environ.get("MAS_RENODE", ""), str(RENODE_DIR / "renode.exe"), shutil.which("renode") or ""):
        if c and Path(c).exists():
            return Path(c)
    return None


class RenodeFw(EmuFw):
    """The same harness over Renode's UART0, bridged to a TCP socket by a server socket terminal."""

    def __init__(self, renode: Path, elf: Path, seed: bytes = SEED):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        repl = renode.parent / REPL_REL
        cmds = ";".join(['mach create "mas_key"', f"machine LoadPlatformDescription @{repl.as_posix()}",
                         f"sysbus LoadELF @{Path(elf).resolve().as_posix()}", f'emulation CreateServerSocketTerminal {port} "term" false',
                         "connector Connect sysbus.uart0 term", "start"])
        self.q = subprocess.Popen([str(renode), "--disable-gui", "-P", "-1", "--plain", "-e", cmds],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.s = None
        deadline = time.time() + 120
        while time.time() < deadline and self.s is None:
            try:
                self.s = socket.create_connection(("127.0.0.1", port), timeout=60)
            except OSError:
                time.sleep(0.2)
        if self.s is None:
            self.q.kill()
            raise Died("Renode did not open its UART")
        self.emitted = []
        self.s.sendall(b"S" + seed)
        assert self._read(1) == b"B", "the firmware did not boot in Renode"


@pytest.fixture(scope="module")
def renode():
    r = renode_path()
    assert r is not None, "Renode not installed (NEEDS_HUMAN #9): P4-FW-RENODE is not done"  # never a skip
    assert (r.parent / REPL_REL).exists(), "Renode's stock MPS2 TrustZone platform is missing"
    return r


@pytest.fixture(scope="module")
def elf():
    try:
        return fwb.build_an505()
    except fwb.BuildError as exc:
        pytest.fail(str(exc))


@pytest.fixture(scope="module")
def host_exe():
    try:
        return fwb.build()
    except fwb.BuildError as exc:
        pytest.fail(str(exc))


def long_script(f: Fw, seeds) -> list:
    out = []
    for s in seeds:
        out += run_script(f, s)
    return out


@pytest.mark.parametrize("block", range(2))
def test_transcript_is_identical_on_the_host_build_and_in_renode(renode, elf, host_exe, tmp_path, block):
    seeds = range(500 + 10 * block, 510 + 10 * block)  # the same seeds as the QEMU test, run back to back in one boot
    h = Fw(host_exe, tmp_path / "flash.bin", SEED)
    r = RenodeFw(renode, elf, SEED)
    try:
        th, tr = long_script(h, seeds), long_script(r, seeds)
    finally:
        h.close()
        r.close()
    assert th == tr
    assert h.emitted == r.emitted
    assert sum(1 for x in th if (x[0] == "flow" and x[2] == "ok") or (x[0] == "sign" and x[1] == "ok")) >= 30


def test_in_renode_the_button_is_the_only_approval_path_and_the_gate_accepts_once(renode, elf):
    r = RenodeFw(renode, elf, SEED)
    try:
        kid, pub, counter = r.info()
        assert kid == ap.key_id_of(pub) and counter == 0
        rnd = random.Random(31)
        for _ in range(300):
            k = rnd.random()
            if k < 0.4:
                r.call(2, req_payload(a_request(rnd)))
            elif k < 0.7:
                r.call(3, rnd.randint(0, 20).to_bytes(4, "big"))
            else:
                r.raw(bytes(rnd.getrandbits(8) for _ in range(rnd.randint(1, 300))) + b"\x00")
            if rnd.random() < 0.1:
                r.advance(rnd.choice([1000, 5000, 40000]))
        assert r.tokens() == [], "no press, no token"
        gate = ap.Gate({kid: pub}, session=SESSION, clock=Tick())
        action = {"kind": "files.write", "target": "host-r", "files": [{"path": "a", "pre": None, "content_b64": "eA=="}], "mkdirs": [],
                  "undo": {"kind": "files.restore", "target": "host-r", "files": [{"path": "a", "pre": None, "content_b64": None}],
                           "rmdirs": []}}
        st, seq, _ = r.request(gate.begin(action, ap.Class.WRITE).key_request())
        r.press(dwell=False)
        assert r.sign(seq) == ("no_press", None), "a press before the 1 s dwell"
        r.press()
        st, tok = r.sign(seq)
        assert st == "ok"
        assert gate.consume(tok, action, cls=ap.Class.WRITE).counter == 1
        with pytest.raises(ap.Rejected):
            gate.consume(tok, action, cls=ap.Class.WRITE)
    finally:
        r.close()


def test_saboteur_a_frozen_clock_in_renode_makes_the_transcripts_differ(renode, host_exe, tmp_path):
    src = tmp_path / "fw"
    shutil.copytree(FW, src, ignore=shutil.ignore_patterns("__pycache__"))
    hal = src / "an505" / "hal_an505.c"
    text = hal.read_text(encoding="utf-8")
    anchor = "void emu_tick_add(uint32_t ms) { tick += ms; }"
    assert text.count(anchor) == 1
    hal.write_text(text.replace(anchor, "void emu_tick_add(uint32_t ms) { (void)ms; }"), encoding="utf-8")
    try:
        bad = fwb.build_an505(src, tmp_path / "out")
    except fwb.BuildError as exc:
        pytest.fail(str(exc))
    h = Fw(host_exe, tmp_path / "h.bin", SEED)
    r = RenodeFw(renode, bad, SEED)
    try:
        assert long_script(h, range(500, 504)) != long_script(r, range(500, 504))
    finally:
        h.close()
        r.close()
