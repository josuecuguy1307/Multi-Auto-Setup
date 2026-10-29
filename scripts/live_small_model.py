#!/usr/bin/env python3
"""R7: measure the harness with a provisioned small model reached over a tunnel (or any OpenAI-compatible endpoint).

  python scripts/live_small_model.py --endpoint http://127.0.0.1:18089/v1 --repeats 5 --out live_results/r7 --label tiny

For each repeat it runs Ring 0 on this machine and the three ladder fixtures, scores them against the ground truth, and
records per run: valid-Action rate, steps, highest rung, false-tested rate, throughput, and every failure with its TYPE.
Repeats use different server seeds, so the spread is real; one extra repeat can be run at temperature 0
with --temperature 0. Also writes run.json: model names and versions, the git
commit, and what the machine was doing while it ran. Nothing here starts or stops a server.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import psutil
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from envrec.adapters import BuiltinRing0Adapter  # noqa: E402
from envrec.contracts import FactStatus, Target, TargetKind  # noqa: E402
from envrec.fingerprint import UsbIds  # noqa: E402
from envrec.ladder import LadderAdapter, load_devices  # noqa: E402
from envrec.loop import Loop, RunConfig  # noqa: E402
from envrec.model import load_profile, make_adapter  # noqa: E402
from envrec.scoring import highest_rung, load_ground_truth, score_target  # noqa: E402
from envrec.store import Store  # noqa: E402

FIXTURES = ("ch340-gps", "mystery-box", "liar-device")


def model_info(endpoint: str) -> dict:
    base = endpoint.rsplit("/v1", 1)[0]
    models = httpx.get(f"{base}/v1/models", timeout=10).json()
    try:
        props = httpx.get(f"{base}/props", timeout=10).json()
    except Exception:
        props = {}
    ids = [m.get("id") for m in models.get("data", [])]
    return {"models": ids, "props_model_path": props.get("model_path"), "build": props.get("build_info")}


def profile_file(tmp: Path, endpoint: str, model_id: str, temperature: float, seed: int | None, label: str, extra: dict | None = None) -> Path:
    base = yaml.safe_load((ROOT / "config" / "models.yaml").read_text())["profiles"]["muse-glimmer"]  # the small profile's dials
    opts = dict(base["options"])
    opts["temperature"] = temperature
    opts["extra_body"] = {**(extra or {}), **({"seed": seed} if seed is not None else {})}
    p = {**base, "name": label, "model_id": model_id, "endpoint": endpoint, "options": opts}
    f = tmp / f"models-{label}-{seed}.yaml"
    f.write_text(yaml.safe_dump({"profiles": {label: p}}))
    return f


def false_tested_rate(report) -> tuple[int, int]:
    """(wrong, total) among scored facts the harness itself called tested or verified."""
    claimed = [r for r in report.rows if r.status in (FactStatus.tested.value, FactStatus.verified.value) and r.outcome in ("correct", "wrong", "contradicted")]
    return sum(1 for r in claimed if r.outcome != "correct"), len(claimed)


def failure_types(s) -> dict[str, int]:
    out = {"unparseable_or_invalid_reply": s.invalid_replies, "fell_back_after_two_invalid": s.fallbacks,
           "valid_json_but_not_on_the_menu": s.rejected_actions, "denied_by_policy": s.policy_denied}
    if s.stop_reason not in ("done", "stop", "finished", "enough"):
        out[f"stopped:{s.stop_reason}"] = 1
    return {k: v for k, v in out.items() if v}


def summarise(s, store, target, report, extra: dict) -> dict:
    wrong, total = false_tested_rate(report)
    calls = max(s.model_calls, 1)
    return {"steps": s.steps, "stop": s.stop_reason, "model_calls": s.model_calls,
            "valid_action_rate": round(1 - (s.invalid_replies / calls), 3), "repaired": s.repaired,
            "highest_rung": highest_rung(store, target.id), "false_tested_wrong": wrong, "false_tested_total": total,
            "false_tested_rate": None if total == 0 else round(wrong / total, 3),
            "false_verified_rate": report.false_verified_rate, "correct": report.count("correct"), "scored": len(report.rows),
            "completion_tokens": s.completion_tokens, "prompt_tokens": s.prompt_tokens, "model_seconds": round(s.model_latency_s, 2),
            "tokens_per_model_second": round(s.completion_tokens / s.model_latency_s, 2) if s.model_latency_s else None,
            "elapsed_s": round(s.elapsed_s, 1), "failures": failure_types(s), **extra}


def run_ring0(models: Path, label: str, out: Path, max_steps: int, max_seconds: float, truth: Path) -> dict:
    profile, options = load_profile(models, label)
    model = make_adapter(profile, options)
    with Store(out / "ring0") as store:
        host = store.put_target(Target(kind=TargetKind.host, ring=0, handle="this-machine"))
        loop = Loop(store, [BuiltinRing0Adapter()], model, config=RunConfig(max_steps=max_steps, max_seconds=max_seconds))
        s = asyncio.run(loop.run(host))
        return summarise(s, store, host, score_target(store, host.id, load_ground_truth(truth)), {"kind": "ring0"})


def run_fixture(models: Path, label: str, out: Path, name: str, max_steps: int, max_seconds: float) -> dict:
    profile, options = load_profile(models, label)
    model = make_adapter(profile, options)
    registry = load_devices(ROOT / "fixtures" / "replay")
    device = registry.get(f"replay:{name}")
    with Store(out / name) as store:
        target = store.put_target(Target(kind=TargetKind.usb, ring=1, handle=device.handle))
        loop = Loop(store, [LadderAdapter(registry, UsbIds.from_file(ROOT / "fixtures" / "samples" / "usb_ids_sample.txt"))], model,
                    config=RunConfig(max_steps=max_steps, max_seconds=max_seconds))
        s = asyncio.run(loop.run(target))
        report = score_target(store, target.id, load_ground_truth(ROOT / "ground_truth" / f"replay-{name}.yaml"))
        proto = {f.key: f for f in store.current_facts(target.id)}.get("serial.protocol")
        return summarise(s, store, target, report, {"kind": f"ladder:{name}", "protocol": f"{proto.value if proto else None} ({proto.status.value if proto else '-'})"})


def git_commit() -> str:
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True).stdout.strip())
        return sha + ("+uncommitted" if dirty else "")
    except OSError:
        return "unknown"


def machine_state() -> dict:
    vm = psutil.virtual_memory()
    return {"load_avg_1_5_15": os.getloadavg(), "cpu_percent_sample": psutil.cpu_percent(interval=1.0), "ram_available_gib": round(vm.available / 2**30, 2),
            "ram_total_gib": round(vm.total / 2**30, 2), "platform": platform.platform(), "python": platform.python_version()}


def spread(values: list[float]) -> dict:
    v = [x for x in values if x is not None]
    if not v:
        return {"n": 0}
    return {"n": len(v), "mean": round(statistics.fmean(v), 3), "min": round(min(v), 3), "max": round(max(v), 3),
            "stdev": round(statistics.stdev(v), 3) if len(v) > 1 else 0.0}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", required=True, help="OpenAI-compatible base URL, e.g. http://127.0.0.1:18089/v1")
    ap.add_argument("--label", default="tiny")
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--temperature", type=float, default=0.2)
    ap.add_argument("--out", default="live_results/r7")
    ap.add_argument("--max-steps", type=int, default=20)
    ap.add_argument("--max-seconds", type=float, default=300.0)
    ap.add_argument("--truth", default=str(ROOT / "ground_truth" / "this-machine.yaml"))
    ap.add_argument("--extra-body", default="{}", help="JSON merged into every request, e.g. {\"reasoning_effort\": \"none\"}")
    ap.add_argument("--model-id", default=None, help="override the id read from /v1/models")
    ap.add_argument("--runtime-note", default="", help="runtime + tag + model entry id, copied into the run manifest")
    args = ap.parse_args(argv)

    out = Path(args.out) / f"{args.label}-{int(time.time())}"
    out.mkdir(parents=True, exist_ok=True)
    info = model_info(args.endpoint)
    model_id = args.model_id or (info["models"] or ["unknown"])[0]
    manifest = {"label": args.label, "endpoint": args.endpoint, "server_reports": info, "runtime_note": args.runtime_note,
                "git_commit": git_commit(), "temperature": args.temperature, "repeats": args.repeats, "started": time.time(),
                "machine_before": machine_state(), "ground_truth_confirmed": "PROVISIONAL: ground_truth/this-machine.yaml is not confirmed by a human"}
    runs: list[dict] = []
    with tempfile.TemporaryDirectory() as t:
        for rep in range(args.repeats):
            models = profile_file(Path(t), args.endpoint, model_id, args.temperature, rep + 1, args.label, json.loads(args.extra_body))
            rdir = out / f"rep{rep + 1}"
            for kind, fn in [("ring0", lambda: run_ring0(models, args.label, rdir, args.max_steps, args.max_seconds, Path(args.truth)))] + [
                    (f, (lambda f=f: run_fixture(models, args.label, rdir, f, args.max_steps, args.max_seconds))) for f in FIXTURES]:
                t0 = time.time()
                try:
                    r = fn()
                except Exception as exc:  # a run that crashes is a labelled failure, not a hole in the table
                    r = {"kind": kind if kind == "ring0" else f"ladder:{kind}", "crashed": f"{type(exc).__name__}: {str(exc)[:200]}",
                         "failures": {"harness_or_backend_error": 1}}
                r.update(rep=rep + 1, seed=rep + 1, wall_s=round(time.time() - t0, 1))
                runs.append(r)
                print(f"rep {rep + 1} {r['kind']:22} " + (r.get("crashed") or f"steps {r['steps']} valid {r['valid_action_rate']} rung {r['highest_rung']} "
                      f"score {r['correct']}/{r['scored']} false-tested {r['false_tested_wrong']}/{r['false_tested_total']} failures {r['failures'] or '-'}"))
    manifest.update(finished=time.time(), machine_after=machine_state())
    agg = {}
    for kind in sorted({r["kind"] for r in runs}):
        rs = [r for r in runs if r["kind"] == kind and "crashed" not in r]
        agg[kind] = {"runs": len([r for r in runs if r["kind"] == kind]), "crashed": len([r for r in runs if r["kind"] == kind and "crashed" in r]),
                     "valid_action_rate": spread([r["valid_action_rate"] for r in rs]), "steps": spread([r["steps"] for r in rs]),
                     "highest_rung": spread([r["highest_rung"] for r in rs]), "score_correct": spread([r["correct"] for r in rs]),
                     "false_tested_rate": spread([r["false_tested_rate"] for r in rs]), "tokens_per_model_second": spread([r["tokens_per_model_second"] for r in rs])}
    (out / "run.json").write_text(json.dumps({"manifest": manifest, "runs": runs, "aggregate": agg}, indent=2, default=str))
    print(f"\nwrote {out / 'run.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
