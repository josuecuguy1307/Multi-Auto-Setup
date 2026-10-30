"""mas coordinator start [--web] | mas pair [HOST:PORT] | mas nodes | mas node run

`mas pair` with no address runs on the COORDINATOR: it shows a fresh code and asks the owner to confirm the node. `mas pair HOST:PORT` runs on the NODE: the owner types the code once."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import signal
import sys
import time
import uuid
from pathlib import Path

import envrec_os

EXIT_USAGE = 2


def _state(a) -> Path:
    try:
        return Path(a.state_dir).expanduser() if a.state_dir else envrec_os.default_state_dir()
    except LookupError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(EXIT_USAGE) from None


def _age(ts: float | None, now: float) -> str:
    if ts is None:
        return "never"
    s = max(0, int(now - ts))
    return f"{s} s ago" if s < 120 else f"{s // 60} min ago" if s < 7200 else f"{s // 3600} h ago"


# ---- coordinator -------------------------------------------------------------------------------------------------------------------------------

def cmd_coordinator_start(a) -> int:
    from envrec_coordinator.server import BindRefused, Coordinator
    state = _state(a)

    async def run() -> None:
        coord = Coordinator(state, a.bind, a.port, idle_timeout_s=a.idle_timeout, web=a.web, web_port=a.web_port, name=a.name, model_profile=a.model_profile, models_file=Path(a.models_file) if a.models_file else None, include_virtual=a.include_virtual,
                            discovery_port=None if a.no_discovery else a.discovery_port)
        await coord.start()
        print(f"coordinator listening on {coord.host}:{coord.port} (one port, TLS). fingerprint {coord.identity.fingerprint}", flush=True)
        st = coord.runs.model_status()
        print(f"agent model: {st['profile']} is reachable." if st["available"] else f"agent model: none ({st['reason']}). Analyses will be code-only and labeled so.", flush=True)
        if coord._udp is not None:
            print(f"discovery on UDP port {coord._discovery_port}: computers with the program installed on this network can find this coordinator as '{coord.display_name}'. "
                  "If your firewall asks, allow it; this program never changes a firewall itself.", flush=True)
        else:
            print("discovery is off: computers must be given this coordinator's address.", flush=True)
        if coord.web is not None:
            print(f"web screen, this computer only (the link opens once; do not share it):\n  {coord.web.link}", flush=True)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:  # a platform whose event loop has no signal handlers: Ctrl-C raises KeyboardInterrupt instead
                pass
        await stop.wait()
        await coord.stop()

    try:
        asyncio.run(run())
    except BindRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except OSError as exc:
        print(f"could not listen on {a.bind}:{a.port}" + (f" or on the web screen's port {a.web_port}" if a.web else "") + f": {exc.strerror or exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        pass
    return 0


def cmd_pair_coordinator(a) -> int:
    """The owner at the coordinator: show a fresh code, then confirm the node that uses it."""
    from envrec_coordinator.control import control_dir, read_json, write_json
    from envrec_net import pake
    layer = envrec_os.current()
    ctl = control_dir(_state(a) / "coordinator")
    oid = uuid.uuid4().hex[:12]
    code = pake.new_code()
    write_json(layer, ctl / "pair-offer.json", {"id": oid, "code": code, **({"node": a.node} if getattr(a, "node", None) else {})})
    status_file = ctl / f"offer-{oid}.json"
    t0 = time.monotonic()
    while read_json(status_file) is None:
        if time.monotonic() - t0 > 5:
            (ctl / "pair-offer.json").unlink(missing_ok=True)
            print("The coordinator is not running here (nobody picked up the request). Start it with `mas coordinator start`.", file=sys.stderr)
            return 1
        time.sleep(0.1)
    where = "On that computer (it is waiting) type this code where it asks." if getattr(a, "node", None) else "On the other computer run:  mas join  (or  mas pair HOST:PORT ) and type this code."
    print(f"\n  Pairing code:  {code}\n  Valid for 2 minutes, one attempt. {where}\n", flush=True)
    answered = False
    while True:
        st = read_json(status_file) or {}
        if st.get("status") and st["status"] != "waiting":
            print({"accepted": "Paired. The node is now in `mas nodes`.", "refused": "Refused. Nothing was stored.", "failed": "Failed: " + str(st.get("reason", "")) + " The code is dead; run `mas pair` again.",
                   "expired": "Expired: " + str(st.get("reason", "")) + " Run `mas pair` again.", "rejected": "Rejected: " + str(st.get("reason", ""))}.get(st["status"], str(st)))
            return 0 if st["status"] == "accepted" else 1
        confirm = read_json(ctl / f"confirm-{oid}.json")
        if confirm and not answered:
            print(f"  A computer is asking to join:\n    name         {confirm['name']}\n    system       {confirm['os']} / {confirm['arch']}\n    fingerprint  {confirm['fingerprint']}\n"
                  "  Compare the fingerprint with the one the node printed.", flush=True)
            try:
                yes = input("  Accept this computer? type yes or no: ").strip().lower() == "yes"
            except EOFError:
                yes = False
            write_json(layer, ctl / f"decision-{oid}.json", {"accept": yes})
            answered = True
        time.sleep(0.2)


def _node_id(reg, text: str) -> str | None:
    """A paired node by id, id prefix or name."""
    hits = [n for n in reg.all() if n.node_id == text or n.name == text or n.node_id.startswith(text)]
    return hits[0].node_id if len(hits) == 1 else None


def _detail(n, reg) -> None:
    print(f"\n{n.name}  ({n.node_id})")
    r = n.report
    if not r:
        print("  report: none yet (it is sent when the node connects)")
    else:
        os_ = r.get("os", {})
        print(f"  system: {os_.get('family')} {os_.get('release')} {os_.get('arch')}")
        socks = r.get("sockets", [])
        print("  listening ports: " + ("0 (OK)" if not socks else "ALERT " + ", ".join(f"{x['proto']}:{x['port']}" for x in socks)))
        k = r.get("key", {})
        print("  identity key: " + ("owner-only (OK)" if k.get("owner_only") else "ALERT readable by others" if k.get("exists") else "not found") + f"  {k.get('path', '')}")
    if n.skew_s is not None:
        print(f"  clock skew at last connect: {n.skew_s:+.0f} s (a warning above 300 s, never a refusal)")
    for al in n.alerts or []:
        print(f"  ALERT {al.get('kind')}: {al.get('text')}")
    a = reg.load_analysis(n.node_id)
    if a:
        print("  analysis: " + "; ".join(f"{k}: {len(v) if hasattr(v, '__len__') else v}" for k, v in a.items() if k in ("system", "hardware", "programs", "ai")) + "  (mas task run <node> machine_analysis to refresh)")


def cmd_nodes(a) -> int:
    from envrec_coordinator.registry import Registry
    reg = Registry(_state(a) / "coordinator", envrec_os.current())
    nodes = reg.all()
    if not nodes:
        print("No paired nodes. Run `mas pair` here, then `mas join` on the other computer.")
        return 0
    now = time.time()
    print(f"{'NAME':<24} {'SYSTEM':<18} {'LAST HEARTBEAT':<16} FINGERPRINT")
    for n in nodes:
        print(f"{n.name:<24} {n.os + '/' + n.arch:<18} {_age(n.last_heartbeat, now):<16} {n.fingerprint}")
    if a.detail:
        for n in nodes:
            _detail(n, reg)
    return 0


def cmd_task_run(a) -> int:
    """Ask the running coordinator for one read-only task on one node and wait for its end (the request travels as a file in the coordinator's private control folder)."""
    from envrec_coordinator.control import control_dir, read_json, write_json
    from envrec_coordinator.registry import Registry
    from envrec_net import task as T
    if a.type not in T.PARAMS:
        print(f"Unknown task type. Choose one of: {', '.join(sorted(T.PARAMS))}", file=sys.stderr)
        return EXIT_USAGE
    reg = Registry(_state(a) / "coordinator", envrec_os.current())
    nid = _node_id(reg, a.node)
    if nid is None:
        print(f"No single paired node matches {a.node!r}. See `mas nodes`.", file=sys.stderr)
        return EXIT_USAGE
    ctl = control_dir(_state(a) / "coordinator")
    rid = uuid.uuid4().hex[:10]
    write_json(envrec_os.current(), ctl / f"task-req-{rid}.json", {"id": rid, "node": nid, "type": a.type})
    res_file = ctl / f"task-res-{rid}.json"
    end = time.monotonic() + T.TIMEOUTS_S[a.type] + 15
    while time.monotonic() < end:
        res = read_json(res_file)
        if res is not None:
            res_file.unlink(missing_ok=True)
            print(f"{a.type} on {a.node}: {res['status']}" + (f" ({T.REJECTIONS.get(res['reason'], res['reason'])})" if res.get("reason") else ""))
            if res["status"] == "done":
                print(json.dumps(res.get("result", {}), indent=1, ensure_ascii=False)[:6000])
            return 0 if res["status"] == "done" else 1
        time.sleep(0.3)
    print("The coordinator did not answer. Is `mas coordinator start` running here?", file=sys.stderr)
    return 1


def cmd_task_list(a) -> int:
    from envrec_coordinator.control import control_dir, read_json
    d = read_json(control_dir(_state(a) / "coordinator") / "tasks.json")
    if d is None:
        print("The coordinator is not running here, or no task was made yet.", file=sys.stderr)
        return 1
    now = time.time()
    print(f"{'ID':<14} {'TYPE':<18} {'NODE':<36} {'BY':<6} {'STATUS':<10} REASON")
    for t in d.get("tasks", []):
        print(f"{t['id']:<14} {t['type']:<18} {t['node']:<36} {t['by']:<6} {t['status']:<10} {t.get('reason', '')}")
    return 0


def cmd_waiting(a) -> int:
    """Computers that found this coordinator and are waiting for the owner to pair them (written by the running coordinator; nothing listens for this command)."""
    from envrec_coordinator.control import control_dir, read_json
    d = read_json(control_dir(_state(a) / "coordinator") / "waiting.json")
    if d is None:
        print("The coordinator is not running here, or nothing has been published yet. Start it with `mas coordinator start`.", file=sys.stderr)
        return 1
    rows = d.get("waiting", [])
    if not rows:
        print("No computers are waiting. On another computer with the program installed, run `mas join`.")
        return 0
    now = time.time()
    print(f"{'ID':<14} {'NAME':<22} {'SYSTEM':<16} {'WAITING':<10} FINGERPRINT (claimed)")
    for w in rows:
        print(f"{w['id']:<14} {w['name']:<22} {w['os'] + '/' + w['arch']:<16} {_age(w['since'], now):<10} {w['fingerprint']}")
    print("\nPair one with:  mas pair --node ID")
    return 0


def cmd_join(a) -> int:
    """On a node: find coordinators, let the owner pick one, register as waiting, then pair with the code the owner types."""
    from envrec_net import discovery
    from envrec_net.log import EventLog
    from envrec_node.discover import DiscoveryFailed, discover, parse_address
    from envrec_node.join import join
    from envrec_node.pair import PairingFailed
    from envrec_node.state import NodeState
    state_dir = _state(a)
    log = EventLog(NodeState(state_dir).log_dir)
    try:
        if a.coordinator:
            try:
                chosen = parse_address(a.coordinator)
            except ValueError:
                print("The coordinator address looks like HOST:PORT (an IP address and a port).", file=sys.stderr)
                return EXIT_USAGE
            log.add("coordinator_named", {"address": a.coordinator})
        else:
            print("Looking for coordinators on this network...", flush=True)
            try:
                found = discover(a.discovery_port, include_virtual=a.include_virtual)
            except DiscoveryFailed as exc:
                log.add("discovery_failed", {"reason": exc.code})
                print(f"{exc}\n" + "\n".join(f"  - {g}" for g in exc.guidance), file=sys.stderr)
                return 1
            log.add("discovery_answers", {"count": len(found)})
            print("Found (an answer before pairing is only a hint; the owner's approval and the code protect the pairing):")
            for i, f in enumerate(found, 1):
                print(f"  {i}. {f.name}   {f.address}:{f.port}   {f.fingerprint}")
            try:
                pick = input(f"Join which one? type a number 1-{len(found)} (or nothing to cancel): ").strip()  # always the owner's choice, even when only one answered
            except EOFError:
                pick = ""
            if not pick.isdigit() or not 1 <= int(pick) <= len(found):
                print("Cancelled. Nothing was changed.")
                return 1
            chosen = found[int(pick) - 1]
            log.add("owner_picked_coordinator", {"name": chosen.name, "address": f"{chosen.address}:{chosen.port}", "fingerprint": chosen.fingerprint}, owner=True)

        async def ask_code() -> str:
            if a.code:
                return a.code
            return await asyncio.to_thread(lambda: getpass.getpass("Pairing code shown on the coordinator: ") if sys.stdin.isatty() else sys.stdin.readline().strip())

        try:
            pin = asyncio.run(join(chosen, state_dir, name=a.name, ask_code=ask_code, on_waiting=lambda m: print(m, flush=True), on_event=lambda t, p: log.add(t, p)))
        except PairingFailed as exc:
            print(f"Not paired: {exc}", file=sys.stderr)
            return 1
        print(f"Paired with the coordinator at {pin.address}. Start the agent with `mas node run`.")
        return 0
    finally:
        log.close()


# ---- node --------------------------------------------------------------------------------------------------------------------------------------

def cmd_pair_node(a) -> int:
    from envrec_node.pair import PairingFailed, pair
    a.address = a.address or a.coordinator
    code = a.code or (getpass.getpass("Pairing code shown on the coordinator: ") if sys.stdin.isatty() else sys.stdin.readline().strip())
    try:
        pin = asyncio.run(pair(a.address, code.strip(), _state(a), name=a.name))
    except PairingFailed as exc:
        print(f"Not paired: {exc}", file=sys.stderr)
        return 1
    print(f"Paired with the coordinator at {pin.address}. Start the agent with `mas node run`.")
    return 0


def cmd_node_run(a) -> int:
    from envrec_node.agent import NodeAgent
    if a.clock_offset_s and os.environ.get("MAS_TESTING") != "1":  # a test-only flag: it refuses to work unless the environment says this is a test run
        print("--clock-offset-s is for tests only (it makes this computer's clock read wrong on purpose). It is refused unless MAS_TESTING=1 is set.", file=sys.stderr)
        return EXIT_USAGE

    async def run() -> None:
        agent = NodeAgent(_state(a), interval_s=a.interval, clock=(lambda: time.time() + a.clock_offset_s) if a.clock_offset_s else time.time, rediscover=not a.no_discovery, discovery_port=a.discovery_port, on_message=lambda m: print(m, flush=True))
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, agent.stop_event.set)
            except NotImplementedError:
                pass
        print(f"node {agent.identity.node_id} dialing {agent.pin.address if agent.pin else '(not paired)'}; it listens on nothing.", flush=True)
        await agent.run()

    try:
        asyncio.run(run())
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        pass
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mas", description="Multi Auto Setup")
    p.add_argument("--state-dir", default=None, help="where keys, pins and the log live (default: the operating system's usual place, from the os-state-paths skill)")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("coordinator", help="the coordinator").add_subparsers(dest="sub", required=True).add_parser("start", help="listen on one port")
    c.add_argument("--bind", required=True, help="the address of the interface to listen on (loopback, private or link-local; never every interface)")
    c.add_argument("--port", type=int, default=7443)
    c.add_argument("--idle-timeout", type=float, default=30.0)
    c.add_argument("--discovery-port", type=int, default=7444, help="the UDP port computers use to find this coordinator (default 7444)")
    c.add_argument("--no-discovery", action="store_true", help="do not open the discovery UDP socket")
    c.add_argument("--name", default=None, help="the name shown to computers that find this coordinator (default: the host name)")
    c.add_argument("--include-virtual", action="store_true", help="also answer on VPN, container and virtual-machine interfaces")
    c.add_argument("--model-profile", default=None, help="the model profile (from config/models.yaml and config/models.local.yaml) the coordinator's agent uses for machine analyses; without one the analysis is code-only and says so")
    c.add_argument("--models-file", default=None, help="the models file (default config/models.yaml)")
    c.add_argument("--web", action="store_true", help="also serve the owner's screen on this computer only (loopback; prints a one-time link). Off by default")
    c.add_argument("--web-port", type=int, default=0, help="the loopback port of the web screen (default: any free one)")
    c.set_defaults(fn=cmd_coordinator_start)
    pr = sub.add_parser("pair", help="on the coordinator: show a code and confirm a node; on a node: `mas pair HOST:PORT`")
    pr.add_argument("address", nargs="?", default=None)
    pr.add_argument("--node", default=None, help="on the coordinator: make the code for this waiting computer (see `mas waiting`)")
    pr.add_argument("--coordinator", default=None, help="on a node: the coordinator's HOST:PORT (same as the positional address)")
    pr.add_argument("--code", default=None, help="the code (prefer typing it when asked: it stays out of your shell history)")
    pr.add_argument("--name", default=None, help="the name this node shows the owner (default: its host name)")
    pr.set_defaults(fn=lambda a: cmd_pair_node(a) if (a.address or a.coordinator) else cmd_pair_coordinator(a))
    sub.add_parser("waiting", help="computers that found this coordinator and wait for the owner to pair them").set_defaults(fn=cmd_waiting)
    j = sub.add_parser("join", help="on a node: find coordinators on this network, pick one, and pair without typing an address")
    j.add_argument("--coordinator", default=None, help="skip discovery: the coordinator's HOST:PORT")
    j.add_argument("--discovery-port", type=int, default=7444)
    j.add_argument("--include-virtual", action="store_true", help="also look on VPN, container and virtual-machine interfaces")
    j.add_argument("--name", default=None, help="the name this computer shows the owner (default: its host name)")
    j.add_argument("--code", default=None, help="the code (prefer typing it when asked)")
    j.set_defaults(fn=cmd_join)
    nd = sub.add_parser("nodes", help="the paired nodes and their last heartbeat")
    nd.add_argument("--detail", action="store_true", help="also show each node's latest self-report, alerts and clock skew")
    nd.set_defaults(fn=cmd_nodes)
    tk = sub.add_parser("task", help="read-only tasks on a paired node").add_subparsers(dest="sub", required=True)
    tr = tk.add_parser("run", help="run node_report or machine_analysis on a node (by name or id)")
    tr.add_argument("node")
    tr.add_argument("type", help="node_report or machine_analysis")
    tr.set_defaults(fn=cmd_task_run)
    tk.add_parser("list", help="the coordinator's recent tasks").set_defaults(fn=cmd_task_list)
    n = sub.add_parser("node", help="the node agent").add_subparsers(dest="sub", required=True).add_parser("run", help="dial out and send heartbeats")
    n.add_argument("--interval", type=float, default=10.0)
    n.add_argument("--clock-offset-s", type=float, default=0.0, help=argparse.SUPPRESS)  # testing only: makes this node's clock read wrong on purpose, to show that nothing depends on it
    n.add_argument("--no-discovery", action="store_true", help="never look for the coordinator again after its address changes")
    n.add_argument("--discovery-port", type=int, default=7444)
    n.set_defaults(fn=cmd_node_run)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
