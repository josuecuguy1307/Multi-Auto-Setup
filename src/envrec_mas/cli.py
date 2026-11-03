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
from envrec_mas import coordconfig

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

def cmd_mcp(a) -> int:
    """The MCP server of the Harness App, started the way a chat host starts it: `mas --state-dir DIR mcp --channel NAME`. The same server as `python -m envrec_harness.mcp_server`, reached through the
    installed `mas` command so that a chat's settings name `mas`, never a release folder or a Python path. Nothing but the protocol goes to stdout."""
    from envrec_harness import mcp_server
    mcp_server.main(["--state-dir", str(_state(a)), "--channel", a.channel] + (["--operator"] if a.operator else []))
    return 0


def cmd_coordinator_start(a) -> int:
    from envrec_coordinator.server import BindRefused, Coordinator
    state = _state(a)
    try:
        cfg = coordconfig.resolve(coordconfig.from_namespace(a), state)
    except coordconfig.ConfigRefused as exc:
        print(f"refused: {exc.text} [{exc.reason}]", file=sys.stderr)
        return EXIT_USAGE
    if not cfg["bind"]:
        print(f"refused: the coordinator needs an address to listen on: pass --bind ADDRESS, or put \"bind\" in {coordconfig.path_of(state)} [no_bind]", file=sys.stderr)
        return EXIT_USAGE

    async def run() -> None:
        coord = Coordinator(state, cfg["bind"], cfg["port"], idle_timeout_s=cfg["idle_timeout"], web=cfg["web"], web_port=cfg["web_port"], name=cfg["name"], model_profile=cfg["model_profile"],
                            models_file=Path(cfg["models_file"]).expanduser() if cfg["models_file"] else None, include_virtual=cfg["include_virtual"],
                            discovery_port=None if cfg["no_discovery"] else cfg["discovery_port"], board_escalation_limit=cfg["board_escalation_limit"])
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
        print(f"could not listen on {cfg['bind']}:{cfg['port']}" + (f" or on the web screen's port {cfg['web_port']}" if cfg["web"] else "") + f": {exc.strerror or exc}", file=sys.stderr)
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


def cmd_runs(a) -> int:
    """Read-only: every run and job the coordinator knows, newest last: so a run is followed from a terminal without opening a protected folder."""
    from envrec_coordinator.control import control_dir, read_json
    d = read_json(control_dir(_state(a) / "coordinator") / "runs.json")
    if d is None:
        print("The coordinator is not running here, or nothing has run yet.", file=sys.stderr)
        return 1
    print(f"{'ID':<14} {'KIND':<18} {'STATE':<22} WHAT")
    for r in d.get("runs", []):
        print(f"{r['id']:<14} {r['kind'] + ' run':<18} {r['status']:<22} {r['node']}" + (f": {r['reason'][:90]}" if r.get("reason") else ""))
    for j in d.get("jobs", []):
        print(f"{j['id']:<14} {j['kind']:<18} {j['state']:<22} {(j.get('text') or '').splitlines()[0][:100] if j.get('text') else ''}")
    return 0


def cmd_config(a) -> int:
    """Show or set the owner's verbosity (brief, normal, detailed): the profile's `verbosity` field, the same place the chat's question and the panel write to. Presentation only; typed by the owner at his terminal."""
    from envrec_coordinator.profile import Profile, ProfileError
    from envrec_harness.formatter import LEVELS
    from envrec_mas import strings as S
    prof = Profile(_state(a) / "coordinator" / "profile.json")
    old = prof.get("verbosity")
    if a.setting is None:
        print(S.CONFIG_SHOW.format(level=old))
        return 0
    if a.setting != "verbosity" or a.value is None:
        print(S.CONFIG_BAD.format(levels=", ".join(LEVELS)), file=sys.stderr)
        return 2
    try:
        prof.set("verbosity", a.value, by="owner")
    except ProfileError:
        print(S.CONFIG_BAD.format(levels=", ".join(LEVELS)), file=sys.stderr)
        return 2
    print(S.CONFIG_SET.format(level=a.value))
    print(S.CONFIG_UNDO.format(old=old))
    return 0


