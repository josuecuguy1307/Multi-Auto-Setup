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
from envrec.mission import MissionCard  # noqa: E402
from envrec.model import load_profile, make_adapter, with_view_budget  # noqa: E402
from envrec.protocols import ProtocolSet  # noqa: E402
from envrec.scoring import load_ground_truth, score_target  # noqa: E402
from envrec.skills.lib import Library  # noqa: E402
from envrec.skills.verify import ModelBaseline, ModelVerifier  # noqa: E402
from envrec.skills.workbench import Shelf, Workbench  # noqa: E402
from envrec.store import Store  # noqa: E402
from envrec.tools.bridge import RegistryAdapter  # noqa: E402


def build(model, profile, st, lib, dev, docs, *, protocols: bool, shelf, lab_approvals: list, human=None, with_workbench=True, mission=None):
    def approve(text: str) -> bool:
        lab_approvals.append(text)
        return True
    reg = RegistryAdapter(platform=__import__("envrec.discovery.types", fromlist=["this_platform"]).this_platform(), collector=LocalCollector(), salts=Salts(), params={},
                          approve=approve, library=lib, shelf=shelf, ask_skill_approval=approve)
    wb = Workbench(lib, st.root / "stage", docs_dir=docs, verifier=ModelVerifier(model), baseline=ModelBaseline(model), shelf=shelf) if with_workbench else None
    cfg = RunConfig(max_steps=40, max_seconds=900, protocols=ProtocolSet.from_library(lib) if protocols else None, library=lib)
    return Loop(st, [reg], model, config=cfg, workbench=wb, ask_human=human, mission=mission), wb


def value_score(st, target_id, path: Path) -> dict | None:
    """Independent check by construction: of the values the device's frame holds, how many appear as a fact value, and under a key that names the right thing."""
    if not path.is_file():
        return None
    want = json.loads(path.read_text())["values"]
    facts = [(f.key, f.value) for f in st.current_facts(target_id) if f.key.startswith("sensor.") and f.status.value != "unknown"]
    hit = {v: [k for k, x in facts if str(x) == v] for v in want}
    return {"values_found": sum(1 for v in want if hit[v]), "named_right": sum(1 for v, word in want.items() if any(word in k for k in hit[v])), "expected": len(want), "found_under": hit}


