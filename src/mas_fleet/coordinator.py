"""The fleet coordinator: runs on the device owner's own machine, from the medium (D-036). It only listens; it never dials anyone.

Per connection (one node), a small state machine:

    node: hello {target}            -> offer {fleet, coordinator, plan, plan_hash}
    node: join {owner_typed, plan_hash}
          the host owner must have typed exactly "JOIN <fleet>" (D-021); the plan must be the one offered
                                    -> key_request (class JOIN_FLEET, bound to target, plan hash and the typed text)
    node: token {hex}               the key this medium is paired with signed it after a press (approval.Gate, pinned key only)
                                    -> joined; roster gains the node; the log keeps the typed text next to the key's counter
    node: setup_done {actions}      -> ok
    node: leave                     -> left; roster drops the node

Anything else is refused and logged. The roster and the log live in the coordinator's own folder.
"""

from __future__ import annotations

import argparse
import base64
import ipaddress
import json
import os
import socket
import sys
from pathlib import Path

from mas_device import approval as ap
from mas_device import pairing as pr
from mas_device.log import AuditLog
from mas_fleet import plan as fp
from mas_fleet.wire import Conn, WireError

SHAPES = {"hello": {"t", "target"}, "join": {"t", "owner_typed", "plan_hash"}, "token": {"t", "token"},
          "setup_done": {"t", "actions"}, "leave": {"t"}}
MAX_TYPED = 200


