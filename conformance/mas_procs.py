"""Run the coordinator and a node as real operating-system processes on this machine, drive them through the `mas` commands exactly as the owner would, and read what each
process listens on. Used by conformance N4 and by the local verification run. SIMULATED-LOCAL: real processes and real TLS sockets, but one machine, over loopback."""
from __future__ import annotations

import ipaddress
import os
import queue
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parent.parent


class Proc:
    def __init__(self, args: list[str], env: dict) -> None:
        self.p = subprocess.Popen([sys.executable, "-u", "-m", "envrec_mas", *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                                  env={**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1", **env}, cwd=str(ROOT))
        self.lines: list[str] = []
        self._q: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        for ln in self.p.stdout:  # type: ignore[union-attr]
            self.lines.append(ln.rstrip("\n"))
            self._q.put(ln)

    def wait_for(self, text: str, timeout: float = 20.0) -> str:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for ln in self.lines:
                if text in ln:
                    return ln
            if self.p.poll() is not None and not any(text in ln for ln in self.lines):
                break
            time.sleep(0.05)
        raise TimeoutError(f"{text!r} not seen; output so far: {self.lines[-8:]}")

    def send(self, text: str) -> None:
        self.p.stdin.write(text + "\n")  # type: ignore[union-attr]
        self.p.stdin.flush()  # type: ignore[union-attr]

    def listening(self) -> list[tuple[str, int]]:
        return listening_of(self.p.pid)

    def stop(self) -> int:
        if self.p.poll() is None:
            self.p.terminate()
            try:
                self.p.wait(10)
            except subprocess.TimeoutExpired:
                self.p.kill()
        return self.p.returncode


def listening_of(pid: int) -> list[tuple[str, int]]:
    """The listening sockets of one process (TCP listeners and bound UDP sockets, any address)."""
    conns = psutil.Process(pid).net_connections(kind="inet")
    return sorted((c.laddr.ip, c.laddr.port) for c in conns if c.status == psutil.CONN_LISTEN or (c.type == socket.SOCK_DGRAM and not c.raddr))


def socket_verdict(listening: list[tuple[str, int]], node_host: str, node_port: int) -> tuple[bool, str]:
    """The coordinator's sockets at runtime (docs/NETWORK_CONTRACT.md 1.1 and 1.7): exactly ONE at the address and port the owner configured for nodes, and at most ONE other, which must be
    loopback. Any second socket that is not loopback (a wildcard, a LAN address) fails, and so does a second loopback one."""
    node_side = [x for x in listening if x == (node_host, node_port)]
    others = [x for x in listening if x != (node_host, node_port)]
    problems = []
    if len(node_side) != 1:
        problems.append(f"expected exactly 1 socket at {node_host}:{node_port}, saw {len(node_side)}")
    for host, port in others:
        try:
            loop = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loop = False
        if not loop:
            problems.append(f"a second socket that is not loopback: {host}:{port}")
    if len([1 for h, _ in others if _is_loopback(h)]) > 1:
        problems.append("more than one loopback socket besides the node-facing one")
    return not problems, "; ".join(problems) or f"1 node-facing socket and {len(others)} loopback web socket(s)"


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def run_local_network(tmp: Path, *, heartbeats: int = 2, host: str = "127.0.0.1", web: bool = False) -> dict:
    """coordinator process, `mas pair` on it, `mas pair HOST:PORT` on the node, the owner's yes, `mas node run`, heartbeats; then the evidence. Always stops what it started."""
    port = free_port()
    cstate, nstate = tmp / "coordinator-state", tmp / "node-state"
    coord = Proc(["--state-dir", str(cstate), "coordinator", "start", "--bind", host, "--port", str(port), "--idle-timeout", "20", *(["--web"] if web else [])], {})
    procs = [coord]
    out: dict = {"port": port}
    try:
        coord.wait_for("coordinator listening")
        if web:
            out["web_link"] = coord.wait_for("http://127.0.0.1:").strip()  # printed once, to the owner's terminal
        owner = Proc(["--state-dir", str(cstate), "pair"], {})
        procs.append(owner)
        code = owner.wait_for("Pairing code:").split(":")[1].strip()
        node_pair = Proc(["--state-dir", str(nstate), "pair", f"{host}:{port}", "--name", "test-node"], {})
        procs.append(node_pair)
        node_pair.send(code)
        owner.wait_for("Compare the fingerprint")  # the prompt itself has no newline, so it is not a line
        owner.send("yes")  # the OWNER's yes, typed on the coordinator's side
        owner.wait_for("Paired.")
        node_pair.wait_for("Paired with the coordinator")
        node = Proc(["--state-dir", str(nstate), "node", "run", "--interval", "0.3"], {})
        procs.append(node)
        node.wait_for("it listens on nothing")
        end = time.monotonic() + 20
        listing = ""
        while time.monotonic() < end:
            nodes = Proc(["--state-dir", str(cstate), "nodes"], {})
            nodes.p.wait(15)
            time.sleep(0.1)
            listing = "\n".join(nodes.lines)
            if "s ago" in listing:
                break
            time.sleep(0.5)
        out["nodes_listing"] = listing
        time.sleep(0.3 * heartbeats + 0.5)
        out["coordinator_listening"] = coord.listening()
        out["node_listening"] = node.listening()
        out["coordinator_pid"], out["node_pid"] = coord.p.pid, node.p.pid
    finally:
        out["exit_codes"] = [p.stop() for p in reversed(procs)]
    return out
