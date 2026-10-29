"""Command line: run the loop on this machine, or score a store.

  python -m envrec run --profile <name from config/models.yaml>
  python -m envrec score --store runs/some-run --truth ground_truth/this-machine.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from envrec.adapters import BuiltinRing0Adapter
from envrec.contracts import Target, TargetKind
from envrec.fingerprint import UsbIds
from envrec.ladder import LadderAdapter, load_devices
from envrec.loop import Loop, RunConfig, RunSummary
from envrec.model import load_profile, make_adapter, with_view_budget
from envrec.scoring import fingerprint_stats, highest_rung, load_ground_truth, render_stats, score_target
from envrec.store import Store

DEFAULT_MODELS = Path("config/models.yaml")
DEFAULT_TRUTH = Path("ground_truth/this-machine.yaml")


def _print_summary(s: RunSummary, profile_name: str, model_id: str) -> None:
    print(f"profile {profile_name} ({model_id})")
    print(f"  stop: {s.stop_reason}   steps: {s.steps}   model calls: {s.model_calls}   elapsed: {s.elapsed_s:.1f}s")
    print(f"  invalid replies: {s.invalid_replies}   repaired: {s.repaired}   fallbacks: {s.fallbacks}   "
          f"rejected actions: {s.rejected_actions}   policy denials: {s.policy_denied}")
    print(f"  tokens: {s.prompt_tokens} prompt / {s.completion_tokens} completion   "
          f"model time: {s.model_latency_s:.1f}s")
    print(f"  tools run ({len(s.tools_run)}): {', '.join(s.tools_run) or '-'}")
    print(f"  ring decisions: {s.ring_decisions}")


def _cards(spec: str | None) -> list[str] | None:
    """--cards none | host,usb ; None keeps the profile's own list."""
    if spec is None:
        return None
    return [] if spec.strip().lower() in ("", "none") else [c.strip() for c in spec.split(",")]


def cmd_run(args: argparse.Namespace) -> int:
    profile, options = load_profile(args.models, args.profile)
    profile = with_view_budget(profile, args.view_budget, args.menu_max, _cards(args.cards))
    model = make_adapter(profile, options)
    out = Path(args.runs_dir) / f"{args.profile}-{int(time.time())}"
    with Store(out) as store:
        host = store.put_target(Target(kind=TargetKind.host, ring=0, handle="this-machine"))
        loop = Loop(store, [BuiltinRing0Adapter()], model,
                    config=RunConfig(max_steps=args.max_steps, max_seconds=args.max_seconds,
                                     playbook_dir=Path(args.playbooks)))
        summary = asyncio.run(loop.run(host))
        _print_summary(summary, profile.name, profile.model_id)
        print(f"  store: {out}")
        report = None
        if args.truth and Path(args.truth).is_file():
            report = score_target(store, host.id, load_ground_truth(args.truth))
            print()
            print(report.render())
        (out / "summary.json").write_text(json.dumps({
            "profile": profile.name, "model_id": profile.model_id, "view_budget_facts": profile.view_budget_facts,
            "menu_max": profile.menu_max, "summary": summary.__dict__,
            "score": report.to_dict() if report else None}, indent=2, default=str), encoding="utf-8")
    return 0


FIXTURES = ("ch340-gps", "mystery-box", "liar-device")


