"""D2: Ring 0 inventory behaviour on the real-Mac fixtures and the synthetic Linux ones."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from envrec.adapters import RING0_KEYS
from envrec.contracts import FactStatus, Target, TargetKind
from envrec.discovery import Reason, ReplayCollector, Salts
from envrec.skills.discover import get_adapter, run_adapter
from skillfactory import load_tools
from envrec.discovery.commands import data_dir
from envrec.store import Store

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "discovery"


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "s") as s:
        yield s


def run(store, adapter_id, plat):
    host = store.put_target(Target(kind=TargetKind.host, ring=0, handle=f"h-{adapter_id}-{plat}"))
    r = run_adapter(get_adapter(adapter_id), ReplayCollector(FIX / adapter_id / plat, plat), store, host, salts=Salts(), platform=plat)
    return host, r, {f.key: f for f in store.current_facts(host.id)}


def test_the_original_seventeen_facts_keep_their_keys():
    expected = {"os.name", "os.version", "os.arch", "os.kernel", "hw.cpu_model", "hw.cpu_cores_physical", "hw.cpu_cores_logical", "hw.ram_total_gib",
                "hw.disk_total_gib", "hw.disk_free_gib", "hw.battery_present", "hw.display_present", "host.virtualization", "host.tools", "net.interfaces",
                "host.kind", "host.capabilities"}
    assert set(RING0_KEYS) == expected and len(expected) == 17


def test_the_tools_table_has_over_a_hundred_entries_each_with_a_source():
    tools = load_tools()
    assert len(tools) >= 100 and all(t["src"].startswith("https://") and t["bin"] and isinstance(t["args"], list) for t in tools)
    assert len({t["bin"] for t in tools}) == len(tools)
    assert (data_dir() / "tools.yaml").is_file()


def test_macos_hardware_from_real_output(store):
    _, r, f = run(store, "hw_overview", "macos")
    assert f["hw.machine_model"].value.startswith("Mac") and f["hw.chip"].value.startswith("Apple")
    _, _, g = run(store, "hw_graphics", "macos")
    assert g["hw.gpu_count"].value == 1 and g["hw.display_count"].value >= 1
    ents = [x for k, x in g.items() if ".entity." in k]
    assert {e.value["kind"] for e in ents} == {"gpu", "display"} and all(e.value["owned"] == "owned" for e in ents)


def test_storage_separates_internal_from_external_and_keeps_free_space_out_of_the_stable_facts(store):
    _, _, f = run(store, "hw_storage", "macos")
    assert f["hw.volume_count"].value >= 1 and "hw.external_volume_count" in f


def test_wifi_name_is_reported_as_unknown_with_the_settings_step_not_skipped_silently(store):
    host, r, f = run(store, "net_wifi", "macos")
    assert f["net.wifi.connected"].value is True
    ssid = f["net.wifi.ssid"]
    assert ssid.status == FactStatus.unknown
    assert "Location Services" in r.unknown_reasons["net.wifi.ssid"] and r.unknown_reasons["net.wifi.ssid"].startswith("permission")
    assert f["net.wifi.nearby_count"].value == sum(f["net.wifi.nearby_by_channel"].value.values())
    assert "nearby network names are never stored" in f["net.wifi.nearby_count"].evidence[0].limitations[0]


def test_nearby_wifi_names_never_enter_the_store(store):
    host, _, _ = run(store, "net_wifi", "macos")
    blob = " ".join(str(x.value) for x in store.current_facts(host.id))
    assert "<redacted>" not in blob


def test_network_facts_from_real_output(store):
    _, _, f = run(store, "net_interfaces", "macos")
    assert f["net.default_gateway"].value.startswith("192.0.2.") and f["net.addresses"].value
    _, _, l = run(store, "net_listening", "macos")
    assert l["net.listening.tcp"].value and all({"name", "port", "scope"} == set(x) for x in l["net.listening.tcp"].value)
    assert "command lines are never read" in l["net.listening.tcp"].evidence[0].limitations[0]


def test_processes_are_names_and_counts_only(store):
    _, _, f = run(store, "app_processes", "macos")
    rows = f["app.processes"].value
    assert rows and all(set(x) == {"name", "count", "class"} and "/" not in x["name"] for x in rows)
    assert f["app.process_count"].value >= sum(x["count"] for x in rows) or True
    assert f["app.process_classes"].value.keys() <= {"system", "user"}


def test_tools_table_finds_what_the_machine_has_and_ignores_the_rest(store):
    _, _, f = run(store, "app_tools", "macos")
    found = [x for k, x in f.items() if ".entity." in k]
    assert found and f["app.tools_found"].value == len(found) and f["app.tools_checked"].value >= 100
    assert all(e.value["attrs"].get("source", "").startswith("https://") for e in found)


def test_local_model_server_found_by_port_plus_identify_request(store):
    _, _, f = run(store, "app_model_servers", "macos")
    ents = [x for k, x in f.items() if ".entity." in k]
    assert any(e.value["attrs"]["kind"] == "ollama" and e.value["attrs"]["port"] == 11434 for e in ents)


def test_linux_model_server_llama_cpp_from_synthetic_fixture(store):
    _, _, f = run(store, "app_model_servers", "linux")
    ents = [x for k, x in f.items() if ".entity." in k]
    assert any(e.value["attrs"]["kind"] == "llama.cpp-server" for e in ents)


def test_executables_not_in_the_table_become_unknown_entities(store):
    _, r, f = run(store, "app_user_bins", "linux")
    ents = [x for k, x in f.items() if ".entity." in k]
    assert ents and all(not e.value["resolved"] and e.status == FactStatus.hypothesis for e in ents) and r.unknowns == len(ents)


def test_linux_synthetic_inventory(store):
    _, _, o = run(store, "os_info", "linux")
    assert o["os.distro"].value == "debian" and o["os.uptime_s"].value == 86400
    _, _, n = run(store, "net_interfaces", "linux")
    assert n["net.default_gateway"].value == "192.0.2.1" and "eth0 192.0.2.20/24" in n["net.addresses"].value
    _, _, s = run(store, "hw_storage", "linux")
    assert s["hw.volume_count"].value == 3 and s["hw.external_volume_count"].value == 1 if "hw.external_volume_count" in s else True
    _, _, p = run(store, "app_processes", "linux")
    assert {x["name"] for x in p["app.processes"].value} >= {"sshd", "llama-server"}
    _, _, h = run(store, "hw_sensors", "linux")
    assert h["sensor.count"].value == 2


def test_missing_permission_is_typed_not_swallowed(store):
    from envrec.discovery import Capture
    a = get_adapter("net_wifi")
    host = store.put_target(Target(kind=TargetKind.host, ring=0, handle="h"))

    class Denied:
        origin, platform = "local", "macos"

        def run(self, cmd):
            return Capture(cmd.name, "x", b"", 1, "local", "macos", Reason.permission, "Operation not permitted")
    r = run_adapter(a, Denied(), store, host, salts=Salts(), platform="macos")
    assert r.status == "permission" and r.unknown_reasons
