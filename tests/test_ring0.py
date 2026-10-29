import asyncio
import json

import pytest

from envrec.adapters import RING0_KEYS, BuiltinRing0Adapter, ToolContext
from envrec.contracts import FactStatus, RingDecision, Target, TargetKind
from envrec.probes import ring0
from envrec.rings import close_ring
from envrec.store import Store

PROBES = [
    ring0.read_os, ring0.read_os_native, ring0.read_cpu_ram, ring0.read_hw_native, ring0.read_disks,
    ring0.read_battery, ring0.read_display, ring0.read_virtualization, ring0.read_tool_versions,
    ring0.read_net_interfaces,
]


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "s") as s:
        yield s


@pytest.fixture
def target(store):
    return store.put_target(Target(kind=TargetKind.host, ring=0, handle="this-machine"))


def run(adapter, tool, store, target):
    return asyncio.run(adapter.run(tool, {}, ToolContext(store=store, target=target, event_id="evt_test")))


@pytest.mark.parametrize("probe", PROBES, ids=lambda p: p.__name__)
def test_each_probe_has_the_ProbeOut_shape_and_valid_keys(probe):
    from envrec.contracts import check_key

    out = probe()
    assert out.values and out.raw and out.provider
    for key in out.values:
        check_key(key)
    for key, value in out.values.items():
        if value is None:
            assert out.reasons.get(key), f"{key} is None without a reason"
    json.loads(out.raw)  # raw evidence is valid JSON


def test_os_cross_check_agrees_on_this_machine():
    a, b = ring0.read_os(), ring0.read_os_native()
    for key in ("os.name", "os.arch"):
        if b.values[key] is not None:
            assert a.values[key] == b.values[key]


def test_values_are_plausible_here():
    cpu = ring0.read_cpu_ram().values
    assert cpu["hw.cpu_cores_logical"] >= 1 and cpu["hw.ram_total_gib"] > 0
    disks = ring0.read_disks().values
    assert disks["hw.disk_total_gib"] > disks["hw.disk_free_gib"] >= 0
    tools = ring0.read_tool_versions().values["host.tools"]
    assert set(tools) == set(ring0.TOOL_VERSION_CMDS)


