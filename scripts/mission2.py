#!/usr/bin/env python3
"""Mission 2: the local model connects the owner's earbuds and plays a track by the named artist from the owner's Spotify. This script only BUILDS the loop and AFTERWARDS verifies, from the
world, by code; it never calls a tool during the run and never answers an approval (it relays the owner's own answer: a terminal prompt, or --approval-dir files).

  python scripts/mission2.py --profile muse-glimmer [--approval-dir DIR]

Needs, done by the owner once: `envrec enroll-local`, `envrec spotify-login --client-id ...`, `envrec helper build`, the earbuds paired in macOS."""
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

from envrec import helper  # noqa: E402
from envrec.cli import DEFAULT_MODELS  # noqa: E402
from envrec.discovery import LocalCollector, Salts  # noqa: E402
from envrec.loop import Loop, RunConfig  # noqa: E402
from envrec.mission import MissionCard, load_targets  # noqa: E402
from envrec.model import load_profile, make_adapter, with_view_budget  # noqa: E402
from envrec.protocols import ProtocolSet  # noqa: E402
from envrec.remote.provision import sync_targets  # noqa: E402
from envrec.skills.lib import Library  # noqa: E402
from envrec.store import Store  # noqa: E402
from envrec.tools import ToolContext  # noqa: E402
from envrec.tools.builtin import media  # noqa: E402
from envrec.tools.bridge import RegistryAdapter  # noqa: E402
from envrec.undo import run_undo  # noqa: E402
from envrec.verify import media_check as mc  # noqa: E402

GOAL = "Connect to my earbuds and play a Harry Styles song from my Spotify."
ARTIST = "Harry Styles"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="muse-glimmer")
    ap.add_argument("--models", default=str(DEFAULT_MODELS))
    ap.add_argument("--targets", default="targets.yaml")
    ap.add_argument("--runs-dir", default="runs")
    ap.add_argument("--approval-dir", default=None)
    ap.add_argument("--max-steps", type=int, default=30)
    ap.add_argument("--label", default="run")
    a = ap.parse_args()
    cfg = load_targets(a.targets)
    entry = next((e for e in cfg.targets if e.local is not None), None)
    if entry is None:
        print("No local entry in targets.yaml: run `python -m envrec enroll-local` first.")
        return 2
    counter = [0]

    def approve(text: str) -> bool:
        print(f"\n[approval] {text}", flush=True)
        if a.approval_dir:
            d = Path(a.approval_dir)
            d.mkdir(parents=True, exist_ok=True)
            counter[0] += 1
            (d / f"prompt-{counter[0]}.txt").write_text(text)
            end = time.monotonic() + 900
            while time.monotonic() < end:
                if (d / f"answer-{counter[0]}.txt").is_file():
                    return (d / f"answer-{counter[0]}.txt").read_text().strip().lower() == "y"
                time.sleep(1)
            return False
        return input("  allow? [y/N] ").strip().lower() == "y"

    profile, options = load_profile(a.models, a.profile)
    model = make_adapter(with_view_budget(profile, None, None), options)
    lib = Library().load()
    salts = Salts()
    out = Path(a.runs_dir) / f"mission2-{int(time.time())}"
    t0 = time.monotonic()
    with Store(out) as st:
        target = sync_targets(st, cfg)[entry.id]
        reg = RegistryAdapter(collector=LocalCollector(), salts=salts, library=lib, approve=approve)
        loop = Loop(st, [reg], model, config=RunConfig(max_steps=a.max_steps, max_seconds=900, protocols=ProtocolSet.from_library(lib), library=lib),
                    mission=MissionCard(goal=GOAL, scope="this computer's paired devices, audio output and Spotify"), ask_human=lambda q: None)
        summary = asyncio.run(loop.run(target))
        run_s = time.monotonic() - t0 - summary.owner_wait_s

        # ---- verification: from the world, by code, independent of the model and of the tools it used ------------------------------------------------
        facts = {f.key: f.value for f in st.current_facts(target.id)}
        device, track = facts.get("media.bt_device"), facts.get("spotify.candidate_id")
        checks = []
        if device and track:
            checks.append(mc.check_connected(device, salts.stable))
            checks.append(mc.check_playing_through(device, salts.stable, track, ARTIST, read_player=media.read_player))
        else:
            checks.append(mc.Check("earbuds_connected", False, {"reason": "the run never connected a device" if not device else "no track was chosen"}))
            checks.append(mc.Check("plays_through_earbuds", False, {"reason": "no device or no track"}))

        # ---- put things back (each step asks the owner again), then confirm --------------------------------------------------------------------------
        undo = run_undo(st, target, lambda: ToolContext(LocalCollector(), reg.platform, st, target, salts, {}, approve))
        previous = facts.get("media.output_current")
        try:
            outs = helper.audio_outputs(salts.stable, approve=lambda text: approve(text))
            now = next((o["id"] for o in outs if o.get("current")), None)
        except helper.HelperError as exc:
            now = None
            print(f"could not confirm the restore: {exc}")
        restore = mc.restore_confirmed(str(previous or ""), now)

        events = st.events()
        tags = collections.Counter(e.tag for e in events)
        res = {
            "label": a.label, "real": True, "goal": GOAL, "model": profile.model_id, "dial": "ON" if summary.walk_for_model else "off", "stop": summary.stop_reason,
            "steps": summary.steps, "model_calls": summary.model_calls, "invalid_replies": summary.invalid_replies, "fallbacks": summary.fallbacks, "protocol_refusals": summary.protocol_refusals,
            "rejected_actions": summary.rejected_actions, "repeat_refusals": summary.repeat_refusals, "tokens": {"prompt": summary.prompt_tokens, "completion": summary.completion_tokens},
            "seconds": {"run": round(run_s, 1), "model": round(summary.model_latency_s, 1), "owner_wait": summary.owner_wait_s},
            "steps_by_actor": dict(tags), "menu_sizes": [len(e.payload.get("menu", [])) for e in st.events(type="view_built")],
            "empty_menu_causes": summary.empty_menu_causes, "tools_run": summary.tools_run, "protocol_history": next(iter(summary.protocol_history.values()), []),
            "approvals_shown": [{"action": e.payload.get("action") or e.payload.get("tool"), "granted": e.payload.get("granted"), "plan": (e.payload.get("plan") or "")[:300]} for e in st.events(type="approval")],
            "checks": [{"name": c.name, "passed": c.passed, "evidence": c.evidence} for c in checks], "restore": {"passed": restore.passed, "evidence": restore.evidence},
            "undo_steps": [{"step": u.step, "ok": u.ok} for u in undo],
            "goal_passed": all(c.passed for c in checks) and tags.get("CLAUDE-CODE", 0) == 0 and tags.get("SCRIPT", 0) == 0,
        }
    (out / "result.json").write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps(res, indent=1, default=str))
    print("result:", out / "result.json")
    return 0 if res["goal_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
