#!/usr/bin/env python3
"""Mission 1: the local model sets up a workspace on the owner's enrolled computer. This script only BUILDS the loop and records afterwards; it never calls a tool during the run and never
answers for the owner (a prompt goes to the terminal, or to files in --relay-dir for whoever relays the owner's own answer).

  python scripts/mission1.py --target <id> [--read-only]      # --read-only: open the door and read the system, deploy nothing (stops before the ladder)

Needs, done by the owner: `python -m envrec enroll` (typed name + yes) for the computer. Everything else is discovered by the model through the loop."""
from __future__ import annotations

import argparse
import asyncio
import collections
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from envrec.cli import DEFAULT_MODELS  # noqa: E402
from envrec.discovery import LocalCollector, Salts  # noqa: E402
from envrec.discovery.scope import is_lan_address, own_networks  # noqa: E402
from envrec.doors import pin, ssh_keys  # noqa: E402
from envrec.loop import Loop, RunConfig  # noqa: E402
from envrec.mission import MissionCard, load_targets  # noqa: E402
from envrec.model import load_profile, make_adapter, with_view_budget  # noqa: E402
from envrec.protocols import ProtocolSet  # noqa: E402
from envrec.recipes.executor import UndoJournal  # noqa: E402
from envrec.remote.provision import sync_targets  # noqa: E402
from envrec.remote.runner import RemoteRunner  # noqa: E402
from envrec.skills.lib import Library  # noqa: E402
from envrec.skills.verify import ModelBaseline, ModelVerifier  # noqa: E402
from envrec.skills.workbench import Shelf, Workbench  # noqa: E402
from envrec.store import Store  # noqa: E402
from envrec.tools.bridge import RegistryAdapter  # noqa: E402

GOAL = "Set up a working small-model workspace on the enrolled laptop, verify it works, and report."


