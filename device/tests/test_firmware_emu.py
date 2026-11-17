"""P4-FW-EMU (EMULATED): the same firmware core, cross-compiled for a Cortex-M33 and run in QEMU's mps2-an505 machine, behaves exactly like
the host build: identical framed transcripts for the same scripts, the button as the only approval path, tokens the gate accepts once.

What emulation covers: the core's logic and its Cortex-M33 compile (Thumb-2, no heap, freestanding), framing over a CMSDK UART. What it
does not: secure boot, RDP, TrustZone partitioning, real flash timing and power cuts, the real button and display, TRNG quality. The tick
and the button are driven by the harness over the same UART (on the board they are SysTick and a GPIO interrupt); flash is RAM for the
length of one emulator run. Spec: DECISIONS.md D-007, A-2, D-018."""

from __future__ import annotations

import os
import random
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

from mas_device import approval as ap
from test_firmware_host import (DWELL_MS, FW, GET_INFO, REQUEST, SESSION, SIGN, TOKEN_LEN, Died, Fw, a_request, frame, fwb,
                                req_payload)
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

SEED = bytes(range(32))


def qemu_path() -> Path:
    for c in (os.environ.get("MAS_QEMU", ""), r"C:\Program Files\qemu\qemu-system-arm.exe", shutil.which("qemu-system-arm") or ""):
        if c and Path(c).exists():
            return Path(c)
    pytest.fail("qemu-system-arm not found: install QEMU (NEEDS_HUMAN) or set MAS_QEMU")  # never a skip


