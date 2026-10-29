#!/usr/bin/env python3
"""LINUX-CONTAINER verification run: the coordinator runs on this machine (the Docker host); the NODE runs inside a Linux container (Docker on this machine) and pairs with it, then sends
heartbeats. The container is a stand-in for a Linux computer on the same host: it is NOT a second physical machine. Needs Docker and the image python:3.12-alpine already present;
installs the node's dependencies (from PyPI) inside the throwaway container only; the repository is mounted read-only."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "conformance")]

from mas_procs import Proc, free_port  # noqa: E402

NAME = "mas-node-linux-test"
IMAGE = "python:3.12-alpine"
DEPS = ["pydantic", "pyyaml", "psutil", "httpx", "cryptography", "spake2"]


def sh(*argv: str, timeout: float = 300, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, input=stdin)


def main(host_alias: str = "host.docker.internal", bind: str = "127.0.0.1") -> int:
    tmp = Path(tempfile.mkdtemp(prefix="mas-container-"))
    res: dict = {"host_alias": host_alias, "bind": bind}
    sh("docker", "rm", "-f", NAME)
    started = sh("docker", "run", "-d", "--rm", "--name", NAME, "-v", f"{ROOT}:/repo:ro", "-e", "PYTHONPATH=/repo/src", "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "HOME=/tmp", IMAGE, "sleep", "900")
    if started.returncode != 0:
        print(json.dumps({"error": "container did not start", "detail": started.stderr[-300:]}))
        return 1
    procs = []
    try:
        pip = sh("docker", "exec", NAME, "pip", "install", "--quiet", "--disable-pip-version-check", *DEPS, timeout=600)
        res["pip_install_ok"] = pip.returncode == 0
        if pip.returncode != 0:
            res["pip_error"] = pip.stderr[-400:]
            print(json.dumps(res, indent=1))
            return 1
        res["container"] = sh("docker", "exec", NAME, "python", "-c", "import platform;print(platform.system(), platform.machine(), platform.python_version())").stdout.strip()
        port = free_port()
        cstate = tmp / "coordinator-state"
        coord = Proc(["--state-dir", str(cstate), "coordinator", "start", "--bind", bind, "--port", str(port), "--idle-timeout", "20"], {})
        procs.append(coord)
        coord.wait_for("coordinator listening")
        owner = Proc(["--state-dir", str(cstate), "pair"], {})
        procs.append(owner)
        code = owner.wait_for("Pairing code:").split(":")[1].strip()
        pr = subprocess.Popen(["docker", "exec", "-i", NAME, "python", "-m", "envrec_mas", "--state-dir", "/state", "pair", f"{host_alias}:{port}", "--name", "linux-container"],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        pr.stdin.write(code + "\n")
        pr.stdin.flush()
        owner.wait_for("Compare the fingerprint")
        owner.send("yes")  # the owner's yes, on the coordinator
        pout, _ = pr.communicate(timeout=60)
        res["node_pair_output"] = pout.strip().splitlines()[-2:]
        owner.wait_for("Paired.")
        sh("docker", "exec", "-d", NAME, "sh", "-c", "python -m envrec_mas --state-dir /state node run --interval 0.5 > /tmp/node.log 2>&1")
        end = time.monotonic() + 25
        listing = ""
        while time.monotonic() < end:
            n = Proc(["--state-dir", str(cstate), "nodes"], {})
            n.p.wait(15)
            time.sleep(0.1)
            listing = "\n".join(n.lines)
            if "s ago" in listing:
                break
            time.sleep(0.7)
        res["nodes_listing"] = listing
        probe = ("import psutil\nfor p in psutil.process_iter(['cmdline']):\n"
                 "  c=' '.join(p.info['cmdline'] or [])\n  if 'envrec_mas' in c and ' node run' in c:\n"
                 "    print(p.pid, sorted((x.laddr.ip,x.laddr.port) for x in p.net_connections(kind='inet') if x.status=='LISTEN'), [x.status for x in p.net_connections(kind='inet')])\n")
        res["node_process_listening(pid, listeners, connection states)"] = sh("docker", "exec", NAME, "python", "-c", probe).stdout.strip()
        res["node_log_tail"] = sh("docker", "exec", NAME, "tail", "-n", "3", "/tmp/node.log").stdout.strip()
        res["coordinator_listening"] = coord.listening()
    finally:
        res["exit_codes"] = [p.stop() for p in reversed(procs)]
        sh("docker", "rm", "-f", NAME)
    print(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