def cmd_ladder(args: argparse.Namespace) -> int:
    """Run the unknown-ladder fixtures (replay devices) with a live model and score each."""
    profile, options = load_profile(args.models, args.profile)
    profile = with_view_budget(profile, args.view_budget, args.menu_max, _cards(args.cards))
    model = make_adapter(profile, options)
    root = Path(args.fixtures)
    usb_ids = UsbIds.from_file(args.usb_ids)
    out = Path(args.runs_dir) / f"ladder-{args.profile}-{int(time.time())}"
    rows = []
    with Store(out) as store:
        for name in args.fixture or FIXTURES:
            registry = load_devices(root / "replay")
            device = registry.get(f"replay:{name}")
            target = store.put_target(Target(kind=TargetKind.usb, ring=1, handle=device.handle))
            loop = Loop(store, [LadderAdapter(registry, usb_ids)], model,
                        config=RunConfig(max_steps=args.max_steps, max_seconds=args.max_seconds,
                                         playbook_dir=Path(args.playbooks), repair=not args.no_repair))
            s = asyncio.run(loop.run(target))
            report = score_target(store, target.id, load_ground_truth(Path(args.truth_dir) / f"replay-{name}.yaml"))
            facts = {f.key: f for f in store.current_facts(target.id)}
            proto = facts.get("serial.protocol")
            verified = sum(1 for f in facts.values() if f.status.value == "verified")
            rows.append({"fixture": name, "steps": s.steps, "stop": s.stop_reason, "invalid": s.invalid_replies, "repaired": s.repaired,
                         "fallbacks": s.fallbacks, "rejected": s.rejected_actions, "rung": highest_rung(store, target.id),
                         "tools": s.tools_run, "protocol": f"{proto.value if proto else None} ({proto.status.value if proto else '-'})",
                         "correct": report.count("correct"), "scored": len(report.rows),
                         "wrong": report.count("wrong"), "contradicted": report.count("contradicted"),
                         "verified": verified, "tokens": s.prompt_tokens + s.completion_tokens,
                         "seconds": round(s.elapsed_s, 1)})
        stats = fingerprint_stats(store)
    print(f"profile {profile.name} ({profile.model_id})  view_budget={profile.view_budget_facts} menu_max={profile.menu_max}")
    for r in rows:
        print(f"  {r['fixture']:12} steps {r['steps']:2}  invalid {r['invalid']}  repaired {r['repaired']}  fallback {r['fallbacks']}  "
              f"rejected {r['rejected']}  highest rung {r['rung']}  score {r['correct']}/{r['scored']}"
              f" (wrong {r['wrong']}, contradicted {r['contradicted']})  verified {r['verified']}  "
              f"protocol {r['protocol']}  {r['seconds']}s")
        print(f"               tools: {', '.join(r['tools']) or '-'}")
    print("  list-based hypotheses:\n    " + render_stats(stats).replace("\n", "\n    "))
    print(f"  store: {out}")
    (out / "ladder_summary.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    with Store(args.store) as store:
        host = next((t for t in store.list_targets() if t.kind == TargetKind.host and t.ring == 0), None)
        if host is None:
            print("no host target in that store", file=sys.stderr)
            return 2
        report = score_target(store, host.id, load_ground_truth(args.truth))
        print(report.render())
        print("\nlist-based hypotheses:\n" + render_stats(fingerprint_stats(store)))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m envrec", description="Environment recognition harness.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="run the loop on this machine (Ring 0) and score it")
    run.add_argument("--profile", required=True, help="a profile name from the models file")
    run.add_argument("--models", default=str(DEFAULT_MODELS))
    run.add_argument("--truth", default=str(DEFAULT_TRUTH))
    run.add_argument("--playbooks", default="playbooks")
    run.add_argument("--runs-dir", default="runs")
    run.add_argument("--view-budget", type=int, default=None, help="override view_budget_facts (the one dial)")
    run.add_argument("--menu-max", type=int, default=None)
    run.add_argument("--no-repair", action="store_true", help="do not read menu-tool-name actions as call_tool")
    run.add_argument("--cards", default=None, help="playbook cards to show: 'none' or e.g. 'host,usb'")
    run.add_argument("--max-steps", type=int, default=25)
    run.add_argument("--max-seconds", type=float, default=300.0)
    run.set_defaults(func=cmd_run)

    lad = sub.add_parser("ladder", help="run the unknown-device ladder fixtures (replay devices) with a live model")
    lad.add_argument("--profile", required=True)
    lad.add_argument("--models", default=str(DEFAULT_MODELS))
    lad.add_argument("--fixture", action="append", choices=FIXTURES, help="repeatable; default: all three")
    lad.add_argument("--fixtures", default="fixtures")
    lad.add_argument("--truth-dir", default="ground_truth")
    lad.add_argument("--usb-ids", default="fixtures/samples/usb_ids_sample.txt")
    lad.add_argument("--playbooks", default="playbooks")
    lad.add_argument("--runs-dir", default="runs")
    lad.add_argument("--view-budget", type=int, default=None)
    lad.add_argument("--menu-max", type=int, default=None)
    lad.add_argument("--no-repair", action="store_true", help="do not read menu-tool-name actions as call_tool")
    lad.add_argument("--cards", default=None, help="playbook cards to show: 'none' or e.g. 'host,usb'")
    lad.add_argument("--max-steps", type=int, default=20)
    lad.add_argument("--max-seconds", type=float, default=300.0)
    lad.set_defaults(func=cmd_ladder)

    score = sub.add_parser("score", help="score a finished store against a ground truth")
    score.add_argument("--store", required=True)
    score.add_argument("--truth", default=str(DEFAULT_TRUTH))
    score.set_defaults(func=cmd_score)

    from envrec.cli_fleet import add_fleet_commands
    add_fleet_commands(sub)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
