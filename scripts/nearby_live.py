#!/usr/bin/env python3
"""What the owner carries (phone, earbuds, watch), seen passively over Bluetooth, with the local model choosing every action through the BLE protocol.

  python scripts/nearby_live.py --profile muse-glimmer

The model picks each step; this script only builds the loop. The scan goes through envrec Helper when macOS blocks the direct probe. Nothing is paired, connected or written."""
from __future__ import annotations

import argparse
import asyncio
import collections
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
from envrec.model import load_profile, make_adapter, with_view_budget  # noqa: E402
from envrec.protocols import ProtocolSet  # noqa: E402
from envrec.skills.lib import Library  # noqa: E402
from envrec.store import Store  # noqa: E402
from envrec.tools.bridge import RegistryAdapter  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="muse-glimmer")
    ap.add_argument("--models", default=str(DEFAULT_MODELS))
    ap.add_argument("--runs-dir", default="runs")
    ap.add_argument("--max-steps", type=int, default=14)
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--skills", default=None, help="a skills directory (default: skills/); used to run the ORIGINAL multi-tool listen menu")
    ap.add_argument("--approval-dir", default=None, help="relay approvals through files instead of stdin: the prompt is written to <dir>/prompt-N.txt and the run waits for the owner's answer in <dir>/answer-N.txt (y or n); whoever relays must copy the owner's own answer")
    ap.add_argument("--label", default="run", help="a name for this run in the result file, for example original-menu or after-harness-change")
    a = ap.parse_args()
    profile, options = load_profile(a.models, a.profile)
    model = make_adapter(with_view_budget(profile, None, None), options)
    out = Path(a.runs_dir) / f"nearby-{int(time.time())}"
    lib = Library(root=Path(a.skills)).load() if a.skills else Library().load()
    counter = [0]

    def approve(text: str) -> bool:
        print(f"\n[approval] {text}", flush=True)
        if a.approval_dir:  # the owner is not at this process's terminal: the prompt goes to a file, the answer comes back through a file, and the wait is bounded
            d = Path(a.approval_dir)
            d.mkdir(parents=True, exist_ok=True)
            counter[0] += 1
            (d / f"prompt-{counter[0]}.txt").write_text(text)
            end = time.monotonic() + 600
            while time.monotonic() < end:
                if (d / f"answer-{counter[0]}.txt").is_file():
                    return (d / f"answer-{counter[0]}.txt").read_text().strip().lower() == "y"
                time.sleep(1)
            return False
        return input("  allow? [y/N] ").strip().lower() == "y"

    reg = RegistryAdapter(collector=LocalCollector(), salts=Salts(), library=lib, params={"seconds": a.seconds}, approve=approve)
    with Store(out) as st:
        target = st.put_target(Target(kind=TargetKind.ble, ring=1, handle="nearby"))
        loop = Loop(st, [reg], model, config=RunConfig(max_steps=a.max_steps, max_seconds=420, protocols=ProtocolSet.from_library(lib), library=lib), ask_human=lambda q: None)
        s = asyncio.run(loop.run(target))
        facts = [(f.key, f.value, f.status.value) for f in st.current_facts(target.id) if f.status.value != "unknown" or not f.key.startswith("ring.")]
        events = collections.Counter((e.actor.value if hasattr(e.actor, "value") else str(e.actor), e.type) for e in st.events())
        unknown = [(f.key) for f in st.current_facts(target.id) if f.status.value == "unknown" and not f.key.startswith("ring.")]
        menu_sizes = [len(e.payload.get("menu", [])) for e in st.events(type="view_built")]
        turns = [(e.payload["action"], e.payload.get("tool"), e.payload.get("validation"), len(e.payload.get("menu", []))) for e in st.events(type="action_chosen")]
        approvals = [(e.payload["tool"] if "tool" in e.payload else e.payload.get("action"), e.payload.get("granted")) for e in st.events(type="approval")]
    res = {"label": a.label, "real": True, "dial": "ON" if s.walk_for_model else "off", "menu_sizes_per_turn": menu_sizes, "empty_menu_turns": s.empty_menu_turns, "empty_menu_causes": s.empty_menu_causes, "turns": turns, "approvals": approvals, "model": profile.model_id, "stop": s.stop_reason, "steps": s.steps, "model_calls": s.model_calls, "invalid_replies": s.invalid_replies, "fallbacks": s.fallbacks,
           "protocol_refusals": s.protocol_refusals, "tools_run": s.tools_run, "protocol_history": next(iter(s.protocol_history.values()), []),
           "events_by_actor": {f"{k[0]}:{k[1]}": v for k, v in sorted(events.items())}, "unknown": unknown, "facts": facts, "seconds": round(s.elapsed_s, 1)}
    (out / "result.json").write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps(res, indent=1, default=str))
    print("result:", out / "result.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