def test_network_interfaces_are_names_only():
    out = ring0.read_net_interfaces()
    import re

    assert all(isinstance(n, str) for n in out.values["net.interfaces"])
    assert not re.search(r"\d+\.\d+\.\d+\.\d+", out.raw)  # no IPv4 address stored
    assert not re.search(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", out.raw, re.I)  # no MAC address stored


@pytest.mark.parametrize("probe", [ring0.read_os_native, ring0.read_hw_native, ring0.read_display,
                                   ring0.read_virtualization])
def test_os_specific_probes_degrade_to_unknown_with_a_reason(probe, monkeypatch):
    monkeypatch.setattr(ring0.platform, "system", lambda: "Plan9")
    monkeypatch.setattr(ring0.Path, "exists", lambda self: False)
    out = probe()
    assert all(v is None for v in out.values.values())
    assert all(out.reasons[k] for k in out.values)


def test_a_failing_command_degrades_instead_of_crashing(monkeypatch):
    def broken(*a, **k):
        raise FileNotFoundError("sw_vers")

    monkeypatch.setattr(ring0.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(ring0.subprocess, "run", broken)
    out = ring0.read_os_native()
    assert out.values["os.version"] is None and out.reasons["os.version"]
    assert ring0.read_display().values["hw.display_present"] is None


@pytest.mark.parametrize(
    ("known", "kind"),
    [
        ({"host.virtualization": "container", "os.name": "Linux"}, "container"),
        ({"host.virtualization": "vm", "os.name": "macOS"}, "vm"),
        ({"os.name": "macOS", "hw.display_present": True, "hw.battery_present": True,
          "host.virtualization": "none"}, "laptop"),
        ({"os.name": "Windows", "hw.display_present": True, "hw.battery_present": False}, "laptop"),
        ({"os.name": "Linux", "os.arch": "aarch64", "hw.display_present": False,
          "host.virtualization": "none"}, "sbc"),
        ({}, None),
    ],
)
def test_classify_host_rules(known, kind, monkeypatch):
    monkeypatch.setattr(ring0.Path, "is_file", lambda self: False)
    out = ring0.classify_host(known)
    assert out.values["host.kind"] == kind
    if kind is None:
        assert out.reasons["host.kind"]


def test_classify_notes_when_a_laptop_has_no_battery(monkeypatch):
    monkeypatch.setattr(ring0.Path, "is_file", lambda self: False)
    out = ring0.classify_host({"os.name": "macOS", "hw.display_present": True, "hw.battery_present": False})
    assert any("desktop" in lim for lim in out.limitations)


def test_capabilities_come_from_known_facts():
    out = ring0.classify_host({"os.name": "macOS", "hw.display_present": True,
                               "host.tools": {"git": "git 2", "docker": None}, "net.interfaces": ["en0"]})
    assert out.values["host.capabilities"] == ["display", "git", "network"]


# ---- the adapter ------------------------------------------------------------------------------

def test_every_tool_spec_is_valid_and_ring_0_observe():
    specs = BuiltinRing0Adapter().tool_specs()
    assert len(specs) == 11
    assert all(s.ring == 0 and s.tier.value == "observe" and s.produces for s in specs)
    assert set(RING0_KEYS) >= {"host.kind", "os.name", "hw.ram_total_gib", "net.interfaces"}


def test_adapter_results_carry_evidence_with_a_hash_checked_raw_ref(store, target):
    result = run(BuiltinRing0Adapter(), "read_cpu_ram", store, target)
    assert result.ok and result.coverage.status == "complete"
    for fact in result.facts:
        ev = fact.evidence[0]
        assert (ev.provider, ev.probe, ev.event_id) == ("psutil", "read_cpu_ram", "evt_test")
        assert json.loads(store.read_raw(ev.raw_ref))  # re-read and hash-verified


def test_unknown_adapter_tool_is_a_failed_result_not_an_exception(store, target):
    result = run(BuiltinRing0Adapter(), "no_such_tool", store, target)
    assert not result.ok and "no tool" in result.error


def test_two_independent_providers_agreeing_make_a_fact_verified(store, target):
    adapter = BuiltinRing0Adapter()
    for tool in ("read_os", "read_os_native"):
        for fact in run(adapter, tool, store, target).facts:
            store.write_fact(fact)
    current = {f.key: f for f in store.current_facts(target.id)}
    if current["os.arch"].status != FactStatus.verified:
        pytest.skip("native OS commands are not wired on this platform")
    assert {e.provider for e in current["os.arch"].evidence} == {"platform", "native"}


def test_classify_host_through_the_adapter_is_a_hypothesis(store, target):
    adapter = BuiltinRing0Adapter()
    for tool in ("read_os", "read_battery", "read_display", "read_virtualization",
                 "read_tool_versions", "read_net_interfaces", "classify_host"):
        for fact in run(adapter, tool, store, target).facts:
            store.write_fact(fact)
    kind = store.current_fact(target.id, "host.kind")
    if kind.status == FactStatus.unknown:
        pytest.skip("this machine could not be classified")
    assert kind.status == FactStatus.hypothesis and kind.evidence[0].provider == "rules"


# ---- closing ring 0 ---------------------------------------------------------------------------

def test_ring_close_decisions(store, target):
    adapter = BuiltinRing0Adapter()
    first = close_ring(store, target, 0, RING0_KEYS, further_rings=True)
    assert first.decision == RingDecision.deepen and first.checked == []  # nothing tried yet

    for spec in adapter.tool_specs():
        for fact in run(adapter, spec.name, store, target).facts:
            store.write_fact(fact)
    done = close_ring(store, target, 0, RING0_KEYS, further_rings=True)
    assert done.decision == RingDecision.extend
    assert set(done.checked) | set(done.unknown) == set(RING0_KEYS)
    final = close_ring(store, target, 0, RING0_KEYS, further_rings=False)
    assert final.decision == RingDecision.stop

    fact = store.current_fact(target.id, "ring.0.coverage")
    assert fact.value["decision"] == "stop" and fact.status == FactStatus.tested
