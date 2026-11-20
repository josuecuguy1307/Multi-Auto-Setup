"""P2-PAIRING: the gate accepts only the paired key_id; MAS chat_choice approvals are absent from the device build; the host owner's
launch is the session consent (DECISIONS D-003, D-004, D-021, D-032).

SIMULATED: the key is the Python key model (or a bare Ed25519 key for the attacker); the gate runs as its own process, as in P2-GUARDRAILS.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import mas_device
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from mas_device import approval as ap
from mas_device import pairing as pr
from mas_device.keymodel import KeyModel

SRC = Path(mas_device.__file__).resolve().parents[1]  # the tree under test: the oracle points PYTHONPATH at its own src
PKG = SRC / "mas_device"
TIMEOUT = 60
TARGET = "pairing-test-host"


class Tick:
    def __init__(self, t: float = 100.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def raw_pub(priv: Ed25519PrivateKey) -> bytes:
    return priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def request_of(kr: dict) -> ap.KeyRequest:
    return ap.KeyRequest(*(bytes.fromhex(kr[n]) for n in ("session_id", "nonce", "action_hash", "undo_hash", "summary")),
                         kr["cls"], kr["press_window_s"])


def approve(key: KeyModel, clock: Tick, kr: dict) -> str:
    """The owner reads the paired key's screen and presses its button once."""
    h = key.request(request_of(kr))
    assert key.display_text()
    clock.t += 2
    key.button.press()
    return key.sign(h).hex()


def forged(priv: Ed25519PrivateKey, key_id: bytes, kr: dict, counter: int = 1) -> str:
    """A token made by a key that is not the paired one (a perfectly valid Ed25519 signature of a well-formed body)."""
    body = ap.pack_body(key_id, counter, request_of(kr))
    return (body + priv.sign(body)).hex()


def records(state: Path) -> list[dict]:
    p = state / "audit.log"
    return [json.loads(line) for line in p.read_bytes().splitlines() if line] if p.exists() else []


class Gated:
    """The gate daemon as the model's side sees it: stdin/stdout JSON lines. Its stderr is what the host's owner sees."""

    def __init__(self, argv: list[str], cwd: Path) -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "PYTEST_"))}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        code = "import sys; sys.path.insert(0, sys.argv[1]); import mas_device.gated as g; sys.exit(g.main(sys.argv[2:]))"
        self.p = subprocess.Popen([sys.executable, "-I", "-B", "-c", code, str(SRC), *argv], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=cwd)
        self.n = 0

    def call(self, method: str, **params) -> dict:
        self.n += 1
        self.p.stdin.write(json.dumps({"id": self.n, "method": method, "params": params}).encode() + b"\n")
        self.p.stdin.flush()
        line = self.p.stdout.readline()
        assert line, "the daemon died: " + self.p.stderr.read().decode("utf-8", "replace")[-2000:]
        return json.loads(line)

    def close(self) -> tuple[int, str]:
        try:
            self.p.stdin.close()
        except OSError:
            pass
        rc = self.p.wait(timeout=TIMEOUT)
        err = self.p.stderr.read().decode("utf-8", "replace")
        self.p.stdout.close()
        self.p.stderr.close()
        return rc, err


@pytest.fixture
def world(tmp_path):
    root = (tmp_path / "host").resolve()
    (root / "etc").mkdir(parents=True)
    (root / "etc" / "app.conf").write_bytes(b"port=1\n")
    state = (tmp_path / "device-state").resolve()
    state.mkdir()
    medium = (tmp_path / "medium").resolve()
    medium.mkdir()
    clock = Tick()
    key = KeyModel.create(tmp_path / "key", clock=clock)
    record = pr.provision(medium, key.public_key)
    started: list[Gated] = []

    def start(argv: list[str] | None = None, pairing: Path | None = None) -> Gated:
        if argv is None:
            argv = ["--root", str(root), "--state", str(state), "--pairing", str(pairing or record), "--target", TARGET]
        g = Gated(argv, tmp_path)
        started.append(g)
        return g

    w = type("World", (), {})()
    w.root, w.state, w.medium, w.clock, w.key, w.record, w.start, w.tmp = root, state, medium, clock, key, record, start, tmp_path
    yield w
    for g in started:
        if g.p.poll() is None:
            g.close()


