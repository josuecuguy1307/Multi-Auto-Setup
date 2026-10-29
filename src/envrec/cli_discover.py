"""`envrec discover`: run the discovery skills for real and print what was found. Read-only; it never edits targets.yaml."""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import sys
import time
from pathlib import Path

from envrec.contracts import FactStatus, Origin, Target, TargetKind
from envrec.discovery import LocalCollector, Salts
from envrec.discovery.classify import classify_lan
from envrec.discovery.config import DiscoveryConfig
from envrec.discovery.runner import AdapterRun
from envrec.discovery.scope import ScopeError, scope_for_interface
from envrec.remote.approval import ApprovalGate, PendingAction
from envrec.skills.discover import all_adapters, get_adapter, mode_of, record_denied, run_adapter, survey
from envrec.store import Store

ACTIVE_ORDER = ["lan_sweep", "lan_ssdp", "lan_ports"]


def _entities(store: Store, target_id: str) -> list:
    return [f for f in store.current_facts(target_id) if ".entity." in f.key and f.status != FactStatus.unknown]


def render_table(runs: list[AdapterRun]) -> str:
    head = f"{'ADAPTER':20} {'RING':4} {'STATUS':14} {'FOUND':>5} {'RESOLVED':>8} {'UNKNOWN':>7} {'SECS':>6}  NOTE"
    rows = [head, "-" * len(head)]
    for r in sorted(runs, key=lambda r: (r.ring, r.adapter_id)):
        rows.append(f"{r.adapter_id:20} {r.ring:<4} {r.status:14} {r.entities:>5} {r.resolved:>8} {r.unknowns:>7} {r.seconds:>6.1f}  {r.reason[:70]}")
    return "\n".join(rows)


def render_entities(store: Store, target_id: str) -> str:
    groups: dict[str, list] = collections.defaultdict(list)
    for f in _entities(store, target_id):
        groups[f.key.split(".")[0] + ":" + f.value["kind"]].append(f)
    lines = []
    for g, fs in sorted(groups.items()):
        unresolved = sum(1 for f in fs if not f.value["resolved"])
        lines.append(f"  {g:24} {len(fs):4}  ({len(fs) - unresolved} resolved, {unresolved} unknown)")
    return "\n".join(lines) or "  (none)"


def run_rea(path: str, cfg: DiscoveryConfig, store: Store) -> str:
    from envrec.adapters import ReaAdapter, SubprocessReaExecutor, ToolContext
    from envrec.adapters.rea import REA_TOOLS
    root = Path(__file__).resolve().parents[2]
    p = Path(path).expanduser().resolve()
    if not cfg.rea_roots or not any(p == r or r in p.parents for r in cfg.rea_roots):
        return f"refused: {p} is not under a folder listed in config/discovery.yaml rea_roots"
    ex = SubprocessReaExecutor(root / "scout" / "rea-run" / "node_modules" / ".bin" / "rea", root / "scout" / "rea-home")
    adapter = ReaAdapter(ex, list(cfg.rea_roots))
    target = store.put_target(Target(kind=TargetKind.app, ring=0, handle=str(p)))
    lines = []
    for name in REA_TOOLS:
        if adapter.available(name, target, store):
            res = asyncio.run(adapter.run(name, {}, ToolContext(store, target, None)))
            for f in res.facts:
                store.write_fact(f)
            lines.append(f"  {name}: {'ok, ' + str(len(res.facts)) + ' facts' if res.ok else 'failed: ' + str(res.error)}")
    return "REA over " + str(p) + "\n" + ("\n".join(lines) or "  no REA tool applies to this path (or REA is not installed)")


