"""Guardrail 3 (MISSION 4.1): an approval is signed, bound to the exact action (its hash), single-use and expiring; the model cannot reuse it.

Spec: DECISIONS.md D-004 (fixed-width binary struct signed by the key; single use through a verifier nonce held in the gate's memory; TTL on the
gate's monotonic clock; signed display summary re-derived from the action; undo bound by hash at approval).
SIMULATED: the key is the Python reference model of the firmware (src/mas_device/keymodel.py), not hardware."""

from __future__ import annotations

import copy
import hashlib
import json
import threading

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mas_device import approval as ap
from mas_device.keymodel import Button, KeyModel, KeyRefused

SESSION = ap.session_id(b"\x01" * 32, b"pairing transcript")
ACTION = {"kind": "write_file", "target": "host-aaaa", "argv": ["mas", "setup", "--recipe", "llamacpp"], "files": {"bin/llama-server": "c" * 64},
          "pre_state": "e" * 64, "undo": {"kind": "remove_path", "argv": ["mas", "undo", "llamacpp"]}}


class Tick:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def gate_clock():
    return Tick()


@pytest.fixture
def key_clock():
    return Tick(5.0)


@pytest.fixture
def key(tmp_path, key_clock):
    return KeyModel.create(tmp_path / "key", clock=key_clock)


@pytest.fixture
def gate(key, gate_clock):
    return ap.Gate({key.key_id: key.public_key}, session=SESSION, clock=gate_clock)


def approve(gate, key, action=ACTION, cls=ap.Class.WRITE):
    pending = gate.begin(action, cls)
    req = key.request(pending.key_request())
    key._clock.t += KeyModel.MIN_DWELL_S  # the person reads the screen first (D-017)
    key.button.press()
    return key.sign(req)


# ---- happy path, exactly once ----------------------------------------------------------

def test_valid_token_is_accepted_once(gate, key):
    ok = gate.consume(approve(gate, key), ACTION)
    assert ok.counter == 1 and ok.action_hash == ap.action_hash(ACTION) and ok.cls == ap.Class.WRITE


def test_token_layout_is_fixed_width(gate, key):
    tok = approve(gate, key)
    assert isinstance(tok, bytes) and len(tok) == ap.BODY_LEN + 64 and tok.startswith(ap.DOMAIN)
    assert ap.BODY_LEN == 15 + 8 + 8 + 32 + 16 + 32 + 32 + 32 + 1 + 1


def test_replay_is_rejected(gate, key):
    tok = approve(gate, key)
    gate.consume(tok, ACTION)
    with pytest.raises(ap.Rejected) as e:
        gate.consume(tok, ACTION)
    assert e.value.code == "unknown_nonce"


def test_gate_restart_kills_outstanding_tokens(gate, key, gate_clock):
    tok = approve(gate, key)
    fresh = ap.Gate({key.key_id: key.public_key}, session=SESSION, clock=gate_clock)
    with pytest.raises(ap.Rejected, match="unknown_nonce"):
        fresh.consume(tok, ACTION)