def plan_and_begin(g: Gated, content: bytes = b"port=2\n") -> tuple[dict, dict]:
    r = g.call("plan_write", files={"etc/app.conf": base64.b64encode(content).decode()})
    assert r["ok"], r
    action = r["result"]["action"]
    r = g.call("begin", action=action)
    assert r["ok"], r
    return action, r["result"]["key_request"]


# ---- the pairing record ---------------------------------------------------------------------------------------------------------------

def test_record_round_trips_and_binds_key_and_medium(tmp_path):
    priv = Ed25519PrivateKey.generate()
    pub = raw_pub(priv)
    path = pr.provision(tmp_path, pub)
    assert path == tmp_path / pr.RECORD_NAME
    p = pr.load(path)
    assert p.key_pub == pub and p.key_id == ap.key_id_of(pub) and len(p.medium_id) == 16
    assert p.transcript == hashlib.sha256(pr.PAIR_DOMAIN + p.medium_id + pub).digest()
    assert path.read_bytes() == pr.record_bytes(pub, p.medium_id)
    doc = json.loads(path.read_bytes())
    assert doc == {"v": 1, "key_id": p.key_id.hex(), "key_pub": pub.hex(), "medium_id": p.medium_id.hex()}
    # another medium paired with the same key has another transcript: a session on one medium never matches the other
    other = tmp_path / "other"
    other.mkdir()
    q = pr.load(pr.provision(other, pub))
    assert q.medium_id != p.medium_id and q.transcript != p.transcript


def test_provisioning_never_overwrites_a_pairing(tmp_path):
    a, b = raw_pub(Ed25519PrivateKey.generate()), raw_pub(Ed25519PrivateKey.generate())
    path = pr.provision(tmp_path, a)
    before = path.read_bytes()
    with pytest.raises(pr.PairingError) as e:
        pr.provision(tmp_path, b)
    assert e.value.code == "already_paired"
    assert path.read_bytes() == before
    for bad in (b"\x00" * 31, b"\x00" * 33, "ab" * 16, None):
        with pytest.raises(pr.PairingError):
            pr.provision(tmp_path / "x", bad)


def _variants(pub: bytes, mid: bytes, other_pub: bytes) -> dict[str, bytes]:
    kid = ap.key_id_of(pub).hex()
    good = {"v": 1, "key_id": kid, "key_pub": pub.hex(), "medium_id": mid.hex()}

    def dump(d) -> bytes:
        return (json.dumps(d, sort_keys=True, separators=(",", ":")) + "\n").encode()

    canonical = pr.record_bytes(pub, mid)
    v = {
        "key_id_of_another_key": dump({**good, "key_id": ap.key_id_of(other_pub).hex()}),
        "key_pub_swapped_key_id_kept": dump({**good, "key_pub": other_pub.hex()}),
        "extra_field": dump({**good, "extra": 1}),
        "second_key": dump({**good, "key_pub2": other_pub.hex()}),
        "keys_list": dump({"v": 1, "keys": [good, good]}),
        "missing_medium": dump({k: x for k, x in good.items() if k != "medium_id"}),
        "missing_key_id": dump({k: x for k, x in good.items() if k != "key_id"}),
        "version_2": dump({**good, "v": 2}),
        "version_true": dump({**good, "v": True}),
        "version_string": dump({**good, "v": "1"}),
        "uppercase_hex": dump({**good, "key_pub": pub.hex().upper()}),
        "short_key": dump({**good, "key_pub": pub.hex()[:-2]}),
        "short_medium": dump({**good, "medium_id": mid.hex()[:-2]}),
        "duplicate_key": canonical.replace(b'"v":1', b'"v":1,"v":1'),
        "bom": b"\xef\xbb\xbf" + canonical,
        "crlf": canonical.replace(b"\n", b"\r\n"),
        "no_newline": canonical.rstrip(b"\n"),
        "trailing_data": canonical + b"{}\n",
        "spaces": dump(good).replace(b",", b", "),
        "not_json": b"key=" + pub.hex().encode() + b"\n",
        "array": b"[]\n",
        "empty": b"",
        "huge": canonical.rstrip(b"\n")[:-1] + b',"pad":"' + b"a" * 5000 + b'"}\n',
        "non_ascii": canonical.replace(b'"v":1', b'"v":1,"n":"\xc3\xa9"'),
    }
    return v


