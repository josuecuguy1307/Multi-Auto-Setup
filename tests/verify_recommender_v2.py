"""Measurement of recommender-v2 (not collected by pytest). Numbers by code, with denominators, for four conditions: the local model with the harness, the same model with one plain prompt, and the two
cloud conditions (UNTESTED unless run through a coordinator whose node holds the cloud access, which this script never touches).

  valid-lead rate  = leads that pass license, active, not archived, enough stars and the domain tags in the README  /  leads the model tried to record, over the domains of fixtures/recommender/measure_domains.json
  task-type accuracy = picks equal to the gold type  /  tasks of fixtures/recommender/gold_task_types.json  (the harness may instead ask the owner: counted apart, never as right or wrong)

REAL: the fetches (api.github.com, unauthenticated, throttled) and the local model. SIMULATED-LOCAL: no coordinator, no node, no owner (nothing is created or dispatched).
Run: PYTHONPATH=src .venv/bin/python tests/verify_recommender_v2.py --profile NAME [--models FILE] [--only leads|types]    Writes live_results/recommender-v2/measurement.json"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from envrec.model import load_profile, make_adapter  # noqa: E402
from envrec_coordinator import measure as M  # noqa: E402
from envrec_coordinator.research import real_fetch  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIX = Path(__file__).parent / "fixtures" / "recommender"
OUT = ROOT / "live_results" / "recommender-v2"
GAP_S = 7.0  # the search API allows about ten requests a minute without a token
_last = [0.0]


def throttled(url: str, max_bytes=None):
    wait = GAP_S - (time.monotonic() - _last[0])
    if "api.github.com" in url and wait > 0:
        time.sleep(wait)
    _last[0] = time.monotonic()
    return real_fetch(url, max_bytes)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True)
    ap.add_argument("--models", default=str(Path.home() / "Desktop" / "env-recognition" / "config" / "models.yaml"))
    ap.add_argument("--only", choices=["leads", "types"])
    ap.add_argument("--out", default=str(OUT / "measurement.json"))
    a = ap.parse_args()
    prof, opts = load_profile(Path(a.models), a.profile)
    model = make_adapter(prof, opts)
    doms = json.loads((FIX / "measure_domains.json").read_text())["domains"]
    gold = json.loads((FIX / "gold_task_types.json").read_text())["tasks"]
    res = await M.run_all(model, doms, gold, only=a.only, fetch=throttled, log=lambda k, v: print(k, v, flush=True))
    out = {"measured_at": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()) + "Z", "local_model": prof.model_id,
           "labels": {"fetches": "REAL (api.github.com, unauthenticated)", "local model": "REAL", "everything else": "SIMULATED-LOCAL (no coordinator, no node, no owner)",
                      "cloud conditions": "UNTESTED here: run by the conductor through the coordinator (mas_measure_recommender) with the Lenovo online"},
           **{k.replace("types_", "types_local_").replace("leads_", "leads_local_"): v for k, v in res.items()}}
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(out, indent=1), encoding="utf-8")
    print("written", a.out)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