def build_plan(fleet: str, target: str, payload: Path, paired: pr.Pairing) -> dict:
    payload = Path(payload)
    files = {}
    for f in sorted(p for p in payload.rglob("*") if p.is_file()):
        files[".mas/node/" + f.relative_to(payload).as_posix()] = base64.b64encode(f.read_bytes()).decode("ascii")
    node = {"coordinator": target, "fleet": fleet, "key_id": paired.key_id.hex(), "worker_can_deploy_further": False}
    node_json = (json.dumps(node, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
    plan = {"v": 1, "fleet": fleet, "coordinator": target,
            "steps": [{"title": "install the MAS node sources (not run in v0)", "files": files},
                      {"title": "configure the node for this fleet", "files": {".mas/node.json": base64.b64encode(node_json).decode()}}]}
    fp.validate(plan)  # never offer a plan a node would refuse
    return plan


class Refusal(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Coordinator:
    def __init__(self, *, pairing: Path, state: Path, fleet: str, target: str, payload: Path) -> None:
        for name, v in (("fleet", fleet), ("target", target)):
            if not fp.NAME.fullmatch(v):
                raise ValueError(f"{name} is a plain name")
        self.paired = pr.load(pairing)
        self.fleet, self.target = fleet, target
        self.state = Path(state)
        self.log = AuditLog.open(self.state / "audit.log")
        self.plan = build_plan(fleet, target, payload, self.paired)
        self.plan_hash = fp.plan_hash(self.plan)
        _, session = pr.new_session(self.paired)
        self.approvals = ap.Gate({self.paired.key_id: self.paired.key_pub}, session=session)  # the paired key and no other
        roster = self.state / "roster.json"
        self.roster = json.loads(roster.read_text(encoding="utf-8")) if roster.exists() else {"fleet": fleet, "nodes": {}}
        self._save_roster()
        self.log.append("coordinator.started", {"fleet": fleet, "target": target, "key_id": self.paired.key_id.hex(),
                                                "plan_hash": self.plan_hash})
        self.new_connection()

    def new_connection(self) -> None:
        self.stage, self.node, self.action = "start", None, None

    def _save_roster(self) -> None:
        tmp = self.state / "roster.json.tmp"
        tmp.write_bytes((json.dumps(self.roster, sort_keys=True, indent=1) + "\n").encode("ascii"))
        os.replace(tmp, self.state / "roster.json")

    def handle(self, msg: dict) -> list[dict]:
        try:
            return self._handle(msg)
        except Refusal as r:
            self.log.append("fleet.join_refused" if self.stage in ("offered", "asked") else "fleet.refused",
                            {"target": self.node, "reason": r.reason, "stage": self.stage})
            self.stage = "closed"
            return [{"t": "refused", "reason": r.reason}]

    def _handle(self, msg: dict) -> list[dict]:
        t = msg.get("t") if isinstance(msg, dict) else None
        if t not in SHAPES or set(msg) != SHAPES[t]:
            raise Refusal("malformed")
        if t == "hello":
            if not isinstance(msg["target"], str) or not fp.NAME.fullmatch(msg["target"]):
                raise Refusal("malformed")
            if self.stage != "start":
                raise Refusal("out_of_order")
            self.node, self.stage = msg["target"], "offered"
            return [{"t": "offer", "fleet": self.fleet, "coordinator": self.target, "plan": self.plan, "plan_hash": self.plan_hash}]
        if t == "join":
            if not isinstance(msg["owner_typed"], str) or len(msg["owner_typed"]) > MAX_TYPED or not isinstance(msg["plan_hash"], str):
                raise Refusal("malformed")
            if self.stage != "offered":
                raise Refusal("out_of_order")
            if msg["plan_hash"] != self.plan_hash:
                raise Refusal("plan_changed")
            if msg["owner_typed"] != f"JOIN {self.fleet}":
                raise Refusal("owner_not_confirmed")
            self.action = {"kind": "join_fleet", "fleet": self.fleet, "coordinator": self.target, "target": self.node,
                           "plan_hash": self.plan_hash, "owner_typed": msg["owner_typed"],
                           "undo": {"kind": "leave_fleet", "fleet": self.fleet, "target": self.node}}
            p = self.approvals.begin(self.action, ap.Class.JOIN_FLEET)
            self.log.append("approval.requested", {"class": "JOIN_FLEET", "target": self.node, "action_hash": p.action_hash.hex(),
                                                   "summary": p.summary.decode("ascii").rstrip()})
            self.stage = "asked"
            return [{"t": "key_request", "key_request": {
                "session_id": p.session.hex(), "nonce": p.nonce.hex(), "action_hash": p.action_hash.hex(), "undo_hash": p.undo_hash.hex(),
                "summary": p.summary.hex(), "cls": int(p.cls), "press_window_s": p.press_window_s}}]
        if t == "token":
            if self.stage != "asked":
                raise Refusal("out_of_order")
            try:
                token = bytes.fromhex(msg["token"]) if isinstance(msg["token"], str) else None
            except ValueError:
                token = None
            if token is None:
                raise Refusal("malformed")
            try:
                a = self.approvals.consume(token, self.action, cls=ap.Class.JOIN_FLEET)
            except ap.Rejected as exc:
                raise Refusal(exc.code) from None
            self.log.append("fleet.joined", {"target": self.node, "owner_typed": self.action["owner_typed"], "key_id": a.key_id.hex(),
                                             "counter": a.counter, "plan_hash": self.plan_hash})
            self.roster["nodes"][self.node] = {"key_id": a.key_id.hex(), "counter": a.counter, "plan_hash": self.plan_hash}
            self._save_roster()
            self.stage = "joined"
            return [{"t": "joined"}]
        if t == "setup_done":
            acts = msg["actions"]
            if not isinstance(acts, list) or not all(isinstance(x, str) and len(x) == 64 for x in acts):
                raise Refusal("malformed")
            if self.stage != "joined":
                raise Refusal("out_of_order")
            self.log.append("fleet.setup_done", {"target": self.node, "actions": acts})
            self.stage = "setup"
            return [{"t": "ok"}]
        if self.stage not in ("joined", "setup"):  # leave
            raise Refusal("out_of_order")
        self.roster["nodes"].pop(self.node, None)
        self._save_roster()
        self.log.append("fleet.left", {"target": self.node})
        self.stage = "closed"
        return [{"t": "left"}]


def serve(co: Coordinator, host: str, port: int, connections: int) -> None:
    with socket.create_server((host, port)) as srv:
        sys.stdout.write(json.dumps({"listening": port}) + "\n")
        sys.stdout.flush()
        for _ in range(connections):
            sock, _addr = srv.accept()
            sock.settimeout(300)
            conn = Conn(sock)
            co.new_connection()
            try:
                while (msg := conn.recv()) is not None:
                    replies = co.handle(msg)
                    for r in replies:
                        conn.send(r)
                    if co.stage == "closed":
                        break
            except (WireError, OSError) as exc:
                co.log.append("fleet.refused", {"target": co.node, "reason": f"connection: {type(exc).__name__}", "stage": co.stage})
            finally:
                conn.close()


def main(argv: list[str], *, pairing: str) -> int:
    p = argparse.ArgumentParser(prog="mas fleet-coordinator", description="Offer this fleet's setup to nodes whose owners ask to join.")
    for name in ("--state", "--listen", "--fleet", "--target", "--payload", "--connections"):
        p.add_argument(name, required=True)
    a = p.parse_args(argv)
    host, _, port = a.listen.rpartition(":")
    try:
        ipaddress.ip_address(host)
        port_n, n = int(port), int(a.connections)
    except ValueError:
        p.error("--listen is IP:PORT (a literal address) and --connections a number")
    if not (0 < port_n < 65536 and 0 < n <= 1000):
        p.error("port 1-65535, connections 1-1000")
    co = Coordinator(pairing=Path(pairing), state=Path(a.state), fleet=a.fleet, target=a.target, payload=Path(a.payload))
    serve(co, host, port_n, n)
    return 0
