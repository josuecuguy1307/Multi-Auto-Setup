"""The iPhone route: a hash instead of an identifier, a fixed read-only table, the Trust prompt as a stop, and nothing that is the owner's secret ends up stored."""
import hashlib
import json

import pytest
from pydantic import ValidationError

from envrec.contracts import Target, TargetKind
from envrec.discovery import Salts
from envrec.discovery.types import this_platform
from envrec.mission import EntryLimits, IosBlock, TargetConfig, TargetEntry, Transport
from envrec.remote import ios
from envrec.remote.runner import RemoteRunner, TargetNotAllowed
from envrec.store import Store
from envrec.tools import ToolContext, get_tool, run_tool

UDID = "00008110-001A2B3C4D5E6F78"
NAME = "Owner's private phone name"
H = hashlib.sha256(UDID.lower().encode()).hexdigest()


def entry(**kw):
    return TargetEntry(id="phone", name="Phone", host="192.168.1.9", allowed_transports=[Transport.ios_usb], ios_usb=IosBlock(udid_sha256=H),
                       limits=EntryLimits(max_disk_gb=1, max_model_params="0.5B"), **kw)


def fake(answers: dict, calls: list):
    def exec_fn(argv, stdin, timeout):
        calls.append(list(argv))
        if argv[0] == "idevice_id":
            return answers.get("list", (0, f"{UDID}\n".encode(), b""))
        key = argv[-1]
        return answers.get(key, answers.get("*", (0, b"x\n", b"")))
    return exec_fn


def runner(answers=None, calls=None):
    return RemoteRunner(TargetConfig(targets=[entry()]), exec_fn=fake(answers or {}, [] if calls is None else calls))


def test_the_file_holds_a_digest_never_the_identifier():
    with pytest.raises(ValidationError):
        IosBlock(udid_sha256=UDID)
    assert UDID not in json.dumps(entry().model_dump(mode="json"))


def test_only_the_attached_device_that_hashes_to_the_entry_is_read_and_only_keys_in_the_table():
    calls = []
    r = runner({"ProductType": (0, b"iPhone15,2\n", b""), "ProductVersion": (0, b"17.5.1\n", b""), "DeviceClass": (0, b"iPhone\n", b"")}, calls)
    res = r.ios_read("phone", "identity")
    assert res.ok and res.stdout.splitlines() == ["ProductType=iPhone15,2", "ProductVersion=17.5.1", "DeviceClass=iPhone"]
    assert [c[0] for c in calls] == ["idevice_id", "ideviceinfo", "ideviceinfo", "ideviceinfo"]
    assert {c[-1] for c in calls[1:]} == {"ProductType", "ProductVersion", "DeviceClass"}
    assert UDID not in " ".join(res.argv_shown)
    with pytest.raises(Exception):
        r.ios_read("phone", "contacts")


def test_a_phone_that_hashes_differently_is_not_touched():
    calls = []
    res = runner({"list": (0, b"00008110-AAAAAAAAAAAAAAAA\n", b"")}, calls).ios_read("phone", "battery")
    assert not res.ok and [c[0] for c in calls] == ["idevice_id"] and ios.classify(res.exit_code, res.stdout, res.stderr)[0] == "not_attached"


def test_an_entry_that_does_not_allow_the_route_raises_before_any_process():
    cfg = TargetConfig(targets=[TargetEntry(id="box", name="Box", host="10.0.0.1", allowed_transports=[Transport.tcp],
                                            tcp={"host": "10.0.0.1", "port": 9}, limits=EntryLimits(max_disk_gb=1, max_model_params="0.5B"))])
    calls = []
    with pytest.raises(TargetNotAllowed):
        RemoteRunner(cfg, exec_fn=fake({}, calls)).ios_read("box", "identity")
    assert calls == []