def test_record_refuses_anything_but_the_exact_form(tmp_path):
    pub, other = raw_pub(Ed25519PrivateKey.generate()), raw_pub(Ed25519PrivateKey.generate())
    mid = bytes(range(16))
    ok = tmp_path / "ok.json"
    ok.write_bytes(pr.record_bytes(pub, mid))
    assert pr.load(ok).key_pub == pub
    for name, data in _variants(pub, mid, other).items():
        p = tmp_path / f"{name}.json"
        p.write_bytes(data)
        with pytest.raises(pr.PairingError, match=r".*"):
            pr.load(p)
    with pytest.raises(pr.PairingError) as e:
        pr.load(tmp_path / "absent.json")
    assert e.value.code == "pairing_missing"
    with pytest.raises(pr.PairingError):
        pr.load(tmp_path)  # a folder is not a record


def test_a_key_id_that_does_not_match_its_key_is_refused_by_name(tmp_path):
    pub, other = raw_pub(Ed25519PrivateKey.generate()), raw_pub(Ed25519PrivateKey.generate())
    p = tmp_path / "r.json"
    p.write_bytes(_variants(pub, bytes(16), other)["key_id_of_another_key"])
    with pytest.raises(pr.PairingError) as e:
        pr.load(p)
    assert e.value.code == "key_id_mismatch"


def test_session_id_binds_the_pairing_and_is_fresh_each_launch(tmp_path):
    pub = raw_pub(Ed25519PrivateKey.generate())
    p = pr.load(pr.provision(tmp_path, pub))
    s1_pub, s1 = pr.new_session(p)
    s2_pub, s2 = pr.new_session(p)
    assert len(s1_pub) == 32 and s1 == ap.session_id(s1_pub, p.transcript)
    assert s1_pub != s2_pub and s1 != s2


# ---- the gate accepts only the paired key ---------------------------------------------------------------------------------------------

def test_the_paired_key_approves_and_any_other_key_is_refused(world):
    g = world.start()
    action, kr = plan_and_begin(g)
    stranger = Edsa = Ed25519PrivateKey.generate()
    paired_kid = ap.key_id_of(world.key.public_key)
    # a stranger key, honest about its own id: not the paired key
    r = g.call("execute", token=forged(stranger, ap.key_id_of(raw_pub(stranger)), kr), action=action)
    assert not r["ok"] and r["error"]["code"] == "unknown_key", r
    # a stranger key claiming the paired key's id: its signature does not verify under the pinned key
    r = g.call("execute", token=forged(Edsa, paired_kid, kr), action=action)
    assert not r["ok"] and r["error"]["code"] == "bad_signature", r
    assert (world.root / "etc" / "app.conf").read_bytes() == b"port=1\n"
    # neither refusal burned the request: the owner's press on the paired key still writes
    r = g.call("execute", token=approve(world.key, world.clock, kr), action=action)
    assert r["ok"], r
    assert (world.root / "etc" / "app.conf").read_bytes() == b"port=2\n"
    rc, _ = g.close()
    assert rc == 0
    codes = [x["payload"]["code"] for x in records(world.state) if x["type"] == "rpc.refused"]
    assert codes.count("unknown_key") == 1 and codes.count("bad_signature") == 1


def test_the_session_comes_from_the_pairing_and_is_logged(world):
    g = world.start()
    _, kr = plan_and_begin(g)
    g.close()
    p = pr.load(world.record)
    started = [x["payload"] for x in records(world.state) if x["type"] == "gate.started"]
    assert len(started) == 1
    s = started[0]
    assert s["key_id"] == p.key_id.hex() and s["medium_id"] == p.medium_id.hex() and s["target"] == TARGET
    assert s["session"] == kr["session_id"]
    assert bytes.fromhex(s["session"]) == ap.session_id(bytes.fromhex(s["session_pub"]), p.transcript)


