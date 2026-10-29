#!/usr/bin/env python3
"""LINUX-CONTAINER verification of discovery: TWO Linux containers on one user-defined Docker network (a stand-in for two Linux computers on a LAN, NOT two physical machines). The
coordinator container answers the node container's real UDP broadcast; the node joins, the owner's pick and yes are typed by this script on the coordinator side. Needs Docker and the
image python:3.12-alpine; installs the dependencies from PyPI inside the throwaway containers only; the repository is mounted read-only."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NET, IMAGE = "mas-disc-net", "python:3.12-alpine"
DEPS = ["pydantic", "pyyaml", "psutil", "httpx", "cryptography", "spake2"]


def sh(*a: str, timeout: float = 600, stdin: str | None = None):
    return subprocess.run(a, capture_output=True, text=True, timeout=timeout, input=stdin)


def mas(name: str, *args: str) -> list[str]:
    return ["docker", "exec", "-i", name, "python", "-m", "envrec_mas", "--state-dir", "/state", *args]


def main() -> int:
    res: dict = {}
    for n in ("mas-coord", "mas-node"):
        sh("docker", "rm", "-f", n)
    sh("docker", "network", "rm", NET)
    sh("docker", "network", "create", NET)
    try:
        for n in ("mas-coord", "mas-node"):
            r = sh("docker", "run", "-d", "--rm", "--name", n, "--network", NET, "-v", f"{ROOT}:/repo:ro", "-e", "PYTHONPATH=/repo/src", "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "HOME=/tmp", IMAGE, "sleep", "900")
            assert r.returncode == 0, r.stderr
            p = sh("docker", "exec", n, "pip", "install", "--quiet", "--disable-pip-version-check", *DEPS)
            assert p.returncode == 0, p.stderr[-300:]
        ip = sh("docker", "exec", "mas-coord", "hostname", "-i").stdout.strip()
        res["coordinator_container_ip"] = ip
        res["container"] = sh("docker", "exec", "mas-node", "python", "-c", "import platform;print(platform.system(), platform.machine())").stdout.strip()
        sh("docker", "exec", "-d", "mas-coord", "sh", "-c", f"python -m envrec_mas --state-dir /state coordinator start --bind {ip} --port 7443 --discovery-port 7444 --name linux-coordinator > /tmp/coord.log 2>&1")
        time.sleep(4)
        res["coordinator_log"] = sh("docker", "exec", "mas-coord", "cat", "/tmp/coord.log").stdout.strip().splitlines()[:2]
        node = subprocess.Popen(mas("mas-node", "join", "--name", "linux-node"), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        node.stdin.write("1\n")
        node.stdin.flush()
        wid = ""
        for _ in range(40):
            w = sh(*mas("mas-coord", "waiting")).stdout.splitlines()
            rows = [l for l in w if l.startswith("w")]
            if rows:
                wid = rows[0].split()[0]
                res["waiting_listing"] = w[:2]
                break
            time.sleep(0.5)
        assert wid, "no waiting machine appeared"
        owner = subprocess.Popen(mas("mas-coord", "pair", "--node", wid), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        code = ""
        for line in owner.stdout:
            if "Pairing code:" in line:
                code = line.split(":")[1].strip()
                break
        node.stdin.write(code + "\n")
        node.stdin.flush()
        for line in owner.stdout:
            if "Accept this computer" in line or "Compare the fingerprint" in line:
                owner.stdin.write("yes\n")
                owner.stdin.flush()
                break
        nout, _ = node.communicate(timeout=60)
        res["node_output"] = nout.strip().splitlines()[-3:]
        sh("docker", "exec", "-d", "mas-node", "sh", "-c", "python -m envrec_mas --state-dir /state node run --interval 0.5 > /tmp/node.log 2>&1")
        time.sleep(5)
        res["mas_nodes"] = sh(*mas("mas-coord", "nodes")).stdout.strip().splitlines()
        probe = "import psutil\nfor p in psutil.process_iter(['cmdline']):\n  c=' '.join(p.info['cmdline'] or [])\n  if ' node run' in c: print(sorted((x.laddr.ip,x.laddr.port) for x in p.net_connections(kind='inet') if x.status=='LISTEN' or (x.type==2 and not x.raddr)))\n"
        res["node_listening_and_udp"] = sh("docker", "exec", "mas-node", "python", "-c", probe).stdout.strip()
        cprobe = probe.replace("' node run'", "'coordinator start'")
        res["coordinator_listening_and_udp"] = sh("docker", "exec", "mas-coord", "python", "-c", cprobe).stdout.strip()
    finally:
        for n in ("mas-coord", "mas-node"):
            sh("docker", "rm", "-f", n)
        sh("docker", "network", "rm", NET)
    print(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