@pytest.mark.parametrize("code,err,cat", [(127, "cannot start", "not_installed"), (1, "ERROR: Could not connect to lockdownd, error code -18", "trust_pending"),
                                          (1, "Pairing dialog response pending", "trust_pending"), (1, "ERROR: user denied pairing -19", "trust_denied"),
                                          (1, "Device is passcode protected -17", "locked"), (1, "No device found", "not_attached")])
def test_the_trust_prompt_and_its_neighbours_are_named_and_never_retried(code, err, cat):
    calls = []
    res = runner({"ProductType": (code, b"", err.encode()), "list": (0, f"{UDID}\n".encode(), b"")}, calls).ios_read("phone", "identity")
    assert not res.ok and ios.classify(res.exit_code, res.stdout, res.stderr)[0] == cat
    assert sum(1 for c in calls if c[0] == "ideviceinfo") == 1  # stopped at the first refusal: no second try, no other route


def test_the_tool_stores_facts_with_the_name_only_as_a_hash(tmp_path):
    r = runner({"DeviceName": (0, NAME.encode() + b"\n", b""), "TotalDiskCapacity": (0, b"128000000000\n", b""), "TotalDataCapacity": (0, b"119000000000\n", b""),
                "BatteryCurrentCapacity": (0, b"87\n", b""), "BatteryIsCharging": (0, b"false\n", b"")})
    plans = []
    with Store(tmp_path / "s") as st:
        t = st.put_target(Target(kind=TargetKind.phone, ring=1, handle="ios:phone"))
        ctx = ToolContext(None, this_platform(), st, t, Salts(), {"ios_runner": r, "ios_target": "phone"}, lambda q: plans.append(q) or True)
        tool = get_tool("ios_info")
        got = {}
        for aspect in ("battery", "storage", "name"):
            res = run_tool(tool, ctx, {"aspect": aspect})
            assert res.ok, res.detail
            got.update(res.facts)
            assert NAME not in b"".join(c.data for c in res.captures).decode()
    assert got["ios.battery_pct"] == 87 and got["ios.battery_charging"] is False and got["ios.storage_total_gb"] == 128.0
    assert NAME not in str(got) and len(got["ios.name_sha"]) == 8 and len(plans) == 3 and all("Read-only" in p for p in plans)


def test_declining_the_approval_reads_nothing():
    calls = []
    ctx = ToolContext(None, this_platform(), None, None, Salts(), {"ios_runner": runner(calls=calls), "ios_target": "phone"}, lambda q: False)
    res = run_tool(get_tool("ios_info"), ctx, {"aspect": "identity"})
    assert not res.ok and calls == []


def test_the_phone_protocol_walks_identity_battery_storage_name_and_ends_only_when_each_was_tried():
    from envrec.contracts import FactStatus
    from envrec.contracts.models import Evidence, Fact
    from envrec.protocols import EvidenceView, ProtocolSet
    from envrec.skills.lib import Library
    run = ProtocolSet.from_library(Library().load()).start("phone")
    assert run.id == "phone" and run.state == "identity"

    def fact(key):
        return Fact(target_id="t", key=key, value=1, status=FactStatus.tested, evidence=[Evidence(provider="p", probe="q", confidence=0.9)])

    seen, order = {}, [("identity", ["ios.product_type", "ios.version"], "battery"), ("battery", ["ios.battery_pct"], "storage"),
                       ("storage", ["ios.storage_total_gb"], "name"), ("name", ["ios.name_sha"], "done")]
    for state, keys, nxt in order:
        assert run.state == state and run.ready(EvidenceView(dict(seen))) is None  # nothing yet: the step cannot be passed
        for k in keys[:-1]:
            seen[k] = fact(k)
        if len(keys) > 1:
            assert run.ready(EvidenceView(dict(seen))) is None  # half of the evidence is not enough
        seen[keys[-1]] = fact(keys[-1])
        assert run.ready(EvidenceView(dict(seen))) == nxt
        run.move(nxt, "model")
    assert run.terminal and run.allows_entry("ios_info", ["ios"]) is False  # at the end nothing more is offered
    assert [h["state"] for h in run.history] == ["identity", "battery", "storage", "name", "done"]