def run_active(args, cfg: DiscoveryConfig, store: Store, host: Target, salts: Salts, collector, wanted: list[str]) -> tuple[list[AdapterRun], object | None]:
    from envrec.lab import SUBNET
    try:
        scope = scope_for_interface(cap_prefix=cfg.subnet_cap_prefix, interface=args.interface)
    except ScopeError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return [record_denied(get_adapter(a), store, host, str(exc)) for a in wanted], None
    ask = (lambda _p: "approve" if str(scope.network) == SUBNET else "deny") if args.lab_approve else input
    gate = ApprovalGate(store, ask, (lambda t: None) if args.json else print)
    runs: list[AdapterRun] = []
    params = {"scope": scope, "rate": cfg.scan_rate_per_s, "cap_prefix": cfg.subnet_cap_prefix}
    for aid in wanted:
        a = get_adapter(aid)
        # the tool writes the exact plan (hosts, ports, rate, duration); the human approves that text, one skill at a time
        approve = lambda text, _aid=aid: gate.request(PendingAction(f"discover_{_aid}", text, "controller", Origin.builtin, None, None))  # noqa: E731
        runs.append(run_adapter(a, collector, store, host, salts=salts, params=params, approve=approve))
    return runs, scope


def write_proposals(store: Store, host: Target, out_dir: Path) -> Path:
    """proposals/targets.discovered.yaml: every candidate DISABLED (commented out). Enrollment stays in the enroll flow; this never edits targets.yaml."""
    out_dir.mkdir(parents=True, exist_ok=True)
    facts = store.current_facts(host.id)
    ports: dict[str, list[int]] = collections.defaultdict(list)
    names: dict[str, str] = {}
    for f in facts:
        if ".entity." in f.key and isinstance(f.value, dict):
            a = f.value.get("attrs", {})
            if f.value.get("kind") == "service" and a.get("ip") and a.get("port") and "service_type" not in a:
                ports[a["ip"]].append(a["port"])
    for f in facts:
        if f.key.startswith("net.device.") and isinstance(f.value, dict):
            names[f.value["ip"]] = f.value.get("class") or "unknown"
    lines = ["# Candidate targets found by `envrec discover --propose-targets`.",
             "# NOTHING here is active: every entry is commented out. Discovery never authorizes a device.",
             "# To enroll one, run the enrollment flow (`python -m envrec enroll`) and confirm you own it; do not paste these into targets.yaml blindly.",
             "targets: []", ""]
    n = 0
    for ip in sorted(ports):
        if 22 in ports[ip]:
            n += 1
            lines += [f"# - id: discovered-{n}", f"#   name: \"{names.get(ip, 'unknown')} at {ip}\"", f"#   host: {ip}", "#   allowed_transports: [ssh]",
                      "#   ssh: {user: CHANGE_ME, port: 22, key_env: CHANGE_ME_KEY_ENV_NAME}", "#   limits: {max_disk_gb: 4, max_model_params: \"1B\"}",
                      "#   owner_confirmed: false   # you must set this yourself", ""]
    path = out_dir / "targets.discovered.yaml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def cmd_discover(args: argparse.Namespace) -> int:
    cfg = DiscoveryConfig.load(args.config)
    out_dir = Path(args.runs_dir) / f"discover-{int(time.time())}"
    adapters = [a for a in all_adapters() if (args.ring is None or a.ring == args.ring) and (not args.adapters or a.id in args.adapters)]
    if args.explain:
        for a in adapters:
            print(f"{a.id} (ring {a.ring}, {a.tier}, {a.mode}, {'/'.join(a.platforms)}): {a.description}")
            for st in a.steps:
                argv = st.args.get("argv")
                print(f"    [{'/'.join(st.platforms)}] {st.tool}" + (f": {' '.join(str(x) for x in argv)}" if argv else ""))
        return 0
    with Store(out_dir) as store:
        host = store.put_target(Target(kind=TargetKind.host, ring=0, handle="this-machine"))
        salts = Salts(Path(args.runs_dir) / ".entity_salt")
        collector = LocalCollector()
        todo = [a for a in adapters if a.tier == "observe" and mode_of(a) == "survey"]
        runs = survey(todo, collector, store, host, salts=salts, params={"ble_scan": {"seconds": cfg.ble_scan_seconds}, "net_mdns": {"seconds": cfg.mdns_browse_seconds}}) if todo else []
        scope = None
        if args.active:
            wanted = [a for a in ACTIVE_ORDER if a in {x.id for x in adapters}] or ACTIVE_ORDER
            more, scope = run_active(args, cfg, store, host, salts=salts, collector=collector, wanted=wanted)
            runs += more
        if any(a.ring == 2 for a in adapters):
            classify_lan(store, host.id)
        if args.rea:
            print(run_rea(args.rea, cfg, store), file=sys.stderr if args.json else sys.stdout)
        if args.propose_targets:
            print("proposals written to", write_proposals(store, host, Path("proposals")), file=sys.stderr if args.json else sys.stdout)
        skipped = [a.id for a in adapters if a.id not in {r.adapter_id for r in runs}]
        if args.json:
            ents = [{"key": f.key, "status": f.status.value, **f.value} for f in _entities(store, host.id)]
            devices = [f.value for f in store.current_facts(host.id) if f.key.startswith("net.device.")]
            facts = {f.key: f.value for f in store.current_facts(host.id) if ".entity." not in f.key and not f.key.startswith(("ring.", "net.device."))}
            print(json.dumps({"store": str(out_dir), "platform": collector.platform, "scope": str(scope.network) if scope else None, "adapters": [r.__dict__ for r in runs],
                              "entities": ents, "devices": devices, "facts": facts, "not_run": skipped}, indent=1, default=str))
            return 0
        print(f"discovery on this {collector.platform} machine, store {out_dir}\n")
        print(render_table(runs))
        print("\nentities by group:\n" + render_entities(store, host.id))
        devs = [f.value for f in store.current_facts(host.id) if f.key.startswith("net.device.")]
        if devs:
            print("\nlocal network devices (class is a hypothesis):")
            for d in sorted(devs, key=lambda d: d["ip"]):
                print(f"  {d['ip']:15} {d['class'] or '?':10} {(d['vendor'] or '')[:30]:30} {', '.join(d['signals'][:3])}")
        unknown = [f for f in store.current_facts(host.id) if f.status == FactStatus.unknown and not f.key.startswith("ring.")]
        if unknown:
            reasons = {k: v for r in runs for k, v in r.unknown_reasons.items()}
            print("\nunknowns:")
            for f in unknown:
                print(f"  {f.key}: {reasons.get(f.key, 'tried, could not tell')[:200]}")
        if skipped:
            print(f"\nnot run here (menu tools that need your approval; add --active for the network ones): {', '.join(skipped)}")
    return 0


def add_discover_commands(sub) -> None:
    d = sub.add_parser("discover", help="discover what is on this machine, attached to it and on the local network (read-only)")
    d.add_argument("--ring", type=int, choices=[0, 1, 2, 3], default=None)
    d.add_argument("--adapters", nargs="*", default=None, help="adapter ids; default: all that apply")
    d.add_argument("--json", action="store_true")
    d.add_argument("--explain", action="store_true", help="list the adapters and the exact commands, run nothing")
    d.add_argument("--active", action="store_true", help="also run the network adapters that send packets (sweep, SSDP, port check), each after your approval")
    d.add_argument("--interface", default=None, help="which of your own interfaces defines the network to scan")
    d.add_argument("--propose-targets", action="store_true", help="write proposals/targets.discovered.yaml with every entry disabled")
    d.add_argument("--rea", default=None, help="a folder under rea_roots (config/discovery.yaml) for static analysis with REA")
    d.add_argument("--lab-approve", action="store_true", help=argparse.SUPPRESS)
    d.add_argument("--config", default=None)
    d.add_argument("--runs-dir", default="runs")
    d.set_defaults(func=cmd_discover)
