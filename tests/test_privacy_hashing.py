"""The identifier hashing in the privacy gate: ambient devices only as per-run hashed ids; owned devices stable across runs; no raw address in a key or a value."""
import json
import re

from envrec.contracts import Target, TargetKind
from envrec.discovery import Salts
from envrec.discovery.types import Capture, Entity
from envrec.privacy.gate import PrivacyGate
from envrec.store import Store

MAC = "AA:BB:CC:DD:EE:01"


def written(tmp_path, owned: str, salts: Salts, name: str):
    with Store(tmp_path / name) as st:
        t = st.put_target(Target(kind=TargetKind.host, ring=1, handle="h"))
        gate = PrivacyGate(st, t, salts, "ble_scan")
        gate.store_captures([Capture("scan", "recorded", b"[]", 0, "local", "macos")])
        facts, _ = gate.build([Entity("ble", 1, "adv:" + MAC, owned, {"vendor": "V", "name": "Phone of " + MAC}, src="scan")])
        assert len(facts) == 1  # the entity was built (a dropped entity would make every check below pass for nothing)
        gate.write_facts(facts)
        return [(f.key, json.dumps(f.value)) for f in st.current_facts(t.id)]


def test_an_ambient_entity_key_is_a_short_hash_and_the_raw_address_appears_nowhere(tmp_path):
    rows = written(tmp_path, "ambient", Salts(), "a")
    keys = [k for k, _ in rows]
    assert len(keys) == 1 and re.fullmatch(r"ble\.entity\.bluetooth-[0-9a-f]{8}|ble\.entity\.ble-[0-9a-f]{8}", keys[0]), keys
    assert MAC.lower() not in json.dumps(rows).lower() and "aabbccddee01" not in json.dumps(rows).lower().replace(":", "")


def test_the_same_ambient_device_gets_a_different_id_in_a_different_run(tmp_path):
    first = written(tmp_path, "ambient", Salts(), "r1")[0][0]
    second = written(tmp_path, "ambient", Salts(), "r2")[0][0]
    assert first != second  # a stranger's device cannot be followed from one run to the next


def test_an_owned_device_keeps_one_id_across_runs_when_the_stable_salt_is_shared(tmp_path):
    stable = Salts(tmp_path / "stable-salt")
    a = written(tmp_path, "owned", stable, "o1")[0][0]
    b = written(tmp_path, "owned", Salts(tmp_path / "stable-salt"), "o2")[0][0]
    assert a == b
