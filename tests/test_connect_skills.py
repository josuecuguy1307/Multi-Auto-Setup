"""S3: the connection and workspace knowledge lives in skills (SKILL.md folders with data in references/). The planner and the sizing now read that data; this proves
they decide exactly what the hard-coded versions they replaced decided, over a grid of facts and limits. Policy (allowed transports, limits, approvals) stays in code."""

from __future__ import annotations

import itertools

import pytest

from envrec.mission import TargetEntry, Transport
from envrec.remote.planner import HINTS, plan_transport, preference
from envrec.remote.sizing import CATALOG_PARAMS_B, RAM_TABLE, choose_model
from envrec.skills.lib import Library


# ---- the previous implementation, kept here verbatim as the reference ------------------------------------------------------------------------------

def old_preference(facts):
    kind, os_name = facts.get("host.kind"), str(facts.get("os.name") or "").lower()
    if kind == "container":
        return [Transport.docker_exec, Transport.ssh], ["host.kind=container"]
    if kind == "phone" or os_name == "android":
        return [Transport.adb_usb, Transport.ssh], [f"host.kind={kind}", f"os.name={os_name}"]
    if kind in ("instrument", "sensor"):
        return [Transport.tcp, Transport.serial], [f"host.kind={kind}"]
    if kind in ("sbc", "microcontroller"):
        return [Transport.ssh, Transport.serial], [f"host.kind={kind}"]
    if os_name in ("linux", "macos"):
        return [Transport.ssh], [f"os.name={os_name}"]
    return [Transport.ssh, Transport.docker_exec, Transport.adb_usb, Transport.serial, Transport.tcp], ["no rule matched: allowed order"]


OLD_HINTS = {
    Transport.ssh: "enable sshd on the device (on Android: install Termux and run sshd), add your own public key, then re-run",
    Transport.docker_exec: "start the container and make sure the docker CLI can see it, then re-run",
    Transport.adb_usb: "connect the cable, turn on USB debugging in developer options and accept the key prompt on the phone, then re-run",
    Transport.ios_usb: "connect the cable, unlock the phone and tap Trust when it asks, then re-run (libimobiledevice must be installed: brew install libimobiledevice)",
    Transport.serial: "connect the serial adapter, check the port name and permissions, then re-run",
    Transport.tcp: "power the instrument, check its address and port on your own network, then re-run",
}
OLD_RAM_TABLE = [(16.0, 13.0), (8.0, 7.0), (4.0, 3.0), (2.0, 1.5), (1.0, 0.5)]
OLD_CATALOG = [0.5, 1.0, 1.5, 3.0, 7.0, 13.0]


def old_disk_needed(p):
    return round(p * 0.6 + 0.5, 2)


def old_choose(ram, free, max_disk, max_params):
    ram_cap = next((cap for floor, cap in OLD_RAM_TABLE if ram >= floor), 0.0)
    caps = {"ram": ram_cap, "config_params": max_params, "disk": max((p for p in OLD_CATALOG if old_disk_needed(p) <= min(free, max_disk)), default=0.0)}
    binding = min(caps, key=lambda k: caps[k])
    fit = max((p for p in OLD_CATALOG if p <= caps[binding]), default=0.0)
    if fit == 0.0:
        return (0.0, "q4", 0.0, binding)
    bigger = next((p for p in OLD_CATALOG if p > fit), None)
    limited = "none" if bigger is None else min((k for k in caps if caps[k] < bigger), key=lambda k: caps[k], default="none")
    return (fit, "q4", old_disk_needed(fit), limited)


KINDS = [None, "container", "phone", "instrument", "sensor", "sbc", "microcontroller", "laptop", "server", "unknown"]
OSES = [None, "", "android", "Linux", "macos", "windows", "ios"]


@pytest.mark.parametrize("kind,os_name", list(itertools.product(KINDS, OSES)))
def test_the_route_table_in_the_skill_orders_transports_exactly_as_the_old_rules_did(kind, os_name):
    facts = {"host.kind": kind, "os.name": os_name}
    assert preference(facts) == old_preference(facts)


def test_the_setup_hints_are_the_sentences_the_planner_used_to_carry():
    assert HINTS == OLD_HINTS


@pytest.mark.parametrize("allowed", [[Transport.ssh], [Transport.docker_exec], [Transport.adb_usb, Transport.ssh], [Transport.tcp, Transport.serial], [Transport.serial, Transport.docker_exec, Transport.ssh]])
@pytest.mark.parametrize("kind", [None, "container", "phone", "sbc"])
def test_the_plan_only_ever_chooses_among_the_allowed_routes_and_names_what_decided_it(allowed, kind):
    entry = TargetEntry.model_construct(id="box", name="Box", host="127.0.0.1", allowed_transports=allowed)  # the planner reads only the allowed list
    for answered in allowed:
        plan = plan_transport(entry, {"host.kind": kind}, {answered: True})
        assert plan.transport == answered and plan.transport in allowed
    none = plan_transport(entry, {"host.kind": kind}, {})
    assert none.transport is None and none.hint.endswith(HINTS[none.considered[0]])


@pytest.mark.parametrize("ram,free,max_disk,max_params", list(itertools.product([0.2, 0.9, 1, 1.9, 2, 3.9, 4, 7.9, 8, 15.9, 16, 64], [0.3, 1.0, 2.0, 5.0, 40.0], [1.0, 4.0, 100.0], [0.5, 1.0, 3.0, 13.0])))
def test_the_size_table_in_the_skill_chooses_exactly_what_the_old_constants_chose(ram, free, max_disk, max_params):
    c = choose_model(ram, free, max_disk, max_params)
    assert (c.params_b, c.quant, c.disk_gb, c.limited_by) == old_choose(ram, free, max_disk, max_params)


def test_the_tables_are_data_in_the_skill_not_python():
    assert RAM_TABLE == OLD_RAM_TABLE and CATALOG_PARAMS_B == OLD_CATALOG
    lib = Library().load()
    names = {"connect-route", "connect-ssh", "connect-adb", "connect-docker-exec", "connect-serial", "connect-tcp", "workspace-sizing", "workspace-provisioning"}
    assert names <= set(lib.skills), (names - set(lib.skills), lib.errors)
    for n in names:
        s = lib.get(n)
        assert s.description.startswith("Use when") and s.meta.plan is None  # guidance and data: nothing here runs
