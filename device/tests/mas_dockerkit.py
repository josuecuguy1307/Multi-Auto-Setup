"""Shared helpers for the EMULATED container tests (P3-READONLY first; D-035 snapshots, D-036 sentinel).

Machines get fixed IPs and MACs on an --internal network so every captured frame names its sender; snapshots are whole `docker export`s
fingerprinted without timestamps (D-022).
"""

from __future__ import annotations

import hashlib
import json
import secrets
import subprocess
import tarfile
import uuid
from pathlib import Path

import pytest

TIMEOUT = 300
MOUNT = "/media/mas"
PY = f"{MOUNT}/runtime/python/bin/python3.13"
NOV6 = ["--sysctl", "net.ipv6.conf.all.disable_ipv6=1", "--sysctl", "net.ipv6.conf.default.disable_ipv6=1"]
LOCKED = ["--cap-drop", "ALL", "--security-opt", "no-new-privileges"]


def docker_ok() -> None:
    try:
        r = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        pytest.fail(f"ERROR docker unavailable: {exc}")
    if r.returncode != 0 or not r.stdout.strip():
        pytest.fail("ERROR the Docker engine is not running (start Docker Desktop from the Start menu, HANDOFF §2 step 0)")


def medium_cache(tree: Path) -> Path:
    cands = [tree / "runs" / "medium-cache"]
    common = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=tree, capture_output=True,
                            text=True, timeout=30).stdout.strip()
    if common:
        cands.append(Path(common).parent / "runs" / "medium-cache")
    for c in cands:
        if c.is_dir():
            return c
    pytest.fail("ERROR medium cache missing: run `python device/medium/mas_medium_build.py fetch`")


