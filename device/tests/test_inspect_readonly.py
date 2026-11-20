"""P3-READONLY: inspection makes zero writes (diff and read-only mount) and never touches the network (MISSION 4.1 rules 2, 9, 10;
D-011, D-012, D-037).

The inspector (mas_device.inspect) is device code: no sockets, no subprocesses (P2-GUARDRAILS' import allowlist). Its collectors form a
closed registry and read only through one reader with a path allowlist; one INSPECT_SESSION press authorises the whole read-only
session (D-012) and nothing is read before it. The report goes to the device's own folder, never to the host.

EMULATED: the inspected host is a clean container of the pinned image (P2-PORTABLE); the medium is mounted read-only.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import mas_device
from mas_device import approval as ap
from mas_device import inspect as ins
from mas_device import pairing as pr
from mas_device.keymodel import KeyModel

SRC = Path(mas_device.__file__).resolve().parents[1]  # the tree under test
TREE = SRC.parent
sys.path[:0] = [str(TREE / "device" / "medium"), str(Path(__file__).resolve().parent)]
import mas_dockerkit as dk  # noqa: E402
import mas_medium_build as md  # noqa: E402

LOCK = TREE / "device" / "medium" / "medium.lock.json"
PREP = "mkdir -p /host/etc && printf 'port=1\\n' > /host/etc/app.conf"
INSPECT = f"sh {dk.MOUNT}/launch.sh inspect --root / --state /state --target vm-i"
PLANTED = "PLANTED-SECRET-5a1c"
SCANNERS = ("lan-sweep", "lan-ports", "lan-ssdp", "ble-scan", "net-neighbors", "net-mdns", "net-listening", "connect-ssh",
            "connect-tcp", "router-environments", "remote-os-probe", "serial-probe", "protocol-discover")


class Tick:
    def __init__(self, t: float = 100.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def press(key: KeyModel, clock: Tick, kr: dict) -> str:
    req = ap.KeyRequest(*(bytes.fromhex(kr[n]) for n in ("session_id", "nonce", "action_hash", "undo_hash", "summary")),
                        kr["cls"], kr["press_window_s"])
    h = key.request(req)
    assert key.display_text()
    clock.t += 2
    key.button.press()
    return key.sign(h).hex()


# ---- in-process: the reader, the registry, the press -------------------------------------------------------------------------------------

def fake_host(root: Path) -> Path:
    files = {
        "etc/os-release": 'ID=debian\nVERSION_ID="12"\nPRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\n',
        "etc/hostname": "fixture-host\n",
        "proc/version": "Linux version 6.1.0 (fixture)\n",
        "proc/cpuinfo": "processor\t: 0\nmodel name\t: Fixture CPU\n\nprocessor\t: 1\nmodel name\t: Fixture CPU\n",
        "proc/meminfo": "MemTotal:        8000000 kB\n",
        "proc/modules": "ext4 1 0 - Live 0x0\nvfat 2 0 - Live 0x0\n",
        "var/lib/dpkg/status": "Package: bash\nStatus: install ok installed\nVersion: 5.2\nArchitecture: amd64\n\n"
                               "Package: gone\nStatus: deinstall ok config-files\nVersion: 1\nArchitecture: amd64\n\n"
                               "Package: zlib1g\nStatus: install ok installed\nVersion: 1.2.13\nArchitecture: amd64\n",
        "sys/block/sda/size": "1000\n", "sys/block/sda/removable": "0\n", "sys/block/sda/device/model": "Fixture Disk\n",
        "sys/class/net/eth0/address": "02:00:00:00:00:01\n", "sys/class/net/eth0/mtu": "1500\n",
        "sys/class/dmi/id/sys_vendor": "Fixture Inc\n", "sys/class/dmi/id/product_name": "Box\n",
        # planted: nothing below may ever be read
        "etc/shadow": f"root:{PLANTED}:19000::::::\n", "root/.ssh/id_rsa": PLANTED, "home/u/.aws/credentials": PLANTED,
        "sys/class/dmi/id/product_serial": PLANTED, "proc/1/environ": PLANTED, "etc/app.conf": PLANTED,
    }
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="ascii")
    return root


def test_the_registry_is_closed_and_has_no_network_collector():
    assert set(ins.REGISTRY) == {"os", "cpu", "memory", "kernel_modules", "packages_dpkg", "packages_apk", "block_devices",
                                 "net_interfaces", "dmi"}
    for name in ins.REGISTRY:
        assert not any(w in name for w in ("scan", "sweep", "neighbor", "mdns", "ssdp", "listen", "port", "ble", "connect", "probe")), name
    text = (SRC / "mas_device" / "inspect.py").read_text(encoding="utf-8")
    for marker in ("socket", "subprocess", "urllib", "http", "ctypes", "os.system", "popen"):
        assert marker not in text, marker


def test_the_reader_reads_only_its_allowlist(tmp_path):
    root = fake_host(tmp_path / "host")
    r = ins.Reader(root)
    assert r.read("etc/os-release").startswith(b"ID=debian")
    assert r.read("lib/apk/db/installed") is None  # allowed but absent: None, not an error
    for bad in ("etc/shadow", "root/.ssh/id_rsa", "home/u/.aws/credentials", "sys/class/dmi/id/product_serial", "proc/1/environ",
                "etc/app.conf", "../outside", "etc/../etc/shadow", "/etc/shadow", "etc//os-release", "sys/block/../../etc/shadow",
                "sys/class/net/eth0/../../../../etc/shadow", "etc\\shadow"):
        with pytest.raises(ins.Forbidden):
            r.read(bad)
    with pytest.raises(ins.Forbidden):
        r.listdir("root")
    assert sorted(r.listdir("sys/block")) == ["sda"]
    assert all(ins.allowed(s) for s in r.sources)


def test_collectors_report_the_fixture_and_never_the_planted_secrets(tmp_path):
    root = fake_host(tmp_path / "host")
    r = ins.Reader(root)
    rep = ins.run_collectors(r)
    assert rep["os"]["id"] == "debian" and rep["os"]["version_id"] == "12" and rep["os"]["hostname"] == "fixture-host"
    assert rep["cpu"]["count"] == 2 and rep["memory"]["total_bytes"] == 8000000 * 1024
    assert [p["name"] for p in rep["packages_dpkg"]] == ["bash", "zlib1g"]  # only installed ones
    assert rep["block_devices"] == [{"name": "sda", "bytes": 1000 * 512, "removable": False, "model": "Fixture Disk"}]
    assert rep["net_interfaces"] == [{"name": "eth0", "mac": "02:00:00:00:00:01", "mtu": 1500}]
    assert rep["dmi"] == {"sys_vendor": "Fixture Inc", "product_name": "Box"}
    assert PLANTED not in json.dumps(rep) and PLANTED not in json.dumps(r.sources)
    assert all(ins.allowed(s) for s in r.sources)


def _session(tmp_path, pub: bytes) -> tuple[ins.Session, Path]:
    medium = tmp_path / "medium"
    pr.provision(medium, pub)
    state = tmp_path / "state"
    state.mkdir()
    return ins.Session(pairing=medium / pr.RECORD_NAME, state=state, target="vm-i"), state


def test_nothing_is_read_without_the_inspect_session_press(tmp_path):
    clock = Tick()
    key = KeyModel.create(tmp_path / "key", clock=clock)
    root = fake_host(tmp_path / "host")
    s, state = _session(tmp_path / "a", key.public_key)
    kr = s.begin(root)
    assert kr["cls"] == ap.Class.INSPECT_SESSION and s.reader.sources == []
    stranger = Ed25519PrivateKey.generate()
    body = ap.pack_body(ap.key_id_of(stranger.public_key().public_bytes_raw()), 1,
                        ap.KeyRequest(*(bytes.fromhex(kr[n]) for n in ("session_id", "nonce", "action_hash", "undo_hash", "summary")),
                                      kr["cls"], kr["press_window_s"]))
    for token in ("", "zz", (body + stranger.sign(body)).hex()):
        with pytest.raises(ins.Refused):
            s.finish(token)
    assert s.reader.sources == [] and not list(state.glob("inspect-*.json"))
    # a WRITE token for the same request is never an inspection approval (no cross-class reuse)
    s2, state2 = _session(tmp_path / "b", key.public_key)
    kr2 = dict(s2.begin(root), cls=int(ap.Class.WRITE))
    with pytest.raises(ins.Refused):
        s2.finish(press(key, clock, kr2))
    assert s2.reader.sources == []
    # the real press: one report in the device's folder, logged with its hash and every path read
    s3, state3 = _session(tmp_path / "c", key.public_key)
    out = s3.finish(press(key, clock, s3.begin(root)))
    assert out.parent == state3 and out.name.startswith("inspect-vm-i-")
    rep = json.loads(out.read_bytes())
    assert rep["target"] == "vm-i" and rep["collectors"]["os"]["id"] == "debian"
    logged = [json.loads(x) for x in (state3 / "audit.log").read_bytes().splitlines() if x]
    done = [x["payload"] for x in logged if x["type"] == "inspect.done"]
    assert len(done) == 1 and done[0]["sources"] == rep["sources"] and len(done[0]["report_sha256"]) == 64


# ---- the medium carries no MAS skill and no scanner (D-011) -------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def lab():
    dk.docker_ok()
    root = TREE / "runs" / "inspect-tests"
    root.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="run-", dir=root))
    clock = Tick()
    key = KeyModel.create(base / "key", clock=clock)
    medium = md.build(base / "medium", cache=dk.medium_cache(TREE), key_pub=key.public_key, tree=TREE)
    lab = dk.Lab(json.loads(LOCK.read_text(encoding="utf-8"))["base_image"], medium)
    lab.base, lab.key, lab.clock = base, key, clock
    yield lab
    lab.close()
    shutil.rmtree(base, ignore_errors=True)


def test_the_medium_carries_no_mas_skill_and_no_scanner(lab):
    manifest = (lab.medium / "MANIFEST.sha256").read_text(encoding="utf-8")
    assert "skills/" not in manifest and "SKILL.md" not in manifest
    for name in SCANNERS:
        assert name not in manifest, name


# ---- in containers: zero writes, read-only root, no network --------------------------------------------------------------------------------

def inspect_in(lab, name: str, command: str, **kw) -> tuple[int, str, Path, dk.Machine, list]:
    state = lab.base / f"state-{name}"
    state.mkdir()
    m = dk.Machine(lab, name, ["sh", "-c", command], mounts=["-v", f"{state}:/state"], **kw)
    statuses = []
    while (msg := m.line()) is not None:
        if "key_request" in msg:
            m.send({"token": press(lab.key, lab.clock, msg["key_request"])})
        elif "status" in msg:
            statuses.append(msg)
    rc, err, _ = m.finish()
    return rc, err, state, m, statuses


def report_of(state: Path) -> dict:
    files = sorted(state.glob("inspect-*.json"))
    assert len(files) == 1, files
    return json.loads(files[0].read_bytes())


def test_inspection_makes_zero_writes_by_whole_filesystem_diff(lab):
    blank = lab.base / "blank-stick"
    blank.mkdir()
    (blank / "hello.sh").write_bytes(b"#!/bin/sh\necho hello\n")
    # same mount points in both arms (medium at /media/mas, a device folder at /state), so any difference is a write
    ctl = dk.Machine(lab, "control", ["sh", "-c", f"{PREP} && sh /state/hello.sh"], mounts=["-v", f"{blank}:/state:ro"],
                     flags=dk.LOCKED)
    assert ctl.finish()[0] == 0
    rc, err, state, m, statuses = inspect_in(lab, "inspect", f"{PREP} && exec {INSPECT}", flags=dk.LOCKED)
    assert rc == 0, err[-3000:]
    d = dk.diff(dk.snapshot(ctl.cid), dk.snapshot(m.cid))
    assert d == {}, d
    rep = report_of(state)
    assert rep["collectors"]["os"]["id"] == "debian" and len(rep["collectors"]["packages_dpkg"]) > 50
    assert statuses and statuses[-1]["status"] == "done"


def test_inspection_works_on_a_read_only_root_with_no_network(lab):
    rc, err, state, _, _ = inspect_in(lab, "readonly", INSPECT, flags=[*dk.LOCKED, "--read-only"], rm=True)
    assert rc == 0, err[-3000:]
    rep = report_of(state)
    c = rep["collectors"]
    assert c["os"]["id"] == "debian" and c["cpu"]["count"] >= 1 and c["memory"]["total_bytes"] > 0
    assert len(c["packages_dpkg"]) > 50 and any(i["name"] == "lo" for i in c["net_interfaces"])
    assert all(ins.allowed(s) for s in rep["sources"])


def test_a_host_that_links_an_allowed_name_to_a_secret_gets_it_refused(lab):
    """A hostile host turns /etc/os-release (normally Debian's link to ../usr/lib/os-release, another allowed file) into a link to
    /etc/shadow: the name is refused and listed, never read; the OS still comes from usr/lib/os-release and the rest goes on."""
    plant = "ln -sf /etc/shadow /etc/os-release && grep -q '^root:' /etc/shadow"
    rc, err, state, _, _ = inspect_in(lab, "hostile", f"{plant} && exec {INSPECT}", flags=dk.LOCKED, rm=True)
    assert rc == 0, err[-3000:]
    rep = report_of(state)
    assert rep["refused"] == ["etc/os-release"]
    assert rep["collectors"]["os"]["id"] == "debian" and "usr/lib/os-release" in rep["sources"]
    text = json.dumps(rep)
    assert "root:" not in text and "daemon:" not in text and "shadow" not in text


def test_inspection_says_nothing_on_the_network(lab):
    net = lab.network()
    c = dk.Sentinel(lab, net, lab.base)
    rc, err, state, m, _ = inspect_in(lab, "netquiet", INSPECT, flags=dk.LOCKED, net=net, host=20, rm=True)
    assert rc == 0, err[-3000:]
    assert dk.silent_violations(c.frames(), m, c, set(lab.macs)) == []
    assert report_of(state)["collectors"]["os"]["id"] == "debian"


def test_the_sentinel_catches_a_talker(lab):
    """Positive control: the same check flags a machine that opens a connection."""
    net = lab.network()
    c = dk.Sentinel(lab, net, lab.base)
    probe = f"import socket; s = socket.socket(); s.settimeout(2); s.connect_ex(('{net[1]}.99', 80)); s.close()"
    m = dk.Machine(lab, "talker", [dk.PY, "-I", "-B", "-c", probe], flags=dk.LOCKED, net=net, host=21, rm=True)
    assert m.finish()[0] == 0
    bad = dk.silent_violations(c.frames(), m, c, set(lab.macs))
    assert any(f["src_mac"] == m.mac.replace(":", "") and f["type"] == "0x806" and f["target"] == f"{net[1]}.99" for f in bad), bad
