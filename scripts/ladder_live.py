#!/usr/bin/env python3
"""The 9B, LIVE, on the unknown ladder, in a SIMULATED world: an invented operating system ("zorg") whose only manual is the real llama.cpp server README on the real internet (fetched by the
model itself with http_get). The owner is simulated (every prompt answered yes and recorded). Nothing real is touched. This measures the harness and the model on the ladder, not Windows.

  python scripts/ladder_live.py --profile muse-glimmer [--stop-at generate]"""
from __future__ import annotations

import argparse
import asyncio
import collections
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tests" / "fakes"))

import test_mission1_flow as m  # noqa: E402
from envrec.cli import DEFAULT_MODELS  # noqa: E402
from envrec.discovery import LocalCollector, Salts  # noqa: E402
from envrec.loop import Loop, RunConfig  # noqa: E402
from envrec.mission import MissionCard  # noqa: E402
from envrec.model import load_profile, make_adapter, with_view_budget  # noqa: E402
from envrec.protocols import ProtocolSet  # noqa: E402
from envrec.remote.provision import sync_targets  # noqa: E402
from envrec.skills.verify import ModelBaseline, ModelVerifier  # noqa: E402
from envrec.skills.workbench import Workbench  # noqa: E402
from envrec.store import Store  # noqa: E402
from envrec.tools.bridge import RegistryAdapter  # noqa: E402
from envrec.mission import TargetConfig  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="muse-glimmer")
    ap.add_argument("--models", default=str(DEFAULT_MODELS))
    ap.add_argument("--max-steps", type=int, default=50)
    ap.add_argument("--stop-at", default=None)
    a = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="ladder-live-"))
    w = m.World(tmp)
    profile, options = load_profile(a.models, a.profile)
    model = make_adapter(with_view_budget(profile, None, None), options)
    w.baseline, w.verifier = ModelBaseline(model), ModelVerifier(model)
    out = ROOT / "runs" / f"ladder-live-{int(time.time())}"
    t0 = time.monotonic()
    with Store(out) as st:
        target = sync_targets(st, TargetConfig(targets=[w.entry]))["toy-box"]
        reg = RegistryAdapter(collector=LocalCollector(), salts=Salts(tmp / "salt"), library=w.lib, shelf=w.shelf, params=w.params(), approve=w.owner, ask_skill_approval=w.owner)
        wb = Workbench(w.lib, tmp / "stage", docs_dir=None, verifier=w.verifier, baseline=w.baseline, shelf=w.shelf)
        loop = Loop(st, [reg], model, config=RunConfig(max_steps=a.max_steps, max_seconds=1500, protocols=ProtocolSet.from_library(w.lib), library=w.lib, stop_at=a.stop_at), workbench=wb,
                    mission=MissionCard(goal=m.GOAL, scope="the enrolled computer; its operating system is not one the skills know"), ask_human=lambda q: None)
        s = asyncio.run(loop.run(target))
        acts = [(e.payload["action"], e.payload.get("tool"), e.payload.get("validation")) for e in st.events(type="action_chosen")]
        res = {"label": "SIMULATED world, LIVE model", "model": profile.model_id, "stop": s.stop_reason, "steps": s.steps, "model_calls": s.model_calls, "invalid": s.invalid_replies, "hangs": s.hangs,
               "tokens": {"prompt": s.prompt_tokens, "completion": s.completion_tokens}, "seconds": round(time.monotonic() - t0, 1), "actors": dict(collections.Counter(e.tag for e in st.events())),
               "menu_sizes": [len(e.payload["menu"]) for e in st.events(type="view_built")], "protocol": [h["state"] for h in next(iter(s.protocol_history.values()), [])],
               "actions": acts, "tools_run": s.tools_run, "skill_results": [e.payload["result"][:300] for e in st.events(type="skill_result")],
               "fetched": [c["source"] for c in st.captures(target.id) if c["tool"] == "http_get"], "removed": w.computer.files == {} and not w.computer.running,
               "research": [e.payload.get("question") for e in st.events(type="research")], "stop_detail": [e.payload for e in st.events(type="action_rejected")][-3:]}
    (out / "result.json").write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps(res, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
