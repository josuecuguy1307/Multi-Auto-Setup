"""A1: knowledge that used to live in Python under src/ now lives in a skill's references/ or in a fixture. Each move has a parity test that holds the previous implementation verbatim and
proves the new one decides exactly the same, so the move changed where the knowledge is kept and nothing it says."""

from __future__ import annotations

import itertools
import json
import re
from pathlib import Path

import pytest

from envrec import lab
from envrec.discovery import Salts
from envrec.discovery.types import short_hash
from envrec.remote import ios
from envrec.tools.builtin.ios import _value

ROOT = Path(__file__).resolve().parents[1]


# ---- the previous remote/ios.py tables and rules, verbatim -----------------------------------------------------------------------------------------

OLD_ASPECTS = {
    "identity": [("ios.product_type", None, "ProductType"), ("ios.version", None, "ProductVersion"), ("ios.device_class", None, "DeviceClass")],
    "battery": [("ios.battery_pct", "com.apple.mobile.battery", "BatteryCurrentCapacity"), ("ios.battery_charging", "com.apple.mobile.battery", "BatteryIsCharging")],
    "storage": [("ios.storage_total_gb", "com.apple.disk_usage", "TotalDiskCapacity"), ("ios.storage_data_total_gb", "com.apple.disk_usage", "TotalDataCapacity")],
    "name": [("ios.name_sha", None, "DeviceName")],
}
OLD_UDID = re.compile(r"^[0-9A-Fa-f][0-9A-Fa-f\-]{15,63}$")


def old_classify(code, out, err):
    text = f"{out}\n{err}".lower()
    if code == 127:
        return "not_installed", "libimobiledevice is not installed (brew install libimobiledevice)"
    if code == 124:
        return "timeout", "the phone did not answer in time; unlock it and watch for the Trust prompt"
    if re.search(r"user denied|denied pairing|-19\b", text):
        return "trust_denied", "the owner did not trust this computer on the phone: stopped, nothing was read"
    if re.search(r"pairing dialog|trust|-18\b|dialog response pending", text):
        return "trust_pending", "the phone is waiting for you: unlock it and tap Trust, then run this again"
    if re.search(r"passcode|password protected|-17\b|locked", text):
        return "locked", "the phone is locked: unlock it, then run this again"
    if re.search(r"no device found|not found", text):
        return "not_attached", "the phone is not attached: connect the cable"
    if "lockdownd" in text:
        return "not_trusted", "the phone did not complete the handshake: unlock it and tap Trust when it asks"
    return "failed", (err.strip().splitlines() or ["libimobiledevice failed"])[-1][:140]


def old_plan_text(aspect, device_hint="the attached device whose identifier hashes to the entry"):
    keys = ", ".join(k for _, _, k in OLD_ASPECTS[aspect])
    return (f"read the {aspect} of {device_hint} over the cable: `idevice_id -l` to see which devices are attached, then `ideviceinfo -u <that identifier> -k <KEY>` for {keys}. "
            "Read-only: it writes nothing to the phone. The phone may ask you to tap Trust; unlock it first. The identifier is never stored and the name is kept only as a salted hash.")


def old_fact_value(fact, key, raw, salt):
    """The branches of the previous tools/builtin/ios.py, verbatim."""
    def number(text):
        try:
            return int(text)
        except ValueError:
            return None
    if key in {"DeviceName"}:
        return short_hash(raw, salt=salt) if raw else None
    if fact in ("ios.storage_total_gb", "ios.storage_data_total_gb"):
        n = number(raw)
        return round(n / 1e9, 1) if n is not None else None
    if fact == "ios.battery_pct":
        return number(raw)
    if fact == "ios.battery_charging":
        return {"true": True, "false": False}.get(raw.lower())
    return raw or None


def test_the_table_the_code_reads_from_the_skill_is_the_one_that_was_hard_coded():
    assert ios.ASPECTS == OLD_ASPECTS and ios.HIDDEN_VALUE == {"DeviceName"}
    assert ios.HINT == "connect the cable, unlock the phone and tap Trust when it asks, then re-run"
    assert ios._UDID.pattern == OLD_UDID.pattern


@pytest.mark.parametrize("code,out,err", list(itertools.product(
    [0, 1, 2, 124, 127, 255],
    ["", "No device found", "lockdownd said no", "ERROR: user denied pairing -19", "Pairing dialog response pending", "x\nDevice is passcode protected -17"],
    ["", "ERROR: Could not connect to lockdownd, error code -18", "cannot start", "Device is locked", "device not found", "weird failure\nsecond line that is long " + "z" * 200])))
def test_the_error_rules_in_the_skill_classify_exactly_as_the_old_rules_did(code, out, err):
    assert ios.classify(code, out, err) == old_classify(code, out, err)


@pytest.mark.parametrize("aspect", sorted(OLD_ASPECTS))
def test_the_plan_sentence_in_the_skill_is_the_one_the_code_used_to_build(aspect):
    assert ios.plan_text(aspect) == old_plan_text(aspect)
    assert ios.plan_text(aspect, "the phone on the desk") == old_plan_text(aspect, "the phone on the desk")


@pytest.mark.parametrize("raw", ["", "87", "x", "true", "FALSE", "False", "128000000000", "119000000000", "0", "iPhone15,2", "17.5.1", "Owner name"])
def test_every_fact_converts_its_raw_value_exactly_as_the_old_branches_did(raw):
    salt = Salts().stable
    for aspect, rows in OLD_ASPECTS.items():
        for fact, _domain, key in rows:
            assert _value(ios.CONVERT[fact], raw, salt) == old_fact_value(fact, key, raw, salt), (fact, raw)


def test_the_conversion_is_named_for_every_fact_the_table_can_read():
    assert set(ios.CONVERT) == {f for rows in OLD_ASPECTS.values() for f, _d, _k in rows}
    assert set(ios.CONVERT.values()) <= {"text", "int", "bool", "gb", "hash"}


# ---- the lab's ground truth, verbatim --------------------------------------------------------------------------------------------------------------

def test_the_lab_ground_truth_read_from_the_fixture_is_what_the_module_used_to_hold():
    old = {
        "hosts": [n.ip for n in lab.NODES],
        "mdns": {"172.30.77.11": ["_ipp._tcp", "_zxq-lab._tcp"]},
        "unknown_mdns_types": ["_zxq-lab._tcp"],
        "ssdp": {"172.30.77.12": {"manufacturer": "Lab Corp", "device_type_contains": "MediaRenderer"}},
        "open_ports": {"172.30.77.13": [22], "172.30.77.14": [80, 8080], "172.30.77.16": [31337], "172.30.77.12": [], "172.30.77.15": []},
        "banners": {"172.30.77.13": ("22", "SSH-2.0"), "172.30.77.16": ("31337", "ZXQ-PROTO v9 ready"), "172.30.77.14": ("80", "LabHTTPd/1.0")},
        "silent_host": "172.30.77.15",
        "unknown_service": ("172.30.77.16", 31337),
        "classes": {"172.30.77.11": "printer"},
    }
    assert lab.EXPECTED == old
    assert (ROOT / "tests/docker/lab/expected.json").is_file() and json.loads((ROOT / "tests/docker/lab/expected.json").read_text())["silent_host"] == "172.30.77.15"
