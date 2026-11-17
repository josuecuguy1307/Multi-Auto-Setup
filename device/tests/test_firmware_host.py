"""P4-FW-HOST: the key firmware core in C, host build with HAL mocks (SIMULATED: flash is a file, TRNG reads a file, button and tick are
injected by the harness; no TrustZone, RDP or real entropy). Spec: DECISIONS.md A-2 (advisor, Fable 5.2) on top of D-004 and D-007.

The harness talks to the host binary through tagged records on stdin; only `F` records reach the firmware's byte input:
    F <u16 len> <bytes>   bytes for mas_rx_byte          -> zero or more  R <u16 len> <frame bytes>, then E
    P                     a physical press (ISR)          -> k
    T <u32 ms>            advance the tick (64-bit on the key) -> k
    D                     dump the display                -> D <summary 32> <next counter u64>
The C firmware must match the Python reference model (src/mas_device/keymodel.py) byte for byte, and its tokens must satisfy approval.Gate."""

from __future__ import annotations

import binascii
import hashlib
import os
import random
import re
import shutil
import struct
import subprocess
from fractions import Fraction
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from mas_device import approval as ap
from mas_device.keymodel import KeyModel, KeyRefused, RequestHandle

ROOT = Path(__file__).resolve().parents[2]
FW = ROOT / "device" / "firmware"
import importlib.util as _ilu

_spec = _ilu.spec_from_file_location("fw_build", FW / "build.py")
fwb = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(fwb)

STATUS = {0: "ok", 1: "malformed", 2: "summary_not_printable", 3: "stale_request", 4: "no_press", 5: "press_window",
          6: "unknown_opcode", 7: "bad_frame", 8: "nv_fault"}
GET_INFO, REQUEST, SIGN = 1, 2, 3
PAGE = 8192
SEED = bytes(range(32))
TOKEN_LEN = ap.BODY_LEN + ap.SIG_LEN
SESSION = ap.session_id(b"\x03" * 32, b"firmware pairing")


# ---- an independent Python COBS and CRC, to check the C ones -----------------------------

def cobs_enc(data: bytes) -> bytes:
    out, block = bytearray(), bytearray()
    for b in data:
        if b == 0:
            out += bytes([len(block) + 1]) + block
            block = bytearray()
        else:
            block.append(b)
            if len(block) == 254:
                out += b"\xff" + block
                block = bytearray()
    out += bytes([len(block) + 1]) + block
    return bytes(out)


def cobs_dec(data: bytes) -> bytes | None:
    out, i = bytearray(), 0
    while i < len(data):
        code = data[i]
        end = i + code
        if code == 0 or end > len(data) or 0 in data[i + 1:end]:
            return None
        out += data[i + 1:end]
        i = end
        if code < 0xFF and i < len(data):
            out.append(0)
    return bytes(out)


def crc(data: bytes) -> bytes:
    return binascii.crc_hqx(data, 0xFFFF).to_bytes(2, "big")


def frame(op: int, payload: bytes = b"") -> bytes:
    body = bytes([op]) + payload
    return cobs_enc(body + crc(body)) + b"\x00"


def req_payload(r: ap.KeyRequest) -> bytes:
    return r.session_id + r.nonce + r.action_hash + r.undo_hash + r.summary + bytes([r.cls, r.press_window_s])


# ---- the harness ---------------------------------------------------------------------------

class Died(Exception):
    pass


DWELL_MS = 1000  # a press counts only after the summary has been on screen this long (D-017)


