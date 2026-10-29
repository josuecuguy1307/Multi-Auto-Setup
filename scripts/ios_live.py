#!/usr/bin/env python3
"""The owner's own enrolled iPhone, over USB, driven by the local model through the phone protocol. Run it in a terminal: every read asks you first.

  python scripts/ios_live.py --target <id in targets.yaml> --profile muse-glimmer

The model picks every action. This script only builds the loop, shows each step and each approval as it happens (you type y/n), and prints the count of model calls. It never reads
the phone itself. The device name, serial and UDID are never printed or stored in clear."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from envrec.cli import DEFAULT_MODELS  # noqa: E402
from envrec.contracts import Target, TargetKind  # noqa: E402
from envrec.discovery import LocalCollector, Salts  # noqa: E402
from envrec.loop import Loop, RunConfig  # noqa: E402
from envrec.mission import load_targets  # noqa: E402
from envrec.model import load_profile, make_adapter  # noqa: E402
from envrec.protocols import ProtocolSet  # noqa: E402
from envrec.remote.runner import RemoteRunner  # noqa: E402
from envrec.skills.lib import Library  # noqa: E402
from envrec.store import Store  # noqa: E402
from envrec.tools.bridge import RegistryAdapter  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True)
    ap.add_argument("--profile", default="muse-glimmer")
    ap.add_argument("--targets", default="config/targets.yaml")
    ap.add_argument("--models", default=str(DEFAULT_MODELS))
    ap.add_argument("--runs-dir", default="runs")
    ap.add_argument("--max-steps", type=int, default=16)
    a = ap.parse_args()
    cfg = load_targets(a.targets)
    entry = cfg.get(a.target)
    if entry is None or entry.ios_usb is None:
        print(f"{a.target!r} is not an enrolled iPhone entry in {a.targets}", file=sys.stderr)
        return 2
    profile, options = load_profile(a.models, a.profile)
    model = make_adapter(profile, options)
    asked = {"n": 0}

    def approve(text: str) -> bool:
        asked["n"] += 1
        print(f"\n[approval {asked['n']}] the model asked to: {text}")
        return input("  allow? [y/N] ").strip().lower() == "y"

    def human(question: str) -> str | None:
        print(f"\n[the model asks you] {question[:300]}")
        ans = input("  your answer (Enter = none): ").strip()
        return ans or None

    out = Path(a.runs_dir) / f"ios-live-{int(time.time())}"
    out.mkdir(parents=True)
    lib = Library().load()
    reg = RegistryAdapter(collector=LocalCollector(), salts=Salts(), approve=approve, library=lib, params={"ios_runner": RemoteRunner(cfg), "ios_target": entry.id})
    with Store(out / "store") as st:
        target = st.put_target(Target(kind=TargetKind.phone, ring=1, handle=f"ios:{entry.id}"))
        loop = Loop(st, [reg], model, config=RunConfig(max_steps=a.max_steps, max_seconds=600, protocols=ProtocolSet.from_library(lib), library=lib), ask_human=human)
        s = asyncio.run(loop.run(target))
        facts = {f.key: (f.value, f.status.value) for f in st.current_facts(target.id) if f.key.startswith("ios.")}
    result = {"model": profile.model_id, "stop": s.stop_reason, "steps": s.steps, "model_calls": s.model_calls, "invalid_replies": s.invalid_replies,
              "protocol_refusals": s.protocol_refusals, "tools_run": s.tools_run, "approvals_asked": asked["n"], "facts": facts,
              "protocol_history": [h["state"] for h in next(iter(s.protocol_history.values()), [])]}
    (out / "result.json").write_text(json.dumps(result, indent=1, default=str))
    print("\n" + json.dumps(result, indent=1, default=str))
    print("result:", out / "result.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