def lan_address() -> str | None:
    return next((str(ifc.ip) for _name, ifc in own_networks() if is_lan_address(ifc.ip)), None)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default=None)
    ap.add_argument("--door", action="store_true", help="the door phase: key, one-time link, wait for the other computer to report, pair, propose (no target is enrolled yet)")
    ap.add_argument("--profile", default="muse-glimmer")
    ap.add_argument("--models", default=str(DEFAULT_MODELS))
    ap.add_argument("--targets", default="targets.yaml")
    ap.add_argument("--runs-dir", default="runs")
    ap.add_argument("--relay-dir", default=None, help="prompts go to prompt-N.txt here and the owner's own answer comes back in answer-N.txt")
    ap.add_argument("--read-only", action="store_true")
    ap.add_argument("--link-ttl", type=float, default=300.0, help="seconds the one-time link lives (default 5 minutes)")
    ap.add_argument("--max-steps", type=int, default=60)
    a = ap.parse_args()
    cfg = load_targets(a.targets)
    if a.door:
        entry = next((e for e in cfg.targets if e.local is not None and e.local.scope == "door"), None)
        if entry is None:
            print("No door entry in targets.yaml: run `python -m envrec enroll-local --door` first.")
            return 2
    else:
        entry = cfg.get(a.target or "")
        if entry is None or entry.ssh is None:
            print(f"{a.target!r} is not an SSH entry in {a.targets}: enroll it first (`python -m envrec enroll-proposed`).")
            return 2
    counter = [0]

    def relay(kind: str, text: str) -> str:
        print(f"\n[{kind}] {text}", flush=True)
        if not a.relay_dir:
            return input("  > ")
        d = Path(a.relay_dir)
        d.mkdir(parents=True, exist_ok=True)
        counter[0] += 1
        (d / f"prompt-{counter[0]}.txt").write_text(text)
        end = time.monotonic() + 1800
        while time.monotonic() < end:
            if (d / f"answer-{counter[0]}.txt").is_file():
                return (d / f"answer-{counter[0]}.txt").read_text().strip()
            time.sleep(1)
        return "no"

    owner = lambda text: relay("approval", text).lower() in ("y", "yes")  # noqa: E731
    out = Path(a.runs_dir) / f"mission1-{int(time.time())}"
    known_hosts = Path(pin.KNOWN_HOSTS)
    if entry.ssh is not None:
        os.environ.setdefault(entry.ssh.key_env, str(ssh_keys.key_path(entry.id)))  # the variable NAME is in targets.yaml; it points at the key this run made
    profile, options = load_profile(a.models, a.profile)
    model = make_adapter(with_view_budget(profile, None, None), options)
    lib, shelf, salts = Library().load(), Shelf(), Salts()
    runner = RemoteRunner(cfg, host_key_policy="yes", known_hosts=str(known_hosts))
    def show(m: str) -> None:
        print(f"\n[for the owner] {m}", flush=True)
        if a.relay_dir:
            Path(a.relay_dir).mkdir(parents=True, exist_ok=True)
            (Path(a.relay_dir) / "link.txt").write_text(m)

    params = {"remote_runner": runner, "remote_target": entry.id, "remote_host": entry.host, "remote_port": entry.ssh.port if entry.ssh else 22, "library": lib, "journal": UndoJournal(out / "undo.jsonl"),
              "lan_address": lan_address(), "show": show, "ask_code": lambda q: relay("owner types", q), "known_hosts": known_hosts, "door_name": "lan-peer", "proposals_dir": ROOT / "proposals", "link_options": {"ttl_s": a.link_ttl}, "emit": lambda kind, info: print(f"[link] {kind} {info}", flush=True)}
    t0 = time.monotonic()
    with Store(out) as st:
        target = sync_targets(st, cfg)[entry.id]
        reg = RegistryAdapter(collector=LocalCollector(), salts=salts, library=lib, shelf=shelf, params=params, approve=owner, ask_skill_approval=owner)
        wb = Workbench(lib, out / "stage", docs_dir=ROOT / "docs" / "external", verifier=ModelVerifier(model), baseline=ModelBaseline(model), shelf=shelf)
        loop = Loop(st, [reg], model, config=RunConfig(max_steps=a.max_steps, max_seconds=1500, protocols=ProtocolSet.from_library(lib), library=lib, stop_at="plan" if a.read_only else None),
                    workbench=wb, mission=MissionCard(goal=("Open the door to the owner's other computer: find out who it is and propose it as a target." if a.door else GOAL), scope="the other computer" if a.door else "the enrolled computer"), ask_human=lambda q: None)
        summary = asyncio.run(loop.run(target))
        cap_dir = out / "captures"  # the REAL read-only captures, for replay (the sandbox tests of a proposed skill are made from these)
        cap_dir.mkdir(exist_ok=True)
        for c in st.captures(target.id):
            if c["tool"] in ("remote_read", "skill.remote_os_probe") or c["name"] in ("uname",):
                (cap_dir / f"{c['name']}.txt").write_bytes(st.read_raw(st.resolve_capture(target.id, c["sha256"][:12])))
        res = {"label": "read-only" if a.read_only else "full", "real": True, "model": profile.model_id, "stop": summary.stop_reason, "steps": summary.steps, "model_calls": summary.model_calls,
               "invalid_replies": summary.invalid_replies, "hangs": summary.hangs, "tokens": {"prompt": summary.prompt_tokens, "completion": summary.completion_tokens},
               "seconds": {"run": round(time.monotonic() - t0 - summary.owner_wait_s, 1), "model": round(summary.model_latency_s, 1), "owner_wait": summary.owner_wait_s},
               "steps_by_actor": dict(collections.Counter(e.tag for e in st.events())), "tools_run": summary.tools_run, "protocol_history": next(iter(summary.protocol_history.values()), []),
               "facts": {f.key: f.value for f in st.current_facts(target.id) if f.status.value != "unknown" and not f.key.startswith("ring.")}}
    (out / "result.json").write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps(res, indent=1, default=str))
    print("result:", out / "result.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