def cmd_run(a) -> int:
    """Read-only: one run or job in full: its state, its reason, and for a run every recorded step with who acted (MODEL, GUARD, SCRIPT, OWNER)."""
    from envrec_coordinator.control import control_dir, read_json
    state = _state(a)
    d = read_json(control_dir(state / "coordinator") / "runs.json") or {}
    hit = next((x for x in d.get("runs", []) if x["id"].startswith(a.id)), None) or next((x for x in d.get("jobs", []) if x["id"].startswith(a.id)), None)
    if hit is None:
        print(f"No run or job matches {a.id!r}. See `mas runs`.", file=sys.stderr)
        return 1
    print(json.dumps({k: v for k, v in hit.items()}, indent=1, default=str))
    store_dir = state / "coordinator" / "runs" / hit["id"]
    if store_dir.is_dir() and (store_dir / "envrec.db").is_file():
        from envrec.store import Store
        with Store(store_dir) as st:
            events = [e for e in st.events() if e.type not in ("model_call", "view")][-60:]
            print(f"\nLast {len(events)} steps:")
            for e in events:
                print(f"  [{e.effective_tag}] {e.type} {json.dumps(e.payload, default=str)[:140]}")
    return 0


def connect_session(core, goal: str, host: str | None, *, read, write, wait: float = 8.0) -> int:
    """`mas connect` / `mas disconnect` at the owner's terminal: start the run, show the plan as text, ask the closed question, and for every write show it and take the owner's typed `yes` as the answer. The typed answer
    is the owner's gesture (the terminal is their own); a script cannot give it. Returns 0 when the run ended done or rolled back."""
    run = core.start(goal, host or "")
    rid, since, status = run["id"], 0, run.get("status")
    while True:
        out = core.intents(rid, since, wait)
        status = out["run"]["status"]
        for i in out["intents"]:
            since = max(since, i["seq"])
            kind = i["kind"]
            if kind == "show_data" and i["payload"].get("data_kind") == "plan":
                write(i["text_fallback"])
            elif kind == "progress":
                write(f"  [{i['actor']}] {i['text_fallback']}")
            elif kind == "text":
                write(i["text_fallback"])
            elif kind == "ask_choice" and i.get("state") == "pending":
                write(i["text_fallback"])
                core.decide(i["id"], read("Your answer: ").strip())
            elif kind == "confirm" and i.get("state") == "pending":
                write(i["text_fallback"])
                whole = bool(i["payload"].get("chat_choice"))  # approval-policy.json: a whole plan is ONE question (the owner's typed yes at his own terminal answers it); a single step keeps its typed confirmation
                yes = read(f"Approve this {'plan' if whole else 'step'}? Type yes to approve, anything else to decline: ").strip().lower() == "yes"
                if whole:
                    core.decide(i["id"], "approve" if yes else "reject")
                else:
                    b = core.begin_confirmation(i["id"], "cli_typed")
                    core.complete_confirmation(b["ticket"], b["plan_hash"], "accept" if yes else "decline")
        if out["run"]["finished"] and not out["waiting"]:
            break
    write(f"The run ended: {status}.")
    return 0 if status in ("done", "rolled_back") else 1