def test_a_key_cannot_be_pinned_by_argument(world):
    pin = raw_pub(Ed25519PrivateKey.generate()).hex()
    base = ["--root", str(world.root), "--state", str(world.state), "--target", TARGET]
    for extra in (["--pin", pin, "--session", "00" * 32], ["--pin", pin], ["--pairing", str(world.record), "--pin", pin],
                  ["--pairing", str(world.record), "--session", "00" * 32], []):
        rc, _ = world.start(argv=base + extra).close()
        assert rc != 0, extra
    assert records(world.state) == []


def test_the_gate_refuses_a_bad_or_writable_pairing(world):
    bad = world.tmp / "bad-medium"
    bad.mkdir()
    other = raw_pub(Ed25519PrivateKey.generate())
    for name, data in _variants(world.key.public_key, bytes(16), other).items():
        (bad / f"{name}.json").write_bytes(data)
        rc, err = world.start(pairing=bad / f"{name}.json").close()
        assert rc != 0, name
        assert "pairing" in err.lower(), name
    rc, _ = world.start(pairing=bad / "absent.json").close()
    assert rc != 0
    # a record the model could write (inside --root) is refused even if it is well formed
    inside = world.root / "medium"
    inside.mkdir()
    rc, err = world.start(pairing=pr.provision(inside, world.key.public_key)).close()
    assert rc != 0 and "pairing" in err.lower()
    assert records(world.state) == []


# ---- the host owner's launch is the session consent (D-021) ----------------------------------------------------------------------------

def test_terms_are_shown_to_the_owner_first_and_logged_before_the_gate_starts(world):
    for _ in range(2):  # every launch shows and logs them again
        g = world.start()
        r = g.call("log_head")
        assert r["ok"]
        rc, err = g.close()
        assert rc == 0
        assert err.replace("\r\n", "\n").startswith(pr.TERMS), err[:300]  # Windows text-mode stderr writes CRLF
    terms = pr.TERMS.lower()
    for words in (("read-only",), ("press", "one"), ("unplug",), ("button",)):
        assert all(w in terms for w in words), words
    recs = records(world.state)
    starts = [i for i, x in enumerate(recs) if x["type"] == "gate.started"]
    assert len(starts) == 2
    for i in starts:
        prev = recs[i - 1]
        assert prev["type"] == "session.terms_shown"
        assert prev["payload"]["sha256"] == hashlib.sha256(pr.TERMS.encode("utf-8")).hexdigest() == pr.terms_sha256()
    assert recs[0]["type"] == "session.terms_shown"


# ---- MAS chat_choice approvals are absent from the device build -----------------------------------------------------------------------

def _device_modules() -> list[Path]:
    return sorted(PKG.glob("*.py"))


def test_device_build_imports_nothing_from_mas_and_has_no_chat_choice():
    allowed_roots = set(sys.stdlib_module_names) | {"cryptography", "mas_device", "__future__"}
    files = _device_modules()
    assert {f.name for f in files} >= {"approval.py", "gate.py", "gated.py", "pairing.py"}
    for f in files:
        text = f.read_text(encoding="utf-8")
        assert "chat_choice" not in text, f
        tree = ast.parse(text, filename=str(f))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0 or (node.module or "").split(".")[0] not in ("envrec",), f
                names = [node.module or ""] if node.level == 0 else []
            elif isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) in ("import_module", "__import__"):
                pytest.fail(f"{f.name}: dynamic import")
            else:
                continue
            for n in names:
                assert n.split(".")[0] in allowed_roots, f"{f.name} imports {n}"


def test_running_the_device_build_loads_no_mas_module():
    mods = sorted(f.stem for f in _device_modules() if f.stem != "__init__")
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import importlib\n"
            "for m in sys.argv[2:]: importlib.import_module('mas_device.' + m)\n"
            "print(sorted(n for n in sys.modules if n.startswith('envrec')))")
    out = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(SRC), *mods], capture_output=True, text=True, timeout=TIMEOUT)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"