class Fw:
    def __init__(self, exe: Path, flash: Path, seed: bytes | None = SEED, **env):
        trng = flash.with_name(flash.name + ".trng")
        if seed is not None:
            trng.write_bytes(seed)
        e = {k: v for k, v in os.environ.items() if not k.startswith("MAS_")}
        e.update({"MAS_FLASH_FILE": str(flash), "MAS_TRNG_FILE": str(trng)})
        e.update({k: str(v) for k, v in env.items()})
        self.p = subprocess.Popen([str(exe)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=e)
        self.emitted: list[tuple[int, bytes]] = []  # every response frame seen, even from a process that then died

    def _read(self, n: int) -> bytes:
        data = self.p.stdout.read(n)
        if len(data) != n:
            raise Died(self.p.wait())
        return data

    def _write(self, data: bytes) -> None:
        try:
            self.p.stdin.write(data)
            self.p.stdin.flush()
        except OSError:
            raise Died(self.p.wait()) from None

    def raw(self, data: bytes) -> list[tuple[str, bytes]]:
        self._write(b"F" + len(data).to_bytes(2, "big") + data)
        out = []
        while True:
            tag = self._read(1)
            if tag == b"E":
                return out
            assert tag == b"R", tag
            n = int.from_bytes(self._read(2), "big")
            raw = self._read(n)
            for chunk in raw.split(b"\x00")[:-1]:
                body = cobs_dec(chunk)
                assert body is not None and len(body) >= 3 and crc(body[:-2]) == body[-2:], f"bad response frame {chunk.hex()}"
                out.append((STATUS[body[0]], body[1:-2]))
                self.emitted.append((body[0], body[1:-2]))

    def call(self, op: int, payload: bytes = b"") -> tuple[str, bytes]:
        r = self.raw(frame(op, payload))
        assert len(r) == 1, r
        return r[0]

    def info(self):
        st, p = self.call(GET_INFO)
        assert st == "ok", st
        assert p[0] == 1 and len(p) == 49
        return p[1:9], p[9:41], int.from_bytes(p[41:49], "big")

    def request(self, r: ap.KeyRequest):
        st, p = self.call(REQUEST, req_payload(r))
        if st != "ok":
            return st, None, None
        assert len(p) == 12
        return st, int.from_bytes(p[:4], "big"), int.from_bytes(p[4:], "big")

    def sign(self, seq: int):
        st, p = self.call(SIGN, seq.to_bytes(4, "big"))
        assert st != "ok" or len(p) == TOKEN_LEN
        return st, (p if st == "ok" else None)

    def _ack(self, data: bytes) -> None:
        self._write(data)
        assert self._read(1) == b"k"

    def press(self, dwell: bool = True):
        """A person's press, by default after reading the screen for the minimum time; dwell=False presses at the current tick."""
        if dwell:
            self.advance(DWELL_MS)
        self._ack(b"P")

    def advance(self, ms: int):
        self._ack(b"T" + ms.to_bytes(4, "big"))

    def display(self) -> str:
        self._write(b"D")
        assert self._read(1) == b"D"
        summary, nxt = self._read(32), int.from_bytes(self._read(8), "big")
        return "" if summary == bytes(32) else f"{summary.decode('ascii').rstrip()}  #{nxt}"

    def close(self) -> int:
        try:
            self.p.stdin.close()
        except OSError:
            pass
        self.p.stdout.read()
        return self.p.wait()

    def tokens(self) -> list[bytes]:
        return [p for st, p in self.emitted if st == 0 and len(p) == TOKEN_LEN]


@pytest.fixture(scope="module")
def exe():
    try:
        return fwb.build()
    except fwb.BuildError as exc:
        pytest.fail(str(exc))  # never a skip: no compiler is a FAIL


@pytest.fixture
def flash(tmp_path):
    return tmp_path / "flash.bin"


@pytest.fixture
def fw(exe, flash):
    f = Fw(exe, flash)
    yield f
    f.close()


def a_request(rnd: random.Random | None = None, *, summary: bytes | None = None, cls: int = 1, pw: int = 30) -> ap.KeyRequest:
    rnd = rnd or random.Random(1)
    rb = lambda n: bytes(rnd.getrandbits(8) for _ in range(n))
    if summary is None:
        summary = bytes(rnd.randint(0x20, 0x7E) for _ in range(32))
    return ap.KeyRequest(SESSION, rb(16), rb(32), rb(32), summary, cls, pw)


def approve(fw: Fw, r: ap.KeyRequest) -> bytes:
    st, seq, _ = fw.request(r)
    assert st == "ok"
    fw.press()
    st, tok = fw.sign(seq)
    assert st == "ok", st
    return tok


# ---- vectors ---------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def vectors(tmp_path_factory):
    try:
        return fwb.build_tool(FW / "tests" / "vectors.c", tmp_path_factory.mktemp("vec"), "vectors")
    except fwb.BuildError as exc:
        pytest.fail(str(exc))


def run_vec(vectors, mode: str, data: bytes) -> str:
    return subprocess.run([str(vectors), mode], input=data.hex() + "\n", capture_output=True, text=True).stdout.strip()


def test_sha256_crc_and_cobs_match_independent_implementations(vectors):
    rnd = random.Random(7)
    cases = [b"", b"\x00", b"abc", b"\x00" * 300, bytes(range(256)) * 3] + [bytes(rnd.getrandbits(8) for _ in range(rnd.randint(0, 600)))
                                                                          for _ in range(25)]
    for d in cases:
        assert run_vec(vectors, "sha256", d) == hashlib.sha256(d).hexdigest()
        assert run_vec(vectors, "crc16", d) == crc(d).hex()
        enc = run_vec(vectors, "cobs_enc", d)
        assert enc == cobs_enc(d).hex()
        assert run_vec(vectors, "cobs_dec", bytes.fromhex(enc)) == d.hex()
    for bad in (b"\x00", b"\x05\x01", b"\x03\x01\x00", b"\x02"):
        assert cobs_dec(bad) is None and run_vec(vectors, "cobs_dec", bad) == "ERR", bad.hex()


def test_monocypher_ed25519_is_rfc8032_as_in_cryptography(tmp_path):
    try:
        smoke = fwb.build_tool(FW / "tests" / "smoke_ed25519.c", tmp_path, "smoke")
    except fwb.BuildError as exc:
        pytest.fail(str(exc))
    pk, sig, ok = subprocess.run([str(smoke)], capture_output=True, text=True).stdout.split()
    k = Ed25519PrivateKey.from_private_bytes(bytes([7]) * 32)
    assert pk == k.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    assert sig == k.sign(bytes([1, 2, 3])).hex() and ok == "0"


def test_identity_comes_from_the_seed_and_key_id_is_sha256_of_pub(fw):
    kid, pub, counter = fw.info()
    expect = Ed25519PrivateKey.from_private_bytes(SEED).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    assert pub == expect and kid == ap.key_id_of(pub) and counter == 0


# ---- the contract with the reference model ---------------------------------------------------

def test_differential_against_the_python_reference_model(exe, tmp_path):
    for script in range(200):  # A-2: ~200 seeded scripts
        rnd = random.Random(1000 + script)
        seed = bytes(rnd.getrandbits(8) for _ in range(32))
        f = Fw(exe, tmp_path / f"f{script}.bin", seed)
        t = [0]
        folder = tmp_path / f"k{script}"
        folder.mkdir()
        km = KeyModel(folder, Ed25519PrivateKey.from_private_bytes(seed), 0, lambda: Fraction(t[0], 1000))
        handles: dict[int, RequestHandle] = {}
        log = []
        for step in range(30):
            op = rnd.choice(["req", "req", "press", "adv", "adv", "sign", "sign"])
            if op == "req":
                kind = rnd.random()
                if kind < 0.07:
                    r = a_request(rnd, summary=b"A" * 31 + bytes([rnd.choice([0x1F, 0x7F, 0x80, 0x00])]))
                elif kind < 0.12:
                    r = a_request(rnd, cls=rnd.choice([0, 8, 255]))
                elif kind < 0.16:
                    r = a_request(rnd, pw=0)
                else:
                    r = a_request(rnd, cls=rnd.randint(1, 7), pw=rnd.choice([1, 2, 5, 30, 31, 255]))
                try:
                    h, want = km.request(r), "ok"
                except KeyRefused as exc:
                    h, want = None, exc.code
                st, seq, nxt = f.request(r)
                log.append((op, want, st))
                assert st == want, log
                if h is not None:
                    assert seq == h.seq and nxt == km._counter + 1
                    handles[seq] = h
            elif op == "press":
                km.button.press()
                f.press(dwell=False)
            elif op == "adv":
                ms = rnd.choice([0, 1, 999, 1000, 5000, 29999, 30000, 30001, 45000])
                t[0] += ms
                f.advance(ms)
            else:
                seq = rnd.choice(list(handles) + [max(handles, default=0) + 1]) if handles else 1
                h = handles.get(seq) or RequestHandle(seq, 0, 0.0, None)
                try:
                    tok, want = km.sign(h), "ok"
                except KeyRefused as exc:
                    tok, want = None, exc.code
                st, ftok = f.sign(seq)
                log.append((op, want, st))
                assert st == want, log
                assert ftok == tok, "tokens differ byte for byte"
            assert f.display() == km.display_text(), log
        f.close()


# ---- rejections at the key -------------------------------------------------------------------

def test_no_press_no_signature_and_the_request_survives(fw):
    st, seq, _ = fw.request(a_request())
    assert fw.sign(seq) == ("no_press", None)
    fw.press()
    assert fw.sign(seq)[0] == "ok"


def check_press_before_request_does_not_count(fw):
    fw.press()
    fw.advance(5000)  # the press lies well before the request, so only the latch clear (not the dwell) can refuse it
    st, seq, _ = fw.request(a_request())
    fw.advance(DWELL_MS)
    assert fw.sign(seq) == ("no_press", None)


def test_press_before_the_request_does_not_count(fw):
    check_press_before_request_does_not_count(fw)


def test_press_window_is_strict_and_capped_at_thirty_seconds(fw):
    for pw, wait, want in [(30, 30000, "ok"), (30, 30001, "press_window"), (5, 5000, "ok"), (5, 5001, "press_window"),
                           (255, 30000, "ok"), (255, 30001, "press_window"), (1, 1000, "ok")]:
        st, seq, _ = fw.request(a_request(pw=pw))
        fw.advance(wait)
        fw.press(dwell=False)
        assert fw.sign(seq)[0] == want, (pw, wait)


def test_the_press_time_counts_not_the_sign_time(fw):
    st, seq, _ = fw.request(a_request())
    fw.advance(1000)
    fw.press(dwell=False)
    fw.advance(600000)
    assert fw.sign(seq)[0] == "ok"


def test_one_press_one_signature_and_seq_is_single_use(fw):
    st, seq, _ = fw.request(a_request())
    fw.press()
    fw.press()
    assert fw.sign(seq)[0] == "ok"
    assert fw.sign(seq)[0] == "stale_request"
    st, seq2, _ = fw.request(a_request())
    assert fw.sign(seq2) == ("no_press", None), "a second press before the request was latched once and consumed"


def test_a_newer_request_makes_the_older_one_stale(fw):
    st, s1, _ = fw.request(a_request(random.Random(1)))
    st, s2, _ = fw.request(a_request(random.Random(2)))
    fw.press()
    assert fw.sign(s1)[0] == "stale_request"
    assert fw.sign(s2)[0] == "ok"


def check_summary_printable(fw, bad):
    assert fw.request(a_request(summary=b"WRITE host" + bytes([bad]) + b" " * 21))[0] == "summary_not_printable"


@pytest.mark.parametrize("bad", [0x00, 0x1F, 0x7F, 0x80, 0xFF])
def test_summary_must_be_printable(fw, bad):
    check_summary_printable(fw, bad)


@pytest.mark.parametrize("cls,pw", [(0, 30), (8, 30), (255, 30), (1, 0)])
def test_class_and_window_ranges(fw, cls, pw):
    assert fw.request(a_request(cls=cls, pw=pw))[0] == "malformed"


@pytest.mark.parametrize("n", [0, 145, 147])
def test_request_length_is_exact(fw, n):
    assert fw.call(REQUEST, bytes(n))[0] == "malformed"


def test_a_payload_larger_than_the_frame_buffer_is_a_bad_frame(fw):
    assert fw.call(REQUEST, bytes(300))[0] == "bad_frame"
    assert fw.info()[2] == 0


def test_failed_request_changes_nothing(fw):
    st, seq, _ = fw.request(a_request())
    before = fw.display()
    fw.press()
    assert fw.request(a_request(cls=0))[0] == "malformed"
    assert fw.display() == before
    assert fw.sign(seq)[0] == "ok", "the latched press and the pending request survived the bad request"


def test_display_shows_exactly_the_signed_summary_and_counter(fw):
    r = a_request(summary=b"WRITE host-a 2f files.write".ljust(32))
    st, seq, nxt = fw.request(r)
    assert fw.display() == f"WRITE host-a 2f files.write  #{nxt}"
    fw.press()
    tok = fw.sign(seq)[1]
    assert tok[143:175] == r.summary and int.from_bytes(tok[23:31], "big") == nxt
    assert fw.display() == ""


# ---- the gate accepts firmware tokens once, and only unaltered and in time -------------------

class Tick:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def test_gate_accepts_a_firmware_token_once_and_rejects_altered_and_expired(fw):
    kid, pub, _ = fw.info()
    clock = Tick()
    gate = ap.Gate({kid: pub}, session=SESSION, clock=clock)
    action = {"kind": "files.write", "target": "host-x", "files": [{"path": "a", "pre": None, "content_b64": "eA=="}], "mkdirs": [],
              "undo": {"kind": "files.restore", "target": "host-x", "files": [{"path": "a", "pre": None, "content_b64": None}], "rmdirs": []}}
    tok = approve(fw, gate.begin(action, ap.Class.WRITE).key_request())
    for i in (0, 30, 100, 200, TOKEN_LEN - 1):
        bad = bytearray(tok)
        bad[i] ^= 1
        with pytest.raises(ap.Rejected):
            gate.consume(bytes(bad), action, cls=ap.Class.WRITE)
    assert gate.consume(tok, action, cls=ap.Class.WRITE).counter == 1
    with pytest.raises(ap.Rejected) as e:
        gate.consume(tok, action, cls=ap.Class.WRITE)
    assert e.value.code == "unknown_nonce"
    p2 = gate.begin(action, ap.Class.WRITE)
    tok2 = approve(fw, p2.key_request())
    clock.t += ap.MAX_TTL_S
    with pytest.raises(ap.Rejected) as e:
        gate.consume(tok2, action, cls=ap.Class.WRITE)
    assert e.value.code == "expired"


def test_the_same_request_twice_needs_two_presses_and_gets_two_counters(fw):
    r = a_request()
    t1, t2 = approve(fw, r), approve(fw, r)
    c1, c2 = int.from_bytes(t1[23:31], "big"), int.from_bytes(t2[23:31], "big")
    assert c2 == c1 + 1


# ---- frames and the opcode allowlist ---------------------------------------------------------

def test_bad_frames_are_answered_and_do_not_break_the_key(fw):
    good = frame(GET_INFO)
    bad_crc = bytearray(good)
    bad_crc[1] ^= 0x40
    cases = [bytes(bad_crc), b"\x05\x01\x00", b"\x00", b"\x01\x00", bytes(range(1, 256)) * 2 + b"\x00", b"\x02\x01\x00"]
    for c in cases:
        r = fw.raw(c)
        assert [s for s, _ in r] == ["bad_frame"], (c[:8].hex(), r)
    assert fw.info()[2] == 0


@pytest.mark.parametrize("op", [0] + list(range(4, 256)))
def test_unknown_opcodes_are_refused(fw, op):
    assert fw.call(op, b"\x01\x02")[0] == "unknown_opcode"


def test_payload_lengths_of_get_info_and_sign_are_exact(fw):
    assert fw.call(GET_INFO, b"\x00")[0] == "malformed"
    assert fw.call(SIGN, b"\x00\x00\x01")[0] == "malformed"
    assert fw.call(SIGN, b"\x00\x00\x00\x01\x00")[0] == "malformed"


def test_opcode_allowlist_in_source_and_flash_access_only_in_nv():
    h = (FW / "core" / "mas_core.h").read_text(encoding="utf-8")
    ops = {int(v, 0) for v in re.findall(r"#define\s+MAS_OP_\w+\s+(0x[0-9a-fA-F]+|\d+)", h)}
    assert ops == {1, 2, 3}
    for p in (FW / "core").glob("*.c"):
        uses = re.findall(r"hal_flash_(program|erase)\s*\(", p.read_text(encoding="utf-8"))
        assert not uses or p.name == "mas_nv.c", p.name


def test_random_frames_without_a_press_change_nothing_and_sign_nothing(exe, flash):
    f = Fw(exe, flash)
    f.info()
    before = flash.read_bytes()
    rnd = random.Random(11)
    for i in range(10000):  # A-2: 10k random frames
        k = rnd.random()
        if k < 0.3:
            f.call(REQUEST, req_payload(a_request(rnd)))
        elif k < 0.6:
            f.call(SIGN, rnd.randint(0, 40).to_bytes(4, "big"))
        elif k < 0.8:
            f.call(rnd.randint(0, 255), bytes(rnd.getrandbits(8) for _ in range(rnd.randint(0, 200))))
        else:
            f.raw(bytes(rnd.getrandbits(8) for _ in range(rnd.randint(0, 400))) + b"\x00")
        if i % 100 == 0:
            f.advance(rnd.randint(0, 40000))
    assert f.tokens() == []
    f.close()
    assert flash.read_bytes() == before


def test_every_token_follows_a_press_after_its_request_under_fuzz(exe, flash):
    f = Fw(exe, flash)
    rnd = random.Random(12)
    pending, pressed, tokens = None, False, 0
    for i in range(2000):
        k = rnd.random()
        if k < 0.25:
            st, seq, _ = f.request(a_request(rnd, cls=rnd.randint(0, 8), pw=rnd.choice([0, 1, 30])))
            if st == "ok":
                pending, pressed = seq, False
        elif k < 0.4:
            f.press()
            if pending is not None:
                pressed = True
        elif k < 0.7:
            seq = rnd.choice([pending or 0, rnd.randint(0, 50)])
            st, tok = f.sign(seq)
            if st == "ok":
                tokens += 1
                assert pressed and seq == pending, "a token without a press after its request"
            if st in ("ok", "press_window"):
                pending, pressed = None, False
        elif k < 0.85:
            f.advance(rnd.choice([0, 100, 20000, 31000]))
        else:
            f.raw(bytes(rnd.getrandbits(8) for _ in range(rnd.randint(1, 300))) + b"\x00")
    f.close()
    assert tokens > 20


def test_signing_never_touches_the_key_page(exe, flash):
    f = Fw(exe, flash)
    f.info()
    key_page = flash.read_bytes()[:PAGE]
    for i in range(20):
        approve(f, a_request(random.Random(i)))
    f.close()
    assert flash.read_bytes()[:PAGE] == key_page


# ---- the counter survives power cuts --------------------------------------------------------

def counter_record(c: int) -> bytes:
    return struct.pack("<QQ", c, c ^ 0xFFFFFFFFFFFFFFFF)


def boot(exe, flash) -> int:
    f = Fw(exe, flash)
    c = f.info()[2]
    f.close()
    return c


def test_counter_persists_across_reboots(exe, flash):
    f = Fw(exe, flash)
    for i in range(3):
        approve(f, a_request(random.Random(i)))
    f.close()
    f = Fw(exe, flash)
    assert f.info()[2] == 3
    assert int.from_bytes(approve(f, a_request())[23:31], "big") == 4
    f.close()


def _sweep(exe, base: bytes, flash: Path, steps: int, erase_modes=(0,)) -> int:
    """Cut power at every flash step of one approval; after each cut the counter never goes back and never repeats."""
    start = None
    cuts = 0
    for mode in erase_modes:
        for k in range(steps + 2):
            flash.write_bytes(base)
            n0 = boot(exe, flash)
            start = n0 if start is None else start
            f = Fw(exe, flash, MAS_CUT_AFTER=k, MAS_CUT_ERASE=mode)
            try:
                approve(f, a_request(random.Random(k)))
            except (Died, AssertionError):
                pass
            code = f.close()
            emitted = [int.from_bytes(t[23:31], "big") for t in f.tokens()]
            if code == 77:
                cuts += 1
            n1 = boot(exe, flash)
            assert n1 in (n0, n0 + 1), (mode, k, n0, n1)
            assert all(n1 >= c for c in emitted), (mode, k, "a released counter was lost", emitted, n1)
            f = Fw(exe, flash)
            nxt = int.from_bytes(approve(f, a_request(random.Random(99)))[23:31], "big")
            f.close()
            assert nxt > max(emitted + [n0]), (mode, k, nxt, emitted)
    return cuts


def test_power_cut_at_every_counter_write(exe, flash):
    f = Fw(exe, flash)
    approve(f, a_request())
    f.close()
    assert _sweep(exe, flash.read_bytes(), flash, 16) >= 16


def test_power_cut_during_page_rollover(exe, flash):
    boot(exe, flash)
    data = bytearray(flash.read_bytes())
    assert len(data) == 3 * PAGE
    for i in range(PAGE // 16):
        data[PAGE + 16 * i:PAGE + 16 * (i + 1)] = counter_record(i + 1)
    flash.write_bytes(bytes(data))
    assert boot(exe, flash) == PAGE // 16
    assert _sweep(exe, bytes(data), flash, 17, erase_modes=(0, 1, 2)) >= 3 * 16


def test_torn_records_and_garbage_are_ignored(exe, flash):
    boot(exe, flash)
    data = bytearray(flash.read_bytes())
    data[PAGE:PAGE + 16] = counter_record(5)
    data[PAGE + 16:PAGE + 32] = counter_record(9)[:8] + b"\xff" * 8           # torn: second half never written
    data[2 * PAGE:2 * PAGE + 16] = struct.pack("<QQ", 700, 12345)             # garbage: complement wrong
    flash.write_bytes(bytes(data))
    assert boot(exe, flash) == 5
    f = Fw(exe, flash)
    assert int.from_bytes(approve(f, a_request())[23:31], "big") == 6
    f.close()
    assert flash.read_bytes()[PAGE + 16:PAGE + 32] == counter_record(9)[:8] + b"\xff" * 8, "a non-erased slot is never programmed"


def test_flash_failure_releases_no_token(exe, flash):
    boot(exe, flash)
    f = Fw(exe, flash, MAS_FLASH_FAIL_AFTER=0)
    st, seq, _ = f.request(a_request())
    f.press()
    assert f.sign(seq) == ("nv_fault", None)
    f.close()
    assert f.tokens() == []


TEARING_SEED = bytes.fromhex("0661851bb1e31131145110929909f260541ea7949e01da850fd862b1581bf375")  # evaluator run 2: cut 9 -> CRC FF FF passes


@pytest.mark.parametrize("seed", [SEED, TEARING_SEED], ids=["seed0", "tearing"])
def test_power_cut_during_first_boot_key_generation(exe, tmp_path, seed):
    for k in range(0, 67):
        flash = tmp_path / f"fb{k}.bin"
        f = Fw(exe, flash, seed=seed, MAS_CUT_AFTER=k)
        try:
            f.info()
        except (Died, AssertionError):
            pass
        f.close()
        f = Fw(exe, flash, seed=seed)
        kid, pub, counter = f.info()
        assert kid == ap.key_id_of(pub) and counter == 0, k
        f.close()


def test_a_corrupted_key_page_is_never_silently_replaced_once_the_key_has_signed(exe, flash):
    f = Fw(exe, flash)
    approve(f, a_request())
    f.close()
    data = bytearray(flash.read_bytes())
    data[5] ^= 0x01
    flash.write_bytes(bytes(data))
    f = Fw(exe, flash)
    assert f.call(GET_INFO)[0] == "nv_fault"
    f.close()
    assert flash.read_bytes() == bytes(data)


# ---- saboteurs: a deliberately weakened firmware must fail these checks ---------------------

def _saboteur(tmp_path, old: str, new: str, file: str = "core/mas_core.c") -> Path:
    src = tmp_path / "fw"
    shutil.copytree(FW, src, ignore=shutil.ignore_patterns("__pycache__"))
    p = src / file
    text = p.read_text(encoding="utf-8")
    assert text.count(old) == 1, f"saboteur anchor not found once in {file}: {old!r}"
    p.write_text(text.replace(old, new), encoding="utf-8")
    try:
        return fwb.build(src, tmp_path / "out")
    except fwb.BuildError as exc:
        pytest.fail(str(exc))


def test_saboteur_no_latch_clear_on_request_is_caught(tmp_path):
    bad = _saboteur(tmp_path, "press_latched = 0; /* SABOTEUR-ANCHOR: latch clear */", "/* removed */")
    f = Fw(bad, tmp_path / "s.bin")
    with pytest.raises(AssertionError):
        check_press_before_request_does_not_count(f)  # the suite's own check fails on the weakened build
    f.close()


def test_saboteur_no_printable_check_is_caught(tmp_path):
    bad = _saboteur(tmp_path, "if (!printable(p + 112)) /* SABOTEUR-ANCHOR: printable */", "if (!printable(p + 112) && 0)")
    f = Fw(bad, tmp_path / "s.bin")
    with pytest.raises(AssertionError):
        check_summary_printable(f, 0x1F)  # the suite's own check fails on the weakened build
    f.close()


def test_saboteur_sign_before_commit_is_caught_by_the_power_cut_sweep(tmp_path):
    bad = _saboteur(tmp_path, "/* SABOTEUR-ANCHOR: commit-then-sign */ if (mas_nv_commit(next) != 0) { fault = 1; reply(ST_NV_FAULT, 0, 0); return; } send_token(next);",
                    "send_token(next); if (mas_nv_commit(next) != 0) { return; }")
    flash = tmp_path / "s.bin"
    f = Fw(bad, flash)
    approve(f, a_request())
    f.close()
    with pytest.raises(AssertionError, match="released counter was lost|nxt"):
        _sweep(bad, flash.read_bytes(), flash, 16)


# ---- evaluator run 1 (device/evals/evaluator/P4-FW-HOST.md) ----------------------------------------

def fill_page_a(exe, flash, upto: int):
    boot(exe, flash)
    data = bytearray(flash.read_bytes())
    for i in range(upto):
        data[PAGE + 16 * i:PAGE + 16 * (i + 1)] = counter_record(i + 1)
    flash.write_bytes(bytes(data))


def test_failed_commits_never_erase_the_page_holding_the_maximum(exe, flash):
    fill_page_a(exe, flash, PAGE // 16)
    f = Fw(exe, flash, MAS_FLASH_FAIL_AFTER=0)
    for k in range(PAGE // 16 + 2):
        st, seq, _ = f.request(a_request(random.Random(k)))
        if st == "ok":
            f.press()
            assert f.sign(seq)[0] != "ok"
    f.close()
    assert f.tokens() == []
    assert boot(exe, flash) == PAGE // 16, "a released counter came back after failed writes"
    f = Fw(exe, flash)
    assert int.from_bytes(approve(f, a_request())[23:31], "big") == PAGE // 16 + 1
    f.close()


def test_one_failed_commit_latches_a_fault_until_reboot(exe, flash):
    boot(exe, flash)
    f = Fw(exe, flash, MAS_FLASH_FAIL_AFTER=0)
    st, seq, _ = f.request(a_request())
    f.press()
    assert f.sign(seq)[0] == "nv_fault"
    assert f.call(REQUEST, req_payload(a_request()))[0] == "nv_fault"
    f.close()


def test_erased_key_page_after_signing_is_a_fault_not_a_new_identity(exe, flash):
    f = Fw(exe, flash)
    approve(f, a_request())
    f.close()
    data = bytearray(flash.read_bytes())
    data[:PAGE] = b"\xff" * PAGE
    flash.write_bytes(bytes(data))
    f = Fw(exe, flash, seed=bytes(range(100, 132)))
    assert f.call(GET_INFO)[0] == "nv_fault"
    f.close()
    assert flash.read_bytes() == bytes(data)


def test_program_that_reports_success_but_does_not_stick_releases_no_token(exe, flash):
    boot(exe, flash)
    f = Fw(exe, flash, MAS_FLASH_STUCK=1)
    st, seq, _ = f.request(a_request())
    f.press()
    assert f.sign(seq) == ("nv_fault", None)
    f.close()
    assert f.tokens() == []


@pytest.mark.parametrize("start,expect", [(2**64 - 3, [2**64 - 2]), (2**64 - 2, []), (2**64 - 1, [])])
def test_counter_never_wraps(exe, flash, start, expect):
    boot(exe, flash)
    data = bytearray(flash.read_bytes())
    data[PAGE:PAGE + 16] = counter_record(start)
    flash.write_bytes(bytes(data))
    got = []
    for k in range(3):
        f = Fw(exe, flash)
        st, seq, _ = f.request(a_request(random.Random(k)))
        if st == "ok":
            f.press()
            st, tok = f.sign(seq)
            if st == "ok":
                got.append(int.from_bytes(tok[23:31], "big"))
        f.close()
    assert got == expect


@pytest.mark.parametrize("byte", [0, 31, 32, 63])
def test_key_page_with_valid_crc_but_inconsistent_seed_and_pub_is_a_fault(exe, flash, byte):
    f = Fw(exe, flash)
    approve(f, a_request())  # the key has signed: its identity must never change
    f.close()
    data = bytearray(flash.read_bytes())
    data[byte] ^= 0x01
    data[64:66] = crc(bytes(data[:64]))
    flash.write_bytes(bytes(data))
    f = Fw(exe, flash)
    assert f.call(GET_INFO)[0] == "nv_fault"
    f.close()
    assert flash.read_bytes() == bytes(data)


def test_press_window_survives_the_32_bit_tick_wrap(fw):
    st, seq, _ = fw.request(a_request())
    fw.advance(0xFFFFFFFF)
    fw.advance(1001)
    fw.press(dwell=False)
    assert fw.sign(seq)[0] == "press_window"


# ---- evaluator run 2 (device/evals/evaluator/P4-FW-HOST.run2.md) ----------------------------------

def test_inconsistent_key_page_before_any_signature_is_regenerated_not_bricked(exe, flash):
    boot(exe, flash)
    data = bytearray(flash.read_bytes())
    data[40] ^= 0x01
    data[64:66] = crc(bytes(data[:64]))
    flash.write_bytes(bytes(data))
    f = Fw(exe, flash)
    kid, pub, counter = f.info()
    assert counter == 0 and kid == ap.key_id_of(pub)
    assert pub == Ed25519PrivateKey.from_private_bytes(SEED).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    f.close()


def fill(exe, flash, pages: dict):
    """pages: {page_index: [counter values for slots 0..]}; the key page comes from a normal first boot."""
    boot(exe, flash)
    data = bytearray(flash.read_bytes())
    for page, values in pages.items():
        for i, v in enumerate(values):
            data[page * PAGE + 16 * i:page * PAGE + 16 * (i + 1)] = counter_record(v)
    flash.write_bytes(bytes(data))
    return bytes(data)


def test_the_maximum_is_found_in_page_a_while_page_b_holds_records(exe, flash):
    fill(exe, flash, {1: [1025], 2: list(range(513, 1025))})
    assert boot(exe, flash) == 1025
    f = Fw(exe, flash)
    assert int.from_bytes(approve(f, a_request())[23:31], "big") == 1026
    f.close()


def test_power_cut_during_the_b_to_a_rollover(exe, flash):
    base = fill(exe, flash, {1: list(range(1, 513)), 2: list(range(513, 1025))})
    assert boot(exe, flash) == 1024
    assert _sweep(exe, base, flash, 17, erase_modes=(0, 1, 2)) >= 3 * 16


def test_saturated_counter_refuses_the_request(exe, flash):
    for start in (2**64 - 2, 2**64 - 1):
        fill(exe, flash, {1: [start]})
        f = Fw(exe, flash)
        assert f.call(REQUEST, req_payload(a_request()))[0] == "nv_fault"
        assert f.display() == ""
        f.close()


class Nv:
    """Drives mas_nv_* directly (device/firmware/tests/nv_tool.c) on a flash file."""

    def __init__(self, tool, flash, **env):
        e = {k: v for k, v in os.environ.items() if not k.startswith("MAS_")}
        e.update({"MAS_FLASH_FILE": str(flash)}, **{k: str(v) for k, v in env.items()})
        self.p = subprocess.Popen([str(tool)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=e, text=True)

    def __call__(self, line: str) -> str:
        self.p.stdin.write(line + "\n")
        self.p.stdin.flush()
        return self.p.stdout.readline().strip()

    def close(self):
        self.p.stdin.close()
        self.p.wait()


@pytest.fixture(scope="module")
def nvtool(tmp_path_factory):
    try:
        return fwb.build_tool(FW / "tests" / "nv_tool.c", tmp_path_factory.mktemp("nv"), "nv_tool",
                              extra_sources=("core/mas_nv.c", "host/hal_host.c"))
    except fwb.BuildError as exc:
        pytest.fail(str(exc))


def test_nv_refuses_counters_that_do_not_increase(exe, nvtool, flash):
    fill(exe, flash, {1: [1, 2, 3]})
    nv = Nv(nvtool, flash)
    assert nv("init") == "0 3"
    assert nv("commit 3") == "-1" and nv("commit 2") == "-1" and nv("commit 0") == "-1"
    assert nv("commit 4") == "0" and nv("counter") == "4"
    nv.close()


def test_nv_latches_after_one_failed_commit(exe, nvtool, flash):
    fill(exe, flash, {1: [1]})
    nv = Nv(nvtool, flash, MAS_FLASH_FAIL_CALLS=1)  # only the first program call fails
    assert nv("init") == "0 1"
    assert nv("commit 2") == "-1"
    assert nv("commit 2") == "-1" and nv("commit 3") == "-1", "the NV module itself must stay closed until reboot"
    nv.close()
    nv = Nv(nvtool, flash)
    assert nv("init").startswith("0 ") and nv("commit 2") == "0"
    nv.close()


def test_nv_verifies_the_erase_before_using_the_new_page(exe, nvtool, flash):
    data = bytearray(fill(exe, flash, {1: list(range(1, 513))}))
    data[2 * PAGE + 16 * 5:2 * PAGE + 16 * 6] = b"\x00" * 16  # page B not erased: garbage beyond slot 0
    flash.write_bytes(bytes(data))
    nv = Nv(nvtool, flash, MAS_ERASE_STUCK=1)
    assert nv("init") == "0 512"
    assert nv("commit 513") == "-1"
    nv.close()
    assert boot(exe, flash) == 512


def test_nv_never_programs_a_slot_that_is_not_erased(exe, nvtool, flash):
    fill(exe, flash, {1: [1, 2]})
    rec = bytearray(counter_record(3))
    poke = bytearray(b"\xff" * 16)
    k, b = next((k, b) for k in range(16) for b in range(8) if not rec[k] >> b & 1)
    poke[k] &= ~(1 << b) & 0xFF  # not erased, yet programming the record over it would read back correctly
    nv = Nv(nvtool, flash)
    assert nv("init") == "0 2"
    assert nv(f"poke 1 32 {poke.hex()}") == "0"
    assert nv("commit 3") == "-1"
    nv.close()


# ---- evaluator run 3 (device/evals/evaluator/P4-FW-HOST.run3.md) ----------------------------------

def test_a_press_counts_only_after_the_summary_was_on_screen_for_a_second(fw):
    st, seq, _ = fw.request(a_request())
    fw.press(dwell=False)
    assert fw.sign(seq) == ("no_press", None), "a press at 0 ms: nobody read the screen"
    fw.advance(DWELL_MS - 1)
    fw.press(dwell=False)
    assert fw.sign(seq) == ("no_press", None)
    fw.advance(1)
    fw.press(dwell=False)
    assert fw.sign(seq)[0] == "ok"


def test_bait_and_switch_just_before_the_press_is_refused(fw):
    st, s1, _ = fw.request(a_request(random.Random(1), summary=b"READ  benign status".ljust(32)))
    fw.advance(4000)
    st, s2, _ = fw.request(a_request(random.Random(2), summary=b"WRITE rm -rf host-a".ljust(32)))
    fw.press(dwell=False)
    assert fw.sign(s2) == ("no_press", None)


def test_a_press_while_the_display_is_being_drawn_does_not_count(exe, flash):
    f = Fw(exe, flash, MAS_PRESS_IN_DISPLAY=1)  # the mock's display presses the button once while it draws the next summary
    st, seq, _ = f.request(a_request())
    f.advance(DWELL_MS)
    assert f.sign(seq) == ("no_press", None)
    f.close()


def test_trng_failure_at_first_boot_is_a_fault_with_no_token(exe, flash):
    f = Fw(exe, flash, seed=None)
    assert f.call(GET_INFO)[0] == "nv_fault"
    assert f.call(REQUEST, req_payload(a_request()))[0] == "nv_fault"
    f.close()
    assert f.tokens() == []


@pytest.mark.parametrize("mode", [{"MAS_FLASH_FAIL_CALLS": 1}, {"MAS_FLASH_STUCK": 1}], ids=["write_fails", "write_does_not_stick"])
def test_first_boot_key_write_failure_is_a_fault_with_no_token(exe, flash, mode):
    f = Fw(exe, flash, **mode)
    assert f.call(GET_INFO)[0] == "nv_fault"
    st = f.call(REQUEST, req_payload(a_request()))[0]
    assert st == "nv_fault"
    f.close()
    assert f.tokens() == []


# ---- evaluator run 4 (device/evals/evaluator/P4-FW-HOST.run4.md): a redraw takes time on a real screen ----------

def check_press_at_the_end_of_a_slow_redraw_is_forgotten(fw):
    """Run with MAS_DISPLAY_MS=1500 and MAS_PRESS_IN_DISPLAY=1: the button is pressed as the 1.5 s redraw finishes."""
    st, seq, _ = fw.request(a_request())
    assert fw.sign(seq) == ("no_press", None)


def check_the_dwell_starts_when_the_summary_is_visible(fw):
    """Run with MAS_DISPLAY_MS=3000: a press 100 ms after a 3 s redraw is too soon."""
    st, seq, _ = fw.request(a_request())
    fw.advance(100)
    fw.press(dwell=False)
    assert fw.sign(seq) == ("no_press", None)


def test_a_press_at_the_end_of_a_slow_redraw_is_forgotten(exe, flash):
    f = Fw(exe, flash, MAS_DISPLAY_MS=1500, MAS_PRESS_IN_DISPLAY=1)
    check_press_at_the_end_of_a_slow_redraw_is_forgotten(f)
    f.close()


def test_the_dwell_starts_when_the_summary_is_visible(exe, flash):
    f = Fw(exe, flash, MAS_DISPLAY_MS=3000)
    check_the_dwell_starts_when_the_summary_is_visible(f)
    f.press()
    assert f.sign(2)[0] in ("stale_request", "ok")
    f.close()


def test_a_press_between_the_latch_clear_and_the_stamp_leaves_the_request_pending(exe, flash):
    f = Fw(exe, flash, MAS_PRESS_IN_IRQ_ENABLE=1)  # the button fires as interrupts come back on, then 5 ms pass before the stamp
    st, seq, _ = f.request(a_request())
    assert f.sign(seq) == ("no_press", None), "a press before the stamp is no press, and the request is not destroyed"
    f.press()
    assert f.sign(seq)[0] == "ok"
    f.close()


BLOCK = '    hal_display(p + SUMMARY_AT, mas_nv_counter() + 1); /* first the screen; a press during the redraw is forgotten just below */\n    seq++;\n    hal_irq_disable();\n    press_latched = 0; /* SABOTEUR-ANCHOR: latch clear */\n    hal_irq_enable();\n    pending.valid = 1;\n    pending.seq = seq;\n    pending.t_req = hal_tick_ms(); /* stamped after the display returned: the dwell and the window start when the summary is visible */\n'


def test_saboteur_display_after_the_stamp_is_caught(tmp_path):
    bad = _saboteur(tmp_path, BLOCK, '    seq++;\n    hal_irq_disable();\n    press_latched = 0; /* SABOTEUR-ANCHOR: latch clear */\n    hal_irq_enable();\n    pending.valid = 1;\n    pending.seq = seq;\n    pending.t_req = hal_tick_ms();\n    hal_display(p + SUMMARY_AT, mas_nv_counter() + 1);\n')
    f = Fw(bad, tmp_path / "s.bin", MAS_DISPLAY_MS=1500, MAS_PRESS_IN_DISPLAY=1)
    with pytest.raises(AssertionError):
        check_press_at_the_end_of_a_slow_redraw_is_forgotten(f)
    f.close()


def test_saboteur_stamp_before_the_display_is_caught(tmp_path):
    bad = _saboteur(tmp_path, BLOCK, '    pending.t_req = hal_tick_ms();\n    hal_display(p + SUMMARY_AT, mas_nv_counter() + 1);\n    seq++;\n    hal_irq_disable();\n    press_latched = 0; /* SABOTEUR-ANCHOR: latch clear */\n    hal_irq_enable();\n    pending.valid = 1;\n    pending.seq = seq;\n')
    f = Fw(bad, tmp_path / "s.bin", MAS_DISPLAY_MS=3000)
    with pytest.raises(AssertionError):
        check_the_dwell_starts_when_the_summary_is_visible(f)
    f.close()
