"""Fleet commands: inventory, enroll, fleet, deploy, undo. Interactive and human-driven by design."""

from __future__ import annotations

import sys
from pathlib import Path

from envrec.enroll import EnrollmentRefused, enroll
from envrec.inventory import build_candidates, render_inventory
from envrec.mission import Transport, load_mission, load_targets
from envrec.remote.approval import ApprovalGate
from envrec.remote.fleet import inventory_fleet, render_table
from envrec.remote.planner import plan_transport, record_plan
from envrec.remote.provision import Provisioner, Release, SERVER_PORT
from envrec.remote.runner import MissingKey, RemoteRunner, TargetNotAllowed
from envrec.store import Store
from envrec.verify import ExecClient, TunnelClient, verify_server


def _runner(cfg, args) -> RemoteRunner:
    return RemoteRunner(cfg, host_key_policy="yes")


def cmd_inventory(args) -> int:
    with Store(args.store) as store:
        cfg = load_targets(args.targets)
        print(render_inventory(build_candidates(store, cfg), cfg))
    return 0


def cmd_enroll(args) -> int:
    with Store(args.store) as store:
        cfg = load_targets(args.targets)
        cands = build_candidates(store, cfg)
        if not cands:
            print("No candidates to enroll.")
            return 0
        for i, c in enumerate(cands, 1):
            print(f"{i}. {c.name} ({c.kind})")
        pick = input("Number of the ONE device to enroll (Enter to quit): ").strip()
        if not pick.isdigit() or not 1 <= int(pick) <= len(cands):
            return 0

        def check(entry, transport) -> bool:
            try:
                return RemoteRunner(load_targets(args.targets), host_key_policy="yes").run(entry.id, transport, "uname_s", timeout=15).ok
            except (MissingKey, TargetNotAllowed, OSError):
                return False

        try:
            res = enroll(cands[int(pick) - 1], targets_path=args.targets, store=store, check=check)
        except EnrollmentRefused as exc:
            print(f"Refused: {exc}", file=sys.stderr)
            return 2
        print(res.reason)
    return 0


def cmd_fleet(args) -> int:
    cfg = load_targets(args.targets)
    with Store(args.store) as store:
        p = Provisioner(_runner(cfg, args), ApprovalGate(store), store, cfg, None)
        wanted = {t.id: t.allowed_transports[0] for t in cfg.targets}
        print(render_table(inventory_fleet(p, wanted)))
    return 0


def _provisioner(args, store, cfg) -> Provisioner:
    release = Release.load(args.release, args.artifacts)
    return Provisioner(_runner(cfg, args), ApprovalGate(store), store, cfg, release)


def _plan(p: Provisioner, tid: str):
    entry = p.config.get(tid)
    reach = {}
    for t in entry.allowed_transports:  # one read-only check per LISTED transport, nothing else
        try:
            reach[t] = p.runner.run(tid, t, "uname_s", timeout=15).ok
        except (MissingKey, TargetNotAllowed, OSError):
            reach[t] = False
    facts = {}
    plan = plan_transport(entry, facts, reach)
    record_plan(p.store, tid, plan)
    return plan


def cmd_deploy(args) -> int:
    cfg = load_targets(args.targets)
    mission = load_mission(args.mission) if args.mission and Path(args.mission).is_file() else None
    with Store(args.store) as store:
        p = _provisioner(args, store, cfg)
        if mission:
            print(mission.render())
        plan = _plan(p, args.target)
        if plan.transport is None:
            print(f"Stopped. {plan.hint}")
            return 3
        print(f"Using {plan.transport.value} ({'; '.join(plan.decided_by)})")
        res = p.deploy(args.target, plan.transport)
        for st in res.steps:
            print(f"  [{st.status}] {st.recipe}: {st.detail}")
        if not res.ok:
            print(f"Failed: {res.failure}" + ("  (run `undo` to remove anything that was installed)" if res.needs_undo else ""))
            return 1
        tunnel = None
        try:
            if plan.transport == Transport.ssh:
                tunnel = p.open_tunnel(args.target)
                client = TunnelClient(tunnel.local_port)
            else:
                client = ExecClient(p.runner, args.target, plan.transport, SERVER_PORT)
            v = verify_server(client, store, p.targets[args.target])
        finally:
            if tunnel:
                tunnel.close()
        print(("VERIFIED: " if v.ok else "NOT VERIFIED: ") + v.reason)
        return 0 if v.ok else 1


def cmd_undo(args) -> int:
    cfg = load_targets(args.targets)
    with Store(args.store) as store:
        p = _provisioner(args, store, cfg)
        plan = _plan(p, args.target)
        if plan.transport is None:
            print(f"Stopped. {plan.hint}")
            return 3
        ok, msg = p.undo(args.target, plan.transport)
        print(msg)
        return 0 if ok else 1


def add_fleet_commands(sub) -> None:
    def common(p, store=True, release=False):
        p.add_argument("--targets", default="targets.yaml")
        if store:
            p.add_argument("--store", default="runs/fleet")
        if release:
            p.add_argument("--release", default="build/worker/release.json")
            p.add_argument("--artifacts", default="build/worker/dist")
            p.add_argument("--target", required=True, help="a target id from targets.yaml")
    i = sub.add_parser("inventory", help="print the inventory report for a finished store (writes nothing)")
    common(i); i.set_defaults(func=cmd_inventory)
    e = sub.add_parser("enroll", help="interactively enroll ONE seen device into targets.yaml (needs a terminal)")
    common(e); e.set_defaults(func=cmd_enroll)
    f = sub.add_parser("fleet", help="read-only capability table of the enrolled devices")
    common(f); f.set_defaults(func=cmd_fleet)
    d = sub.add_parser("deploy", help="provision a model server on one enrolled device (asks approval for every write)")
    common(d, release=True); d.add_argument("--mission", default="mission.yaml"); d.set_defaults(func=cmd_deploy)
    u = sub.add_parser("undo", help="stop the server and remove everything installed on one device (one approval)")
    common(u, release=True); u.set_defaults(func=cmd_undo)