def no_harness_arm(model, device: str) -> dict:
    """The arm without any harness: the same model is handed the raw frame the device prints and the manual in ONE call and asked for the facts as JSON. No tools, no protocol,
    no skill, no privacy gate, no evidence rule beyond what it writes. Scored with the same independent value check."""
    spec = json.loads((ROOT / "fixtures" / "replay" / f"{device}.json").read_text())
    frame = (spec.get("stream") or [next(iter(spec.get("responses", {}).values()), "")])[0]
    manual = (ROOT / "fixtures" / "docs" / f"{device}.md").read_text(encoding="utf-8")
    system = 'Read the device frame using the manual. Reply with one JSON object only: {"facts": [{"key": "sensor.<name>", "value": <number or string>}]}.'
    user = f"MANUAL:\n{manual}\nFRAME:\n{frame}"
    try:
        done = asyncio.run(model.complete(system, user))
        data = json.loads(done.text[done.text.index("{"): done.text.rindex("}") + 1])
        facts = [(f["key"], f["value"]) for f in data.get("facts", []) if isinstance(f, dict) and "key" in f]
        err = None
    except Exception as exc:  # noqa: BLE001
        facts, err = [], f"{type(exc).__name__}: {str(exc)[:100]}"
    want = json.loads((ROOT / "fixtures" / "replay" / f"{device}.expect.json").read_text())["values"] if (ROOT / "fixtures" / "replay" / f"{device}.expect.json").is_file() else {}
    hit = {v: [k for k, x in facts if str(x) == v] for v in want}
    return {"facts": facts, "error": err, "values_found": sum(1 for v in want if hit[v]), "named_right": sum(1 for v, w in want.items() if any(w in k for k in hit[v])), "expected": len(want),
            "model_calls": 1, "note": "no tools, no protocol, no skill; it was handed the raw frame and the manual"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default=None)
    ap.add_argument("--endpoint", default=None, help="an OpenAI-compatible endpoint (for example the tunnel to the provisioned small model); with --model-id and --label")
    ap.add_argument("--model-id", default="model")
    ap.add_argument("--label", default="endpoint")
    ap.add_argument("--context", type=int, default=2048)
    ap.add_argument("--models", default=str(DEFAULT_MODELS))
    ap.add_argument("--device", default="zxq-sensor")
    ap.add_argument("--no-protocols", action="store_true")
    ap.add_argument("--walk-for-model", action="store_true", help="EXPERIMENTAL dial: code advances the protocol for the model; every forced step is SCRIPT and the result says so")
    ap.add_argument("--human", choices=["simulated", "none", "both"], default="both",
                    help="simulated: a scripted stand-in for the owner answers ask_human with the device's manual text ONLY; none: nobody answers and the run must end as blocked_needs_human")
    ap.add_argument("--max-tokens", type=int, default=1800, help="the draft of a skill does not fit in the default reply length")
    ap.add_argument("--runs-dir", default="runs")
    ap.add_argument("--max-steps", type=int, default=40)
    a = ap.parse_args()
    if a.endpoint:
        profile = ModelProfile(name=a.label, kind="openai_compat", model_id=a.model_id, endpoint=a.endpoint, context_tokens=a.context, view_budget_facts=8, menu_max=6, walk_for_model=a.walk_for_model)
        options = {"timeout_s": 300, "temperature": 0.2, "max_tokens": a.max_tokens}
        a.profile = a.label
    else:
        profile, options = load_profile(a.models, a.profile)
        if a.walk_for_model:
            profile = profile.model_copy(update={"walk_for_model": True})
    options = {**options, "max_tokens": max(int(options.get("max_tokens", 400)), a.max_tokens)}
    model = make_adapter(profile, options)
    out = Path(a.runs_dir) / f"prove-{a.profile}{'-noproto' if a.no_protocols else ''}-{int(time.time())}"
    skills = out / "skills"
    out.mkdir(parents=True)
    shutil.copytree(ROOT / "skills", skills, ignore=shutil.ignore_patterns("generated", "state", "__pycache__"))
    lib = Library(root=skills).load()
    result: dict = {"profile": profile.name, "model_id": profile.model_id, "protocols": not a.no_protocols, "device": a.device,
                    "simulated_human_note": "In the runs labeled simulated_human, a SCRIPTED stand-in for the owner answers ask_human with the device's manual text only (the text of "
                                            f"fixtures/docs/{a.device}.md). There is no documentation folder in either run: the manual reaches the model only through that answer. It never provides the finished skill or the parsed facts. It also auto-approves sandbox and lab approvals. "
                                            "No real person was involved."}
    manual = (ROOT / "fixtures" / "docs" / f"{a.device}.md").read_text(encoding="utf-8")
    result["no_harness"] = no_harness_arm(model, a.device)
    modes = ["simulated", "none"] if a.human == "both" else [a.human]
    for mode in modes:
        skills = out / f"skills-{mode}"
        shutil.copytree(ROOT / "skills", skills, ignore=shutil.ignore_patterns("generated", "state", "__pycache__"))
        lib = Library(root=skills).load()
        answers: list[str] = []

        def human(question: str, _mode=mode, _answers=answers) -> str | None:
            if _mode != "simulated":
                return None
            _answers.append(question)
            return " ".join(manual.split())[:600]  # what an owner has in hand: the manual's text, nothing derived from it
        with PtyDevice.from_replay(ROOT / "fixtures" / "replay" / f"{a.device}.json", period_s=0.25) as dev:
            for n in (1, 2):
                with Store(out / f"store-{mode}-{n}") as st:
                    serial = st.put_target(Target(kind=TargetKind.serial, ring=1, handle=dev.port))
                    if n == 1:
                        # discovery: the model is given a goal and candidates (the device among distractors of other kinds) and picks one with extend_to; no script builds its target
                        root = st.put_target(Target(kind=TargetKind.scope, ring=0, handle="candidates"))
                        for t_ in (serial, st.put_target(Target(kind=TargetKind.ble, ring=1, handle="ble:nearby-1")), st.put_target(Target(kind=TargetKind.usb, ring=1, handle="usb:attached-1"))):
                            st.put_target(t_.model_copy(update={"parent": root.id}))
                        start = root
                    else:
                        start = serial  # run 2 measures reuse (zero model calls), not discovery: the device is given, and the result file says so
                    shelf, lab = Shelf(), []
                    mission = MissionCard(goal="Identify the device that talks over a serial port: what it is and what its readings mean.", scope="the candidate targets listed")
                    loop, wb = build(model, profile, st, lib, dev, None, with_workbench=(n == 1), protocols=not a.no_protocols, shelf=shelf, lab_approvals=lab, human=human if n == 1 else None, mission=mission)
                    s = asyncio.run(loop.run(start))
                    target = serial  # scored against the real device whatever the model chose
                    chosen = [st.get_target(e.payload["to"]).kind.value for e in st.events(type="ring_extended")]
                    gt = ROOT / "ground_truth" / f"proof-{a.device}.yaml"
                    rep = score_target(st, target.id, load_ground_truth(gt)) if gt.is_file() else None
                    vals = value_score(st, target.id, ROOT / "fixtures" / "replay" / f"{a.device}.expect.json")
                    facts = {f.key: (f.value, f.status.value) for f in st.current_facts(target.id) if f.key.startswith(("sensor.", "skill.", "serial.protocol"))}
                    skill_results = [e.payload for e in st.events(type="skill_result")]
                    result.setdefault(mode, {})[f"run{n}"] = {
                        "dial": "ON (walk_for_model): forced steps are SCRIPT" if s.walk_for_model else "off", "target_chosen_by": ("MODEL via extend_to" if n == 1 else "given (run 2 measures reuse)"),
                        "candidates": ["serial", "ble", "usb"] if n == 1 else None, "chosen_kind": (chosen[0] if chosen else None),
                        "simulated_human": mode == "simulated", "human_questions_answered": len(answers) if n == 1 else 0,
                        "steps": s.steps, "stop": s.stop_reason, "model_calls": s.model_calls, "invalid_replies": s.invalid_replies, "fallbacks": s.fallbacks, "repaired": s.repaired,
                        "protocol_refusals": s.protocol_refusals, "repeat_refusals": s.repeat_refusals, "protocol_history": [h["state"] for h in next(iter(s.protocol_history.values()), [])],
                        "tools_run": s.tools_run, "skill_attempts": [r["result"][:300] for r in skill_results], "skill_saved": (wb.saved if wb else None),
                        "score": ({"correct": rep.count("correct"), "wrong": rep.count("wrong"), "scored": len(rep.rows)} if rep else None), "value_score": vals, "facts": facts,
                        "lab_approvals": len(lab), "seconds": round(s.elapsed_s, 1), "tokens": s.prompt_tokens + s.completion_tokens}
                    if n == 1 and not (wb and wb.saved):
                        result[mode]["run2"] = "not run: no skill was saved in run 1"
                        break
    (out / "result.json").write_text(json.dumps(result, indent=1, default=str))
    print(json.dumps(result, indent=1, default=str))
    print("result:", out / "result.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
