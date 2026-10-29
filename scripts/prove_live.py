#!/usr/bin/env python3
"""The proof, live: an unknown device on a pty, a real model, the generic protocol, a skill written and verified against its baseline, then a second run with no model call.

  python scripts/prove_live.py --profile muse-glimmer [--no-protocols] [--device zxq-sensor]

The device is fixtures/replay/<device>.json served on a pseudo-terminal (envrec.fakedev). The model is the profile's model for EVERY role: it drives the loop, it is the
no-skill baseline, and, in a separate call with a clean context, the verifier. Approvals are given by this script and logged as lab approvals: the device is a fake one
this script created. Nothing real is touched. The result is written to runs/prove-<profile>-<time>/result.json and printed."""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from envrec.cli import DEFAULT_MODELS  # noqa: E402
from envrec.contracts import ModelProfile, Target, TargetKind  # noqa: E402
from envrec.discovery import LocalCollector, Salts  # noqa: E402
from envrec.fakedev import PtyDevice  # noqa: E402
from envrec.loop import Loop, RunConfig  # noqa: E402
from envrec.model import load_profile, make_adapter, with_view_budget  # noqa: E402
from envrec.protocols import ProtocolSet  # noqa: E402
from envrec.scoring import load_ground_truth, score_target  # noqa: E402
from envrec.skills.lib import Library  # noqa: E402
from envrec.skills.verify import ModelBaseline, ModelVerifier  # noqa: E402
from envrec.skills.workbench import Shelf, Workbench  # noqa: E402
from envrec.store import Store  # noqa: E402
from envrec.tools.bridge import RegistryAdapter  # noqa: E402


def build(model, profile, st, lib, dev, docs, *, protocols: bool, shelf, lab_approvals: list):
    def approve(text: str) -> bool:
        lab_approvals.append(text)
        return True
    reg = RegistryAdapter(platform=__import__("envrec.discovery.types", fromlist=["this_platform"]).this_platform(), collector=LocalCollector(), salts=Salts(), params={"port": dev.port},
                          approve=approve, library=lib, shelf=shelf, ask_skill_approval=approve)
    wb = Workbench(lib, st.root / "stage", docs_dir=docs, verifier=ModelVerifier(model), baseline=ModelBaseline(model), shelf=shelf) if docs else None
    cfg = RunConfig(max_steps=40, max_seconds=900, protocols=ProtocolSet.from_library(lib) if protocols else None, library=lib)
    return Loop(st, [reg], model, config=cfg, workbench=wb), wb


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True)
    ap.add_argument("--models", default=str(DEFAULT_MODELS))
    ap.add_argument("--device", default="zxq-sensor")
    ap.add_argument("--no-protocols", action="store_true")
    ap.add_argument("--max-tokens", type=int, default=1800, help="the draft of a skill does not fit in the default reply length")
    ap.add_argument("--runs-dir", default="runs")
    ap.add_argument("--max-steps", type=int, default=40)
    a = ap.parse_args()
    profile, options = load_profile(a.models, a.profile)
    options = {**options, "max_tokens": max(int(options.get("max_tokens", 400)), a.max_tokens)}
    model = make_adapter(profile, options)
    out = Path(a.runs_dir) / f"prove-{a.profile}{'-noproto' if a.no_protocols else ''}-{int(time.time())}"
    skills = out / "skills"
    out.mkdir(parents=True)
    shutil.copytree(ROOT / "skills", skills, ignore=shutil.ignore_patterns("generated", "state", "__pycache__"))
    docs = out / "docs"
    docs.mkdir()
    shutil.copy(ROOT / "fixtures" / "docs" / f"{a.device}.md", docs / f"{a.device}.md")
    lib = Library(root=skills).load()
    result: dict = {"profile": profile.name, "model_id": profile.model_id, "protocols": not a.no_protocols, "device": a.device}
    with PtyDevice.from_replay(ROOT / "fixtures" / "replay" / f"{a.device}.json", period_s=0.25) as dev:
        for n in (1, 2):
            with Store(out / f"store{n}") as st:
                target = st.put_target(Target(kind=TargetKind.serial, ring=1, handle=dev.port))
                shelf, lab = Shelf(), []
                loop, wb = build(model, profile, st, lib, dev, docs if n == 1 else None, protocols=not a.no_protocols, shelf=shelf, lab_approvals=lab)
                s = asyncio.run(loop.run(target))
                truth = load_ground_truth(ROOT / "ground_truth" / "proof-zxq-sensor.yaml")
                rep = score_target(st, target.id, truth)
                facts = {f.key: (f.value, f.status.value) for f in st.current_facts(target.id) if f.key.startswith(("sensor.", "skill.", "serial.protocol"))}
                result[f"run{n}"] = {
                    "steps": s.steps, "stop": s.stop_reason, "model_calls": s.model_calls, "invalid_replies": s.invalid_replies, "fallbacks": s.fallbacks, "repaired": s.repaired,
                    "protocol_refusals": s.protocol_refusals, "protocol_history": [h["state"] for h in next(iter(s.protocol_history.values()), [])], "tools_run": s.tools_run,
                    "skill_saved": (wb.saved if wb else None), "score": {"correct": rep.count("correct"), "wrong": rep.count("wrong"), "scored": len(rep.rows)}, "facts": facts,
                    "lab_approvals": len(lab), "seconds": round(s.elapsed_s, 1), "tokens": s.prompt_tokens + s.completion_tokens}
                if n == 1 and not (wb and wb.saved):
                    result["run2"] = "not run: no skill was saved in run 1"
                    break
    (out / "result.json").write_text(json.dumps(result, indent=1, default=str))
    print(json.dumps(result, indent=1, default=str))
    print("result:", out / "result.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
