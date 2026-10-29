"""mas coordinator start [--web] | mas pair [HOST:PORT] | mas nodes | mas node run

`mas pair` with no address runs on the COORDINATOR: it shows a fresh code and asks the owner to confirm the node. `mas pair HOST:PORT` runs on the NODE: the owner types the code once."""

from __future__ import annotations

import argparse
import asyncio
import getpass
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
        coord = Coordinator(state, a.bind, a.port, idle_timeout_s=a.idle_timeout, web=a.web, web_port=a.web_port)
        await coord.start()
        print(f"coordinator listening on {coord.host}:{coord.port} (one port, TLS). fingerprint {coord.identity.fingerprint}", flush=True)
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
    write_json(layer, ctl / "pair-offer.json", {"id": oid, "code": code})
    status_file = ctl / f"offer-{oid}.json"
    t0 = time.monotonic()
    while read_json(status_file) is None:
        if time.monotonic() - t0 > 5:
            (ctl / "pair-offer.json").unlink(missing_ok=True)
            print("The coordinator is not running here (nobody picked up the request). Start it with `mas coordinator start`.", file=sys.stderr)
            return 1
        time.sleep(0.1)
    print(f"\n  Pairing code:  {code}\n  Valid for 2 minutes, one attempt. On the other computer run:  mas pair HOST:PORT  and type this code.\n", flush=True)
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


def cmd_nodes(a) -> int:
    from envrec_coordinator.registry import Registry
    reg = Registry(_state(a) / "coordinator", envrec_os.current())
    nodes = reg.all()
    if not nodes:
        print("No paired nodes. Run `mas pair` here, then `mas pair HOST:PORT` on the other computer.")
        return 0
    now = time.time()
    print(f"{'NAME':<24} {'SYSTEM':<18} {'LAST HEARTBEAT':<16} FINGERPRINT")
    for n in nodes:
        print(f"{n.name:<24} {n.os + '/' + n.arch:<18} {_age(n.last_heartbeat, now):<16} {n.fingerprint}")
    return 0


# ---- node --------------------------------------------------------------------------------------------------------------------------------------

def cmd_pair_node(a) -> int:
    from envrec_node.pair import PairingFailed, pair
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

    async def run() -> None:
        agent = NodeAgent(_state(a), interval_s=a.interval)
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
    c.add_argument("--web", action="store_true", help="also serve the owner's screen on this computer only (loopback; prints a one-time link). Off by default")
    c.add_argument("--web-port", type=int, default=0, help="the loopback port of the web screen (default: any free one)")
    c.set_defaults(fn=cmd_coordinator_start)
    pr = sub.add_parser("pair", help="on the coordinator: show a code and confirm a node; on a node: `mas pair HOST:PORT`")
    pr.add_argument("address", nargs="?", default=None)
    pr.add_argument("--code", default=None, help="the code (prefer typing it when asked: it stays out of your shell history)")
    pr.add_argument("--name", default=None, help="the name this node shows the owner (default: its host name)")
    pr.set_defaults(fn=lambda a: cmd_pair_node(a) if a.address else cmd_pair_coordinator(a))
    sub.add_parser("nodes", help="the paired nodes and their last heartbeat").set_defaults(fn=cmd_nodes)
    n = sub.add_parser("node", help="the node agent").add_subparsers(dest="sub", required=True).add_parser("run", help="dial out and send heartbeats")
    n.add_argument("--interval", type=float, default=10.0)
    n.set_defaults(fn=cmd_node_run)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
