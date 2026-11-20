"""Joining a fleet from a host, started by the host's owner: `sh <medium>/launch.sh fleet-join --root ... --state ... --target ...
--coordinator <IP>:<port>` (D-021, D-036).

It opens exactly one TCP connection, to the literal address the owner typed (no discovery, no name lookup, no listening socket), and
drives the device's gate as a child process over the gate's own JSON-lines interface, exactly as the model does: it holds no key and no
write authority of its own. Each gate action still needs a press.

    1. the gate starts (the owner sees the session terms first)
    2. the coordinator offers a plan; the node checks it (mas_fleet.plan) before asking anybody anything
    3. the host's owner must type "JOIN <fleet>"; otherwise nothing is sent and the node exits
    4. the device owner presses for JOIN_FLEET (the coordinator checks the token against the key it pins)
    5. each plan step is one gate write: one press each
    6. on "leave", each step is undone through the gate (one press each) and the coordinator drops the node

Owner and key I/O (emulated in v0 on this process's stdin/stdout, one JSON object per line):
    out {"owner_prompt": text}   in {"owner": typed text}
    out {"key_request": {...}}   in {"token": hex}
    out {"status": "joined" | "setup_done" | "left" | "stayed" | "refused"}   in, after setup_done, {"cmd": "leave" | "stay"}
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import socket
import subprocess
import sys

from mas_device import approval as ap
from mas_fleet import plan as fp
from mas_fleet.wire import Conn, WireError


class Stop(Exception):
    def __init__(self, code: int, why: str) -> None:
        super().__init__(why)
        self.code, self.why = code, why


def _out(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg, sort_keys=True) + "\n")
    sys.stdout.flush()


def _in(field: str):
    line = sys.stdin.buffer.readline()
    if not line:
        raise Stop(6, "the owner/key channel closed")
    try:
        msg = json.loads(line)
    except ValueError:
        raise Stop(6, "the owner/key channel sent something that is not JSON") from None
    if not isinstance(msg, dict) or set(msg) != {field}:
        raise Stop(6, f"expected {field!r} from the owner/key channel")
    return msg[field]


class Gate:
    """The device's gate as a child process; this module speaks to it like the model does."""

    def __init__(self, runtime: str, root: str, state: str, target: str) -> None:
        cmd = [sys.executable, "-I", "-B", os.path.join(runtime, "launch.py"), "--root", root, "--state", state, "--target", target]
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)  # stderr: the owner's console (terms first)
        self.n = 0

    def call(self, method: str, **params):
        self.n += 1
        self.p.stdin.write(json.dumps({"id": self.n, "method": method, "params": params}).encode("ascii") + b"\n")
        self.p.stdin.flush()
        line = self.p.stdout.readline()
        if not line:
            raise Stop(7, "the gate stopped")
        r = json.loads(line)
        if not r.get("ok"):
            raise Stop(7, f"the gate refused {method}: {r.get('error', {}).get('code')}")
        return r["result"]

    def close(self) -> int:
        try:
            self.p.stdin.close()
        except OSError:
            pass
        return self.p.wait(timeout=120)


def _press(kr: dict) -> str:
    _out({"key_request": kr})
    token = _in("token")
    if not isinstance(token, str):
        raise Stop(6, "a token is hex text")
    return token


def _expect(conn: Conn, t: str) -> dict:
    msg = conn.recv()
    if msg is None:
        raise Stop(4, "the coordinator closed the connection")
    if msg.get("t") == "refused":
        raise Stop(4, f"the coordinator refused: {msg.get('reason')}")
    if msg.get("t") != t:
        raise Stop(4, f"the coordinator sent {msg.get('t')!r}, expected {t!r}")
    return msg


def join(conn: Conn, gate: Gate, target: str) -> int:
    conn.send({"t": "hello", "target": target})
    offer = _expect(conn, "offer")
    plan = offer.get("plan")
    try:
        steps = fp.validate(plan)
    except fp.PlanRefused as exc:
        raise Stop(5, f"the offered plan was refused: {exc}") from None
    if offer.get("plan_hash") != fp.plan_hash(plan) or offer.get("fleet") != plan["fleet"] or offer.get("coordinator") != plan["coordinator"]:
        raise Stop(5, "the offer does not match its plan")
    fleet, coordinator = plan["fleet"], plan["coordinator"]
    expected = f"JOIN {fleet}"
    _out({"owner_prompt": f"Type {expected} to let this computer join the fleet '{fleet}' of {coordinator}"})
    if _in("owner") != expected:
        raise Stop(3, f"the host's owner did not confirm joining the fleet '{fleet}' (nothing was sent or written)")
    conn.send({"t": "join", "owner_typed": expected, "plan_hash": offer["plan_hash"]})
    kr = _expect(conn, "key_request")["key_request"]
    conn.send({"t": "token", "token": _press(kr)})
    _expect(conn, "joined")
    _out({"status": "joined"})
    done = []
    for step, files in zip(plan["steps"], steps):
        action = gate.call("plan_write", files=step["files"])["action"]
        token = _press(gate.call("begin", action=action)["key_request"])
        gate.call("execute", token=token, action=action)
        done.append(ap.action_hash(action).hex())
    conn.send({"t": "setup_done", "actions": done})
    _expect(conn, "ok")
    _out({"status": "setup_done"})
    cmd = _in("cmd")
    if cmd == "stay":
        _out({"status": "stayed"})
        return 0
    if cmd != "leave":
        raise Stop(6, "after setup the owner says leave or stay")
    for ah in reversed(done):
        undo = gate.call("undo_for", action_hash=ah)["undo"]
        token = _press(gate.call("begin_undo", undo=undo)["key_request"])
        gate.call("execute_undo", token=token, undo=undo)
    conn.send({"t": "leave"})
    _expect(conn, "left")
    _out({"status": "left"})
    return 0


def main(argv: list[str], *, runtime: str) -> int:
    p = argparse.ArgumentParser(prog="mas fleet-join", description="Join a fleet, started by this computer's owner.")
    for name in ("--root", "--state", "--target", "--coordinator"):
        p.add_argument(name, required=True)
    a = p.parse_args(argv)
    host, _, port = a.coordinator.rpartition(":")
    try:
        ip = ipaddress.ip_address(host)
        port_n = int(port)
    except ValueError:
        p.error("--coordinator is a literal IP address and a port (no names: nothing is looked up)")
    if ip.is_multicast or ip.is_unspecified or ip.is_reserved or not 0 < port_n < 65536:  # is_reserved covers the broadcast address
        p.error("--coordinator must be one machine")
    if not fp.NAME.fullmatch(a.target):
        p.error("--target is a plain name")
    gate = Gate(runtime, a.root, a.state, a.target)
    conn = None
    try:
        conn = Conn(socket.create_connection((str(ip), port_n), timeout=300))
        return join(conn, gate, a.target)
    except Stop as s:
        sys.stderr.write(f"ERROR {s.why}\n")
        _out({"status": "refused"})
        return s.code
    except (WireError, OSError) as exc:
        sys.stderr.write(f"ERROR fleet connection: {type(exc).__name__}: {exc}\n")
        _out({"status": "refused"})
        return 4
    finally:
        if conn is not None:
            conn.close()
        gate.close()