class EmuFw(Fw):
    """The same harness, over QEMU's emulated UART0 (a TCP socket on 127.0.0.1) instead of a host process's pipes."""

    def __init__(self, qemu: Path, elf: Path, seed: bytes = SEED):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.q = subprocess.Popen([str(qemu), "-M", "mps2-an505", "-nographic", "-monitor", "none", "-kernel", str(elf),
                                   "-serial", f"tcp:127.0.0.1:{port},server=on,wait=on"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.s = None
        for _ in range(200):
            try:
                self.s = socket.create_connection(("127.0.0.1", port), timeout=30)
                break
            except OSError:
                time.sleep(0.05)
        if self.s is None:
            self.q.kill()
            raise Died("QEMU did not open its UART")
        self.emitted = []
        self.s.sendall(b"S" + seed)
        assert self._read(1) == b"B", "the emulated firmware did not boot"

    def _write(self, data: bytes) -> None:
        self.s.sendall(data)

    def _read(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            try:
                chunk = self.s.recv(n - len(buf))
            except OSError:
                chunk = b""
            if not chunk:
                raise Died(-1)
            buf += chunk
        return buf

    def close(self) -> int:
        try:
            self.s.close()
        finally:
            self.q.kill()
            self.q.wait()
        return 0


@pytest.fixture(scope="module")
def qemu():
    return qemu_path()


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


@pytest.fixture
def emu(qemu, elf):
    f = EmuFw(qemu, elf)
    yield f
    f.close()


def run_script(f: Fw, seed: int, steps: int = 40) -> list:
    """A deterministic random script of requests, presses, ticks, signs, junk frames and display reads; returns everything observed."""
    rnd = random.Random(seed)
    out, seqs = [], []
    for _ in range(steps):
        k = rnd.random()
        if k < 0.3:  # a whole approval: request, the person reads and presses (around the dwell and the window), sign
            st, p = f.call(REQUEST, req_payload(a_request(rnd, cls=rnd.randint(1, 7), pw=rnd.choice([5, 30]))))
            out.append(("req", st, p.hex()))
            if st == "ok":
                seq = int.from_bytes(p[:4], "big")
                seqs.append(seq)
                wait = rnd.choice([0, 999, 1000, 1500, 4000, 5001, 30000, 30001])
                f.advance(wait)
                f.press(dwell=False)
                st, p = f.call(SIGN, seq.to_bytes(4, "big"))
                out.append(("flow", wait, st, p.hex()))
            continue
        k = rnd.random()
        if k < 0.25:
            r = a_request(rnd, cls=rnd.choice([1, 1, 1, 2, 7, 0, 8]), pw=rnd.choice([1, 5, 30, 255, 0]))
            if rnd.random() < 0.1:
                r = r._replace(summary=b"X" * 31 + bytes([rnd.choice([0x1F, 0x7F, 0x80])]))
            st, p = f.call(REQUEST, req_payload(r))
            out.append(("req", st, p.hex()))
            if st == "ok":
                seqs.append(int.from_bytes(p[:4], "big"))
        elif k < 0.4:
            f.press(dwell=False)
            out.append(("press",))
        elif k < 0.55:
            ms = rnd.choice([0, 1, 500, 999, 1000, 1001, 29000, 30001, 40000])
            f.advance(ms)
            out.append(("tick", ms))
        elif k < 0.8:
            seq = rnd.choice(seqs + [0, 99]) if seqs else 1
            st, p = f.call(SIGN, seq.to_bytes(4, "big"))
            out.append(("sign", st, p.hex()))
        elif k < 0.9:
            out.append(("junk", f.raw(bytes(rnd.getrandbits(8) for _ in range(rnd.randint(1, 200))) + b"\x00")))
        else:
            out.append(("info", f.call(GET_INFO)))
        out.append(("display", f.display()))
    return out


# ---- the emulated build is the same firmware -------------------------------------------------

def test_the_emulated_build_compiles_the_same_core_and_the_port_adds_no_logic():
    assert fwb.CORE == ["core/mas_core.c", "core/mas_frame.c", "core/mas_nv.c", "core/sha256.c"]
    for p in (FW / "an505").glob("*.c"):
        text = p.read_text(encoding="utf-8")
        assert "crypto_ed25519" not in text and "mas_nv_commit" not in text, p.name
        assert not re.search(r"\bvoid\s+mas_(rx_byte|button_isr|init)\s*\(", text), p.name


def test_emulated_identity_comes_from_the_seed(emu):
    kid, pub, counter = emu.info()
    assert pub == Ed25519PrivateKey.from_private_bytes(SEED).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    assert kid == ap.key_id_of(pub) and counter == 0


@pytest.mark.parametrize("script", range(20))
def test_transcript_is_identical_on_the_host_build_and_in_qemu(qemu, elf, host_exe, tmp_path, script):
    h = Fw(host_exe, tmp_path / "flash.bin", SEED)
    e = EmuFw(qemu, elf, SEED)
    try:
        th, te = run_script(h, 500 + script), run_script(e, 500 + script)
    finally:
        h.close()
        e.close()
    assert th == te
    assert h.emitted == e.emitted


# ---- the button is the only approval path ---------------------------------------------------

def test_emulated_rejections(emu):
    st, seq, _ = emu.request(a_request())
    assert emu.sign(seq) == ("no_press", None)
    emu.press(dwell=False)
    assert emu.sign(seq) == ("no_press", None), "a press before the dwell"
    emu.press()
    assert emu.sign(seq)[0] == "ok"
    assert emu.sign(seq)[0] == "stale_request"
    emu.press()
    emu.advance(5000)
    st, seq, _ = emu.request(a_request(random.Random(3)))
    emu.advance(DWELL_MS)
    assert emu.sign(seq) == ("no_press", None), "a press before the request"
    emu.advance(30001)
    emu.press(dwell=False)
    assert emu.sign(seq)[0] == "press_window"
    for op in (0, 4, 0x7F, 0xFF):
        assert emu.call(op)[0] == "unknown_opcode"
    assert [s for s, _ in emu.raw(b"\x05\x01\x00")] == ["bad_frame"]


def test_random_frames_without_a_press_never_sign_in_qemu(emu):
    rnd = random.Random(21)
    for _ in range(400):
        k = rnd.random()
        if k < 0.4:
            emu.call(REQUEST, req_payload(a_request(rnd)))
        elif k < 0.7:
            emu.call(SIGN, rnd.randint(0, 20).to_bytes(4, "big"))
        else:
            emu.raw(bytes(rnd.getrandbits(8) for _ in range(rnd.randint(1, 300))) + b"\x00")
        if rnd.random() < 0.1:
            emu.advance(rnd.choice([1000, 5000, 40000]))
    assert emu.tokens() == []


class Tick:
    def __init__(self):
        self.t = 50.0

    def __call__(self):
        return self.t


def test_the_gate_accepts_an_emulated_token_once(emu):
    kid, pub, _ = emu.info()
    gate = ap.Gate({kid: pub}, session=SESSION, clock=Tick())
    action = {"kind": "files.write", "target": "host-q", "files": [{"path": "a", "pre": None, "content_b64": "eA=="}], "mkdirs": [],
              "undo": {"kind": "files.restore", "target": "host-q", "files": [{"path": "a", "pre": None, "content_b64": None}], "rmdirs": []}}
    st, seq, _ = emu.request(gate.begin(action, ap.Class.WRITE).key_request())
    emu.press()
    st, tok = emu.sign(seq)
    assert st == "ok" and len(tok) == TOKEN_LEN
    assert gate.consume(tok, action, cls=ap.Class.WRITE).counter == 1
    with pytest.raises(ap.Rejected):
        gate.consume(tok, action, cls=ap.Class.WRITE)


def test_saboteur_a_broken_emulated_clock_makes_the_transcripts_differ(qemu, host_exe, tmp_path):
    """The comparison has teeth: an emulated build whose tick never moves must disagree with the host build on some script."""
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
    differs = False
    for script in range(6):
        h = Fw(host_exe, tmp_path / f"h{script}.bin", SEED)
        e = EmuFw(qemu, bad, SEED)
        try:
            differs |= run_script(h, 500 + script) != run_script(e, 500 + script)
        finally:
            h.close()
            e.close()
    assert differs


def test_scripts_exercise_signatures(qemu, elf):
    """Across the scripts used above, real tokens are produced (the transcripts compare signatures, not only refusals)."""
    oks = 0
    for script in range(20):
        e = EmuFw(qemu, elf, SEED)
        try:
            oks += sum(1 for x in run_script(e, 500 + script) if (x[0] == "sign" and x[1] == "ok") or (x[0] == "flow" and x[2] == "ok"))
        finally:
            e.close()
    assert oks >= 60, oks