def cmd_connect(a) -> int:
    """Connect a chat you already use (Open WebUI, Claude Desktop, a terminal agent) to Multi Auto Setup. The coordinator must be running (with --web); every write is approved here, by you typing yes."""
    from envrec_harness.client import CoreClient, CoreError
    disconnect = a.cmd == "disconnect"
    if not sys.stdin.isatty():
        print("This needs you at the terminal: every step asks for your yes, and a script cannot give it.", file=sys.stderr)
        return EXIT_USAGE
    hdir = _state(a) / "coordinator" / "harness"
    try:
        core = CoreClient(hdir, "cli", timeout=60.0)
        core.declare({"event_call": True, "host": "terminal"})
        code = connect_session(core, "disconnect_host" if disconnect else "connect_host", a.host, read=input, write=print)
    except FileNotFoundError:
        print("The coordinator is not running here with --web. Start it with `mas coordinator start --bind 127.0.0.1 --web`.", file=sys.stderr)
        return 1
    except CoreError as exc:
        print(f"{exc.error}: {exc.message}".strip(": "), file=sys.stderr)
        return 1
    if code == 0 and not disconnect and a.host == "openwebui":  # the generated secret is never printed at the end of a run (a terminal is scrollback and gets pasted); it is shown only when asked for
        print("\nOpen WebUI is at http://127.0.0.1:8080. To see its administrator account, run: mas openwebui-account")
    return code


def cmd_openwebui_account(a) -> int:
    """Print the Open WebUI administrator account that `mas connect openwebui` made, from the file only you can read (at your own terminal: it is never sent to a chat)."""
    from envrec_coordinator.connect import Env, read_admin_account
    acct = read_admin_account(Env.system(_state(a)))
    if not acct:
        print("No Open WebUI account was made here by `mas connect openwebui`.", file=sys.stderr)
        return 1
    print(f"Open WebUI: http://127.0.0.1:8080\n  email    {acct['email']}\n  secret   {acct['password']}")
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


def _version_text() -> str:
    from envrec_mas import release
    info = release.running_release()
    return "mas 0.1.0 (" + (f"release {info['tag']}" if info and info.get("tag") else "source checkout") + ")"