def test_concurrent_double_spend_succeeds_exactly_once(gate, key):
    tok = approve(gate, key)
    wins, errs = [], []

    def go():
        try:
            wins.append(gate.consume(tok, copy.deepcopy(ACTION)))
        except ap.Rejected as exc:
            errs.append(exc.code)

    ts = [threading.Thread(target=go) for _ in range(16)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(wins) == 1 and set(errs) == {"unknown_nonce"}


def test_counter_must_increase_within_a_session(gate, key):
    p1, p2 = gate.begin(ACTION, ap.Class.WRITE), gate.begin(ACTION, ap.Class.WRITE)
    r1 = key.request(p1.key_request())
    key.button.press()
    t1 = key.sign(r1)
    r2 = key.request(p2.key_request())
    key.button.press()
    t2 = key.sign(r2)
    gate.consume(t2, ACTION)
    with pytest.raises(ap.Rejected, match="counter_regressed"):
        gate.consume(t1, ACTION)


# ---- bound to the exact action ---------------------------------------------------------

@pytest.mark.parametrize("mutate", [
    lambda a: a["argv"].append("--force"),
    lambda a: a["argv"].__setitem__(3, "llamacpp "),
    lambda a: a["files"].__setitem__("bin/llama-server", "d" * 64),
    lambda a: a.__setitem__("target", "host-other"),
    lambda a: a.__setitem__("pre_state", "f" * 64),
    lambda a: a["undo"]["argv"].__setitem__(2, "everything"),
    lambda a: a.__setitem__("extra", "yes"),
])
def test_token_does_not_cover_a_different_action(gate, key, mutate):
    tok = approve(gate, key)
    other = copy.deepcopy(ACTION)
    mutate(other)
    with pytest.raises(ap.Rejected) as e:
        gate.consume(tok, other)
    assert e.value.code == "action_mismatch"


def test_rejected_attempt_does_not_burn_the_token(gate, key):
    tok = approve(gate, key)
    other = copy.deepcopy(ACTION)
    other["argv"].append("--force")
    with pytest.raises(ap.Rejected):
        gate.consume(tok, other)
    assert gate.consume(tok, ACTION).counter == 1


def test_token_for_one_class_is_not_valid_for_another(gate, key):
    tok = approve(gate, key, cls=ap.Class.WRITE)
    with pytest.raises(ap.Rejected, match="class_mismatch"):
        gate.consume(tok, ACTION, cls=ap.Class.JOIN_FLEET)


def test_a_write_without_an_undo_is_never_offered(gate):
    no_undo = {k: v for k, v in ACTION.items() if k != "undo"}
    with pytest.raises(ap.Rejected, match="no_undo"):
        gate.begin(no_undo, ap.Class.WRITE)


def test_undo_is_bound_by_hash(gate, key):
    ok = gate.consume(approve(gate, key), ACTION)
    assert ok.undo_hash == ap.action_hash(ACTION["undo"])


def test_action_hash_is_domain_separated_and_key_order_independent():
    reordered = dict(reversed(list(ACTION.items())))
    assert ap.action_hash(reordered) == ap.action_hash(ACTION)
    assert ap.action_hash(ACTION) != hashlib.sha256(ap.canonical(ACTION)).digest()


@pytest.mark.parametrize("bad", [{"x": 1.5}, {"x": float("nan")}, {1: "int key"}, {"x": b"bytes"}, {"x": "é"}, {"x": {1, 2}}, {"x": 2**63}, {"x": ("tuple",)}])
def test_canonical_refuses_ambiguous_values(bad):
    with pytest.raises(ap.Rejected) as e:
        ap.canonical(bad)
    assert e.value.code == "malformed"


def test_duplicate_json_keys_in_an_action_are_refused():
    with pytest.raises(ap.Rejected, match="malformed"):
        ap.parse_action('{"kind": "write_file", "kind": "remove_path"}')


# ---- tampering with the token ----------------------------------------------------------

def test_every_single_bit_flip_is_rejected(gate, key):
    """Exhaustive over all 241*8 bit positions: no flipped token is ever accepted, and the original still works afterwards."""
    tok = approve(gate, key)
    for i in range(len(tok) * 8):
        bad = bytearray(tok)
        bad[i // 8] ^= 1 << (i % 8)
        with pytest.raises(ap.Rejected):
            gate.consume(bytes(bad), ACTION)
    assert gate.consume(tok, ACTION)


@pytest.mark.parametrize("n", [0, 1, ap.BODY_LEN, ap.BODY_LEN + 63, ap.BODY_LEN + 65])
def test_wrong_length_is_malformed(gate, n):
    with pytest.raises(ap.Rejected, match="malformed"):
        gate.consume(b"\x00" * n, ACTION)


def test_non_bytes_is_malformed(gate):
    with pytest.raises(ap.Rejected, match="malformed"):
        gate.consume("not bytes", ACTION)


def test_token_from_an_unpinned_key_is_rejected(tmp_path, gate, key_clock):
    rogue = KeyModel.create(tmp_path / "rogue", clock=key_clock)
    with pytest.raises(ap.Rejected, match="unknown_key"):
        gate.consume(approve(gate, rogue), ACTION)


def test_forged_token_reusing_the_pinned_key_id_is_rejected(gate, key):
    """An attacker who knows everything except the private key builds a perfect body and signs it with its own key."""
    pending = gate.begin(ACTION, ap.Class.WRITE)
    body = ap.pack_body(key.key_id, 7, pending.key_request())
    forged = body + Ed25519PrivateKey.generate().sign(body)
    with pytest.raises(ap.Rejected, match="bad_signature"):
        gate.consume(forged, ACTION)


def test_summary_that_differs_from_the_action_is_rejected(gate, key):
    """The key signs what it showed. A host that sends a misleading summary to the key gets a token the gate refuses."""
    pending = gate.begin(ACTION, ap.Class.WRITE)
    req = pending.key_request()
    lying = req._replace(summary=ap.pad_summary("READ host-aaaa 0f harmless"))
    r = key.request(lying)
    key.button.press()
    with pytest.raises(ap.Rejected, match="summary_mismatch"):
        gate.consume(key.sign(r), ACTION)


# ---- expiry (gate clock; the key has no trusted wall clock) -----------------------------

def test_expired_pending_approval_is_rejected(gate, key, gate_clock):
    tok = approve(gate, key)
    gate_clock.t += ap.MAX_TTL_S
    with pytest.raises(ap.Rejected, match="expired"):
        gate.consume(tok, ACTION)
    gate_clock.t -= ap.MAX_TTL_S  # and expiry burned it
    with pytest.raises(ap.Rejected, match="unknown_nonce"):
        gate.consume(tok, ACTION)


def test_valid_until_just_before_expiry(gate, key, gate_clock):
    tok = approve(gate, key)
    gate_clock.t += ap.MAX_TTL_S - 0.001
    assert gate.consume(tok, ACTION)


# ---- bound to this session ---------------------------------------------------------------

def test_token_from_another_session_is_rejected(key, gate_clock):
    other = ap.Gate({key.key_id: key.public_key}, session=ap.session_id(b"\x02" * 32, b"other pairing"), clock=gate_clock)
    mine = ap.Gate({key.key_id: key.public_key}, session=SESSION, clock=gate_clock)
    tok = approve(other, key)
    with pytest.raises(ap.Rejected) as e:
        mine.consume(tok, ACTION)
    assert e.value.code in {"wrong_session", "unknown_nonce"}


def test_session_id_binds_key_and_transcript():
    assert ap.session_id(b"\x01" * 32, b"t") != ap.session_id(b"\x01" * 32, b"u") != ap.session_id(b"\x03" * 32, b"t")
    assert len(ap.session_id(b"\x01" * 32, b"t")) == 32


# ---- the key: only a physical press signs ----------------------------------------------

def test_key_refuses_to_sign_without_a_press(gate, key):
    req = key.request(gate.begin(ACTION, ap.Class.WRITE).key_request())
    with pytest.raises(KeyRefused, match="no_press"):
        key.sign(req)


def test_a_press_before_the_request_does_not_count(gate, key):
    key.button.press()
    req = key.request(gate.begin(ACTION, ap.Class.WRITE).key_request())
    with pytest.raises(KeyRefused, match="no_press"):
        key.sign(req)


def test_one_press_signs_one_request(gate, key):
    req = key.request(gate.begin(ACTION, ap.Class.WRITE).key_request())
    key.button.press()
    key.sign(req)
    with pytest.raises(KeyRefused):
        key.sign(req)


def test_a_new_request_cancels_the_pending_one(gate, key):
    r1 = key.request(gate.begin(ACTION, ap.Class.WRITE).key_request())
    r2 = key.request(gate.begin(dict(ACTION, target="host-z"), ap.Class.WRITE).key_request())
    key.button.press()
    with pytest.raises(KeyRefused, match="stale_request"):
        key.sign(r1)
    assert key.sign(r2)


def test_press_window_expires_on_the_key_clock(gate, key, key_clock):
    req = key.request(gate.begin(ACTION, ap.Class.WRITE).key_request())
    key_clock.t += KeyModel.PRESS_WINDOW_S + 1
    key.button.press()
    with pytest.raises(KeyRefused, match="press_window"):
        key.sign(req)


def test_key_displays_the_summary_and_counter(gate, key):
    pending = gate.begin(ACTION, ap.Class.WRITE)
    key.request(pending.key_request())
    shown = key.display_text()
    assert ap.summary_for(ACTION, ap.Class.WRITE).decode().strip() in shown and "#1" in shown


def test_key_refuses_non_printable_summary(gate, key):
    req = gate.begin(ACTION, ap.Class.WRITE).key_request()._replace(summary=b"\x1b[2J" + b" " * 28)
    with pytest.raises(KeyRefused, match="summary"):
        key.request(req)


def test_key_counter_is_monotonic_across_restarts(tmp_path, gate, key_clock):
    k = KeyModel.create(tmp_path / "k", clock=key_clock)
    g = ap.Gate({k.key_id: k.public_key}, session=SESSION, clock=Tick())
    c1 = g.consume(approve(g, k), ACTION).counter
    k2 = KeyModel.load(tmp_path / "k", clock=key_clock)
    assert g.consume(approve(g, k2), ACTION).counter == c1 + 1


def test_power_cut_after_counter_commit_never_reuses_a_counter(tmp_path, gate, key_clock, monkeypatch):
    k = KeyModel.create(tmp_path / "k", clock=key_clock)
    req = k.request(gate.begin(ACTION, ap.Class.WRITE).key_request())
    k.button.press()
    monkeypatch.setattr(k, "_release", lambda tok: (_ for _ in ()).throw(RuntimeError("power cut")))
    with pytest.raises(RuntimeError):
        k.sign(req)
    k2 = KeyModel.load(tmp_path / "k", clock=key_clock)
    g = ap.Gate({k2.key_id: k2.public_key}, session=SESSION, clock=Tick())
    assert g.consume(approve(g, k2), ACTION).counter == 2


def test_key_secret_never_appears_in_tokens(gate, key):
    tok = approve(gate, key)
    assert key._secret_bytes_for_test() not in tok


def test_key_secret_never_appears_in_its_public_state_file(tmp_path, key_clock):
    k = KeyModel.create(tmp_path / "k", clock=key_clock)
    state = json.loads((tmp_path / "k" / "state.json").read_text())
    assert k._secret_bytes_for_test().hex() not in json.dumps(state)


def test_button_has_no_programmatic_setter():
    b = Button()
    assert [m for m in dir(b) if not m.startswith("_")] == ["press"]
