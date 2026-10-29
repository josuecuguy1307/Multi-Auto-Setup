"""`envrec lab up|down|discover`: a Docker bridge network with nodes whose contents are known by construction.

Nodes: an mDNS advertiser (a printer service and one strange service type), an SSDP responder (a UPnP media renderer), an sshd, an HTTP server with a banner,
a silent host, and a host with an unknown service on a strange port. What discovery must find is listed in EXPECTED: that list is the ground truth.
The lab network is only reachable from inside Docker, so `discover` runs the scanner in a container attached to it."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NET, SUBNET = "envrec-lab", "172.30.77.0/24"
IMG_NODE, IMG_SCANNER, IMG_SSHD = "envrec-lab-node", "envrec-lab-scanner", "envrec-fixture-sshd"
SCANNER_IP = "172.30.77.2"


@dataclass(frozen=True)
class Node:
    name: str
    ip: str
    role: str  # mdns | ssdp | http | unknown | silent | sshd


NODES = (Node("envrec-lab-mdns", "172.30.77.11", "mdns"), Node("envrec-lab-ssdp", "172.30.77.12", "ssdp"), Node("envrec-lab-sshd", "172.30.77.13", "sshd"),
         Node("envrec-lab-http", "172.30.77.14", "http"), Node("envrec-lab-silent", "172.30.77.15", "silent"), Node("envrec-lab-unknown", "172.30.77.16", "unknown"))

# Ground truth by construction. `hosts` must be found as live hosts; the rest are the observations each adapter must make.
# The lab's contents are fixture data (tests/docker/lab/expected.json, next to the node that serves them), not code: the hosts follow from NODES, the rest is read from the file.
_FIXTURE = json.loads((ROOT / "tests/docker/lab/expected.json").read_text())
EXPECTED = {
    "hosts": [n.ip for n in NODES],
    **_FIXTURE,
    "banners": {ip: tuple(pair) for ip, pair in _FIXTURE["banners"].items()},
    "unknown_service": tuple(_FIXTURE["unknown_service"]),
}


def sh(*argv: str, timeout: float = 120, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout, check=check)


def docker_ready() -> bool:
    try:
        return sh("docker", "info", timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def have_image(name: str) -> bool:
    return bool(sh("docker", "images", "-q", name).stdout.strip())


def build_images() -> None:
    sh("docker", "build", "-q", "-t", IMG_NODE, "-f", str(ROOT / "tests/docker/lab/Dockerfile.node"), str(ROOT / "tests/docker/lab"), timeout=900, check=True)
    sh("docker", "build", "-q", "-t", IMG_SCANNER, "-f", str(ROOT / "tests/docker/lab/Dockerfile.scanner"), str(ROOT / "tests/docker/lab"), timeout=900, check=True)


def up() -> list[str]:
    for img in (IMG_NODE, IMG_SCANNER):
        if not have_image(img):
            build_images()
            break
    down()
    sh("docker", "network", "create", "--subnet", SUBNET, NET, check=True)
    started = []
    for n in NODES:
        if n.role == "sshd":
            argv = ["docker", "run", "-d", "--rm", "--name", n.name, "--network", NET, "--ip", n.ip, "-e", "PUBKEY=ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPL lab", IMG_SSHD]
        elif n.role == "silent":
            argv = ["docker", "run", "-d", "--rm", "--name", n.name, "--network", NET, "--ip", n.ip, IMG_NODE, "silent", n.ip]
        else:
            argv = ["docker", "run", "-d", "--rm", "--name", n.name, "--network", NET, "--ip", n.ip, IMG_NODE, n.role, n.ip]
        r = sh(*argv)
        if r.returncode != 0:
            raise RuntimeError(f"could not start {n.name}: {r.stderr.strip()[:200]}")
        started.append(n.name)
    return started


def down() -> None:
    for n in NODES:
        sh("docker", "rm", "-f", n.name)
    sh("docker", "network", "rm", NET)


def scanner_argv(network: str, command: list[str], ip: str | None = None) -> list[str]:
    """The discovery controller as a container on `network`. The skill scripts it runs are sandboxed by the host's Docker daemon (socket mounted), so the repository is
    mounted at the same absolute path as on the host: the sandbox's read-only bind mounts name host paths."""
    return (["docker", "run", "--rm", "--network", network] + (["--ip", ip] if ip else []) +
            ["-v", f"{ROOT}:{ROOT}:ro", "-w", str(ROOT), "-e", f"PYTHONPATH={ROOT}/src", "-v", "/var/run/docker.sock:/var/run/docker.sock", IMG_SCANNER] + command)


def discover(extra: list[str] | None = None, timeout: float = 240) -> subprocess.CompletedProcess:
    """Run `envrec discover` inside the lab network. The lab scan is auto-approved (it only works when the scope is exactly the lab subnet)."""
    return sh(*scanner_argv(NET, ["python", "-m", "envrec", "discover", "--ring", "2", "--active", "--lab-approve", "--json", "--runs-dir", "/tmp/runs"] + (extra or []), ip=SCANNER_IP), timeout=timeout)


def cmd_lab(args) -> int:
    if not docker_ready():
        print("Docker is not running.", file=sys.stderr)
        return 2
    if args.lab_cmd == "up":
        print("started:", ", ".join(up()))
    elif args.lab_cmd == "down":
        down()
        print("lab removed")
    elif args.lab_cmd == "discover":
        r = discover()
        print(r.stdout)
        print(r.stderr[-800:], file=sys.stderr)
        return r.returncode
    return 0


def add_lab_commands(sub) -> None:
    p = sub.add_parser("lab", help="a Docker network of nodes with known contents, for testing discovery")
    ls = p.add_subparsers(dest="lab_cmd", required=True)
    for name, h in (("up", "create the lab network and start the nodes"), ("down", "remove the lab"), ("discover", "run discovery from inside the lab and print the JSON result")):
        ls.add_parser(name, help=h)
    p.set_defaults(func=cmd_lab)