def _redirect_output(path: Path, max_bytes: int = 5 * 1024 * 1024) -> None:
    """A service has no terminal (and a windowless interpreter has no stdout at all): everything goes to one log file in the state folder, rotated once when it grows past the limit."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.stat().st_size > max_bytes:
            os.replace(path, path.with_suffix(path.suffix + ".1"))
    except OSError:
        pass
    fh = open(path, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = fh


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mas", description="Multi Auto Setup")
    p.add_argument("--state-dir", default=None, help="where keys, pins and the log live (default: the operating system's usual place, from the os-state-paths skill)")
    p.add_argument("--log-file", default=None, help="append everything this command prints to this file (what a service uses)")
    p.add_argument("--version", action="version", version=_version_text())
    sub = p.add_subparsers(dest="cmd")  # no command: the first-run menu at a terminal, plain help otherwise
    coord = sub.add_parser("coordinator", help="the coordinator").add_subparsers(dest="sub", required=True)
    c = coord.add_parser("start", help="listen on one port")
    ri = coord.add_parser("rotate-identity", help="make a new coordinator identity (every node must pair again)")
    ri.add_argument("--yes", action="store_true", help="answer yes to the question that was shown")
    ri.set_defaults(fn=lambda a: __import__("envrec_mas.lifecycle", fromlist=["x"]).cmd_rotate_identity(a))
    coordconfig.add_flags(c)  # one flag per setting in skills/os-service/references/coordinator-config.json; the file <state>/coordinator.json supplies what is not given
    c.set_defaults(fn=cmd_coordinator_start)
    pr = sub.add_parser("pair", help="on the coordinator: show a code and confirm a node; on a node: `mas pair HOST:PORT`")
    pr.add_argument("address", nargs="?", default=None)
    pr.add_argument("--node", default=None, help="on the coordinator: make the code for this waiting computer (see `mas waiting`)")
    pr.add_argument("--coordinator", default=None, help="on a node: the coordinator's HOST:PORT (same as the positional address)")
    pr.add_argument("--code", default=None, help="the code (prefer typing it when asked: it stays out of your shell history)")
    pr.add_argument("--name", default=None, help="the name this node shows the owner (default: its host name)")
    pr.set_defaults(fn=lambda a: cmd_pair_node(a) if (a.address or a.coordinator) else cmd_pair_coordinator(a))
    sub.add_parser("runs", help="read-only: the runs and jobs the coordinator knows and what state each is in").set_defaults(fn=cmd_runs)
    cf = sub.add_parser("config", help="show or set how much detail is shown: mas config verbosity brief|normal|detailed")
    cf.add_argument("setting", nargs="?", default=None)
    cf.add_argument("value", nargs="?", default=None)
    cf.set_defaults(fn=cmd_config)
    rr = sub.add_parser("run", help="read-only: one run or job in full, step by step")
    rr.add_argument("id")
    rr.set_defaults(fn=cmd_run)
    sub.add_parser("waiting", help="computers that found this coordinator and wait for the owner to pair them").set_defaults(fn=cmd_waiting)
    cn = sub.add_parser("connect", help="connect a chat you already use (Open WebUI, Claude Desktop, a terminal agent); you see the plan and approve each step")
    cn.add_argument("host", nargs="?", default=None, help="openwebui, claude-desktop, claude-code, codex, opencode or muse-code; without it you are asked which of the chats found here")
    cn.set_defaults(fn=cmd_connect)
    sub.add_parser("openwebui-account", help="show the Open WebUI administrator account made by `mas connect openwebui`").set_defaults(fn=cmd_openwebui_account)
    dc = sub.add_parser("disconnect", help="undo the connection of a chat that was made earlier (only what it wrote)")
    dc.add_argument("host", help="the chat's name, as for `mas connect`")
    dc.set_defaults(fn=cmd_connect)
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
    from envrec_mas import lifecycle
    mc = sub.add_parser("mcp", help="the Harness App's MCP server for a chat (stdio); a chat host runs this, you do not")
    mc.add_argument("--channel", default="claude-desktop", help="the chat's name (claude-desktop, terminal, ...)")
    mc.add_argument("--operator", action="store_true", help="a verification run driven by the assistant on the owner's behalf")
    mc.set_defaults(fn=cmd_mcp)
    sv = sub.add_parser("service", help="start the node (or coordinator) by itself when you sign in; per user, no administrator")
    sv.add_argument("action", choices=["install", "status", "uninstall"])
    sv.add_argument("--role", choices=["node", "coordinator"], default="node")
    coordconfig.add_flags(sv)  # coordinator only: the same settings as `mas coordinator start`, written to <state>/coordinator.json
    sv.add_argument("--yes", action="store_true", help="answer yes to the question that was shown")
    sv.set_defaults(fn=lifecycle.cmd_service)
    up = sub.add_parser("update", help="check the latest public release, show what changes, and replace this one safely")
    up.add_argument("--check", action="store_true", help="only say whether there is a newer release and what would change")
    up.add_argument("--yes", action="store_true", help="answer yes to the question that was shown")
    up.set_defaults(fn=lifecycle.cmd_update)
    un = sub.add_parser("unpair", help="on the coordinator: remove a paired node (it is refused from now on and forgets this coordinator)")
    un.add_argument("node", help="name, id or id prefix (see `mas nodes`)")
    un.add_argument("--yes", action="store_true", help="answer yes to the question that was shown")
    un.set_defaults(fn=lifecycle.cmd_unpair)
    fg = sub.add_parser("forget", help="on a node: forget the coordinator locally")
    fg.add_argument("--yes", action="store_true", help="answer yes to the question that was shown")
    fg.set_defaults(fn=lifecycle.cmd_forget)
    a = p.parse_args(argv)
    if a.cmd is None:
        from envrec_mas import menu
        from envrec_style import term
        code = menu.main_for_terminal(_state(a), ["--state-dir", a.state_dir] if a.state_dir else [])
        if code == 2:  # no terminal to answer on: say what `mas` can do and never wait for input
            print(term.plain_text() + "\n\n" + p.format_help())
        return code
    if a.log_file:
        _redirect_output(Path(a.log_file))
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
