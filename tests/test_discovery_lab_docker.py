"""D5: the Docker lab. Its contents are known by construction (envrec.lab.EXPECTED), so what discovery finds is scored against automatic ground truth."""

from __future__ import annotations

import json

import pytest

from docker_helpers import need_docker
from envrec import lab

pytestmark = need_docker("envrec-fixture-sshd")


@pytest.fixture(scope="module")
def lab_run():
    if not lab.docker_ready():
        pytest.skip("docker is not running")
    lab.up()
    try:
        r = lab.discover()
        assert r.returncode == 0, r.stderr[-800:]
        yield json.loads(r.stdout)
    finally:
        lab.down()


def ents(run, kind=None):
    return [e for e in run["entities"] if kind is None or e["kind"] == kind]


def test_every_lab_host_is_found_alive(lab_run):
    found = {e["attrs"]["ip"] for e in ents(lab_run, "host")}
    assert set(lab.EXPECTED["hosts"]) <= found
    sweep = next(a for a in lab_run["adapters"] if a["adapter_id"] == "lan_sweep")
    assert sweep["status"] == "ran" and lab_run["facts"]["net.lan_sweep_scope"]["network"] == lab.SUBNET


def test_the_scope_of_the_scan_is_the_labs_own_subnet(lab_run):
    assert lab_run["scope"] == lab.SUBNET
    assert lab_run["facts"]["net.lan_sweep_scope"]["scanned"] == 253


def test_mdns_services_are_found_and_the_strange_type_stays_unknown(lab_run):
    svc = [e for e in ents(lab_run, "service") if e["attrs"].get("service_type")]
    types = {(e["attrs"]["ip"], e["attrs"]["service_type"]) for e in svc}
    for ip, ts in lab.EXPECTED["mdns"].items():
        for t in ts:
            assert (ip, t) in types, (ip, t)
    strange = next(e for e in svc if e["attrs"]["service_type"] in lab.EXPECTED["unknown_mdns_types"])
    assert strange["resolved"] is False
    ipp = next(e for e in svc if e["attrs"]["service_type"] == "_ipp._tcp")
    assert ipp["resolved"] and ipp["attrs"].get("txt_ty") == "Lab LaserJet"
    assert "deviceid" not in json.dumps(ipp) and "AA:BB" not in json.dumps(lab_run)  # TXT identifiers never survive reduction


def test_ssdp_device_description_is_read_and_its_serial_is_not(lab_run):
    ip, want = next(iter(lab.EXPECTED["ssdp"].items()))
    d = next(e for e in ents(lab_run, "service") if e["attrs"].get("ip") == ip and e["attrs"].get("manufacturer"))
    assert d["attrs"]["manufacturer"] == want["manufacturer"] and want["device_type_contains"] in (d["attrs"]["device_type"] or "")
    assert "LAB-SECRET" not in json.dumps(lab_run) and "uuid:lab" not in json.dumps(lab_run)


def test_open_ports_and_banners_match_the_construction(lab_run):
    open_ports: dict[str, set[int]] = {}
    for e in ents(lab_run, "service"):
        a = e["attrs"]
        if a.get("port") and not a.get("service_type"):
            open_ports.setdefault(a["ip"], set()).add(a["port"])
    for ip, ports in lab.EXPECTED["open_ports"].items():
        assert open_ports.get(ip, set()) == set(ports), (ip, open_ports.get(ip))
    for ip, (port, text) in lab.EXPECTED["banners"].items():
        svc = next(e for e in ents(lab_run, "service") if e["attrs"].get("ip") == ip and str(e["attrs"].get("port")) == port)
        assert text in ((svc["attrs"].get("banner") or "") + (svc["attrs"].get("http_server") or ""))


def test_the_silent_host_is_alive_but_has_nothing_open(lab_run):
    ip = lab.EXPECTED["silent_host"]
    assert ip in {e["attrs"]["ip"] for e in ents(lab_run, "host")}
    assert not [e for e in ents(lab_run, "service") if e["attrs"].get("ip") == ip]


def test_the_unknown_service_on_a_strange_port_is_an_unknown_entity(lab_run):
    ip, port = lab.EXPECTED["unknown_service"]
    s = next(e for e in ents(lab_run, "service") if e["attrs"].get("ip") == ip and e["attrs"].get("port") == port)
    assert s["resolved"] is False and s["status"] == "hypothesis"


def test_classes_are_hypotheses_and_the_printer_is_recognised(lab_run):
    by_ip = {d["ip"]: d for d in lab_run["devices"]}
    for ip, cls in lab.EXPECTED["classes"].items():
        assert by_ip[ip]["class"] == cls and by_ip[ip]["signals"]
    assert by_ip["172.30.77.15"]["class"] is None  # no signal, no class


def test_recall_and_precision_against_the_construction(lab_run):
    expected = set(lab.EXPECTED["hosts"])
    found = {e["attrs"]["ip"] for e in ents(lab_run, "host")} - {"172.30.77.1", lab.SCANNER_IP}  # the bridge gateway and the scanner itself are not lab nodes
    assert expected <= found and len(found - expected) == 0


def test_scope_refusal_outside_the_lab_subnet():
    """The same scanner on another network: the lab auto-approval only works on the lab's own subnet, so every active step is refused."""
    lab.sh("docker", "network", "rm", "envrec-lab-other")
    lab.sh("docker", "network", "create", "--subnet", "172.30.88.0/24", "envrec-lab-other", check=True)
    try:
        r = lab.sh(*lab.scanner_argv("envrec-lab-other", ["python", "-m", "envrec", "discover", "--ring", "2", "--active", "--lab-approve", "--json", "--runs-dir", "/tmp/runs"]), timeout=120)
    finally:
        lab.sh("docker", "network", "rm", "envrec-lab-other")
    out = json.loads(r.stdout)
    active = {a["adapter_id"]: a["status"] for a in out["adapters"] if a["adapter_id"].startswith("lan_")}
    assert active and set(active.values()) == {"denied"}