def snapshot(cid: str) -> dict[str, tuple]:
    """Every entry of a stopped container's filesystem: path -> (type, mode, uid, gid, size, sha256 or link target)."""
    p = subprocess.Popen(["docker", "export", cid], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out = {}
    with tarfile.open(fileobj=p.stdout, mode="r|") as tf:
        for m in tf:
            name = m.name[2:] if m.name.startswith("./") else m.name
            path = "/" + name.strip("/")
            if m.isfile():
                h = hashlib.sha256()
                f = tf.extractfile(m)
                while chunk := f.read(1 << 20):
                    h.update(chunk)
                what = h.hexdigest()
            else:
                what = m.linkname if (m.issym() or m.islnk()) else ""
            out[path] = (m.type, m.mode & 0o7777, m.uid, m.gid, m.size if m.isfile() else 0, what)
    assert p.wait(timeout=TIMEOUT) == 0, p.stderr.read().decode("utf-8", "replace")
    return out


def diff(a: dict, b: dict) -> dict[str, str]:
    d = {p: "added" for p in b.keys() - a.keys()}
    d.update({p: "removed" for p in a.keys() - b.keys()})
    d.update({p: "changed" for p in a.keys() & b.keys() if a[p] != b[p]})
    return d


class Lab:
    """Containers and networks of one test module, removed at the end."""

    def __init__(self, image: str, medium: Path) -> None:
        self.image, self.medium = image, medium
        self.containers: list[str] = []
        self.networks: list[str] = []
        self.macs: set[str] = set()

    def network(self) -> tuple[str, str]:
        r = None
        for _ in range(20):
            name, prefix = f"mas-lab-{uuid.uuid4().hex[:8]}", f"10.232.{secrets.randbelow(250) + 1}"
            r = subprocess.run(["docker", "network", "create", "--internal", "--subnet", f"{prefix}.0/24", name], capture_output=True,
                               text=True, timeout=TIMEOUT)
            if r.returncode == 0:
                self.networks.append(name)
                return name, prefix
        pytest.fail("ERROR could not create an isolated network: " + (r.stderr if r else ""))

    def close(self) -> None:
        for cid in self.containers:
            subprocess.run(["docker", "rm", "-f", cid], capture_output=True, timeout=TIMEOUT)
        for net in self.networks:
            subprocess.run(["docker", "network", "rm", net], capture_output=True, timeout=TIMEOUT)


class Machine:
    """`docker run -i` of the clean image with the medium read-only; stdin/stdout carry JSON lines. Kept after exit unless rm=True."""

    def __init__(self, lab: Lab, role: str, args: list[str], *, mounts: list[str] = (), flags: list[str] = (),
                 net: tuple[str, str] | None = None, host: int = 0, rm: bool = False) -> None:
        self.cid = f"mas-lab-{role}-{uuid.uuid4().hex[:8]}"
        lab.containers.append(self.cid)
        netargs = ["--network", "none"]
        self.ip = self.mac = None
        if net is not None:
            self.ip = f"{net[1]}.{host}"
            self.mac = "02:00:0a:e8:%02x:%02x" % (int(net[1].split(".")[-1]), host)
            lab.macs.add(self.mac)
            netargs = ["--network", net[0], "--ip", self.ip, "--mac-address", self.mac]
        cmd = ["docker", "run", "-i", *(["--rm"] if rm else []), "--name", self.cid, *netargs, *NOV6, *flags,
               "-v", f"{lab.medium}:{MOUNT}:ro", *mounts, lab.image, *args]
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def line(self) -> dict | None:
        raw = self.p.stdout.readline()
        return json.loads(raw) if raw else None

    def send(self, msg: dict) -> None:
        self.p.stdin.write(json.dumps(msg).encode() + b"\n")
        self.p.stdin.flush()

    def finish(self) -> tuple[int, str, bytes]:
        try:
            self.p.stdin.close()
        except OSError:
            pass
        out = self.p.stdout.read()
        rc = self.p.wait(timeout=TIMEOUT)
        err = self.p.stderr.read().decode("utf-8", "replace")
        return rc, err, out


CANARY = r'''
import json, select, socket, sys
s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.ntohs(3))
frames = []
print("ready", flush=True)
while True:
    r, _, _ = select.select([s, sys.stdin], [], [], 1.0)
    if sys.stdin in r and not sys.stdin.buffer.read1(4096):
        break
    if s in r:
        data, addr = s.recvfrom(65535)
        if addr[2] == socket.PACKET_OUTGOING:
            continue
        et = int.from_bytes(data[12:14], "big")
        f = {"type": hex(et), "dst_mac": data[0:6].hex(), "src_mac": data[6:12].hex()}
        if et == 0x0806:
            f.update(op=int.from_bytes(data[20:22], "big"), sender=socket.inet_ntoa(data[28:32]), target=socket.inet_ntoa(data[38:42]))
        elif et == 0x0800:
            f.update(src=socket.inet_ntoa(data[26:30]), dst=socket.inet_ntoa(data[30:34]), proto=data[23])
        elif et == 0x86DD:
            f.update(src6=socket.inet_ntop(socket.AF_INET6, data[22:38]), dst6=socket.inet_ntop(socket.AF_INET6, data[38:54]))
        frames.append(f)
print(json.dumps(frames), flush=True)
'''


class Sentinel:
    """A machine that only captures the frames it receives (CAP_NET_RAW only here) and reports them when its stdin closes."""

    def __init__(self, lab: Lab, net: tuple[str, str], scratch: Path, host: int = 12) -> None:
        d = scratch / f"canary-{uuid.uuid4().hex[:6]}"
        d.mkdir(parents=True)
        (d / "canary.py").write_text(CANARY, encoding="ascii")
        self.m = Machine(lab, "canary", [PY, "-I", "-B", "/canary/canary.py"], mounts=["-v", f"{d}:/canary:ro"],
                         flags=["--cap-drop", "ALL", "--cap-add", "NET_RAW"], net=net, host=host, rm=True)
        assert self.m.p.stdout.readline().strip() == b"ready", self.m.p.stderr.read()[-2000:]
        self.ip, self.mac = self.m.ip, self.m.mac

    def frames(self) -> list[dict]:
        rc, err, out = self.m.finish()
        assert rc == 0, err[-2000:]
        return json.loads(out.decode().strip().splitlines()[-1])


def silent_violations(frames: list[dict], quiet, sentinel: Sentinel, machines: set[str]) -> list[dict]:
    """Frames that show `quiet` (a machine that must not use the network) or any other test machine saying anything.

    Allowed: the kernel's own ARP announcement of a machine's address when its interface comes up (sender == target == its IP), and
    IPv6 link-local multicast from MACs of no test machine (Docker's bridge in its VM, D-036)."""
    mine = {m.replace(":", "") for m in machines}
    bad = []
    for f in frames:
        src = f["src_mac"]
        if src == sentinel.mac.replace(":", ""):
            continue
        if src in mine:
            announce = f["type"] == "0x806" and f.get("op") == 1 and f.get("sender") == f.get("target") and \
                src == quiet.mac.replace(":", "") and f.get("sender") == quiet.ip
            if not announce:
                bad.append(f)
        elif not (f["type"] == "0x86dd" and f.get("dst6", "").startswith("ff02:")):
            bad.append(f)
    return bad
