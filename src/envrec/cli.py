"""Command line: run the loop on this machine, or score a store.

  python -m envrec run --profile <name from config/models.yaml>
  python -m envrec score --store runs/some-run --truth ground_truth/this-machine.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from envrec.adapters import BuiltinRing0Adapter
from envrec.contracts import Target, TargetKind
from envrec.loop import Loop, RunConfig, RunSummary
from envrec.model import load_profile, make_adapter, with_view_budget
from envrec.scoring import load_ground_truth, score_target
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


def _protocols(args: argparse.Namespace):
    """The protocols of the skill library, enforced by the loop; --no-protocols runs the loop without them (the baseline for measuring them)."""
    if getattr(args, "no_protocols", False):
        return None
    from envrec.protocols import ProtocolSet
    return ProtocolSet.from_library()


def cmd_run(args: argparse.Namespace) -> int:
    profile, options = load_profile(args.models, args.profile)
    profile = with_view_budget(profile, args.view_budget, args.menu_max)
    model = make_adapter(profile, options)
    out = Path(args.runs_dir) / f"{args.profile}-{int(time.time())}"
    with Store(out) as store:
        host = store.put_target(Target(kind=TargetKind.host, ring=0, handle="this-machine"))
        loop = Loop(store, [BuiltinRing0Adapter()], model,
                    config=RunConfig(max_steps=args.max_steps, max_seconds=args.max_seconds,
                                     protocols=_protocols(args)))
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


def cmd_catalog(args) -> int:
    from envrec.catalog import generate, write
    from envrec.catalog.live import LiveMetadata
    from envrec.runtime.manifest import Manifest, NotApproved

    manifest = Manifest.load(args.manifest)
    try:
        manifest.require_approved()
    except NotApproved as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 2
    gen = generate(manifest, LiveMetadata(github_token=os.environ.get("GITHUB_TOKEN")))
    write(gen, args.lock, args.excluded)
    print(f"catalog: {gen.lock.counts}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m envrec", description="Environment recognition harness.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="run the loop on this machine (Ring 0) and score it")
    run.add_argument("--profile", required=True, help="a profile name from the models file")
    run.add_argument("--models", default=str(DEFAULT_MODELS))
    run.add_argument("--truth", default=str(DEFAULT_TRUTH))
    run.add_argument("--no-protocols", action="store_true", help="run without the protocol state machine (the baseline)")
    run.add_argument("--runs-dir", default="runs")
    run.add_argument("--view-budget", type=int, default=None, help="override view_budget_facts (the one dial)")
    run.add_argument("--menu-max", type=int, default=None)
    run.add_argument("--no-repair", action="store_true", help="do not read menu-tool-name actions as call_tool")
    run.add_argument("--max-steps", type=int, default=25)
    run.add_argument("--max-seconds", type=float, default=300.0)
    run.set_defaults(func=cmd_run)

    score = sub.add_parser("score", help="score a finished store against a ground truth")
    score.add_argument("--store", required=True)
    score.add_argument("--truth", default=str(DEFAULT_TRUTH))
    score.set_defaults(func=cmd_score)

    cat = sub.add_parser("catalog", help="generate catalog.lock.yaml + catalog.excluded.md from the approved sources")
    cat.add_argument("--manifest", default="config/runtime_manifest.yaml")
    cat.add_argument("--lock", default="catalog.lock.yaml")
    cat.add_argument("--excluded", default="catalog.excluded.md")
    cat.set_defaults(func=cmd_catalog)

    from envrec.cli_discover import add_discover_commands
    add_discover_commands(sub)
    from envrec.data_fetch import add_data_commands
    add_data_commands(sub)
    from envrec.lab import add_lab_commands
    add_lab_commands(sub)
    from envrec.cli_tools import add_tools_commands
    add_tools_commands(sub)
    from envrec.cli_fleet import add_fleet_commands
    add_fleet_commands(sub)
    from envrec.cli_skills import add_skills_commands
    add_skills_commands(sub)
    from envrec.cli_helper import add_helper_commands
    add_helper_commands(sub)
    from envrec.cli_media import add_media_commands
    add_media_commands(sub)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
