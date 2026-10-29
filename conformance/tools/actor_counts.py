#!/usr/bin/env python3
"""Count, per recorded run, who made each decision, from the event log alone (nothing is re-run).

  python conformance/tools/actor_counts.py [runs-dir]

Every event carries a `tag` (MODEL, GUARD, SCRIPT, OWNER); older events get it derived from their type. Decisions counted:
  MODEL   an action the model chose (action_chosen, no fallback) and a protocol advance it asked for
  SCRIPT  a fallback action, a protocol advance or ring move made by code, unknowns written by code, first-rung skill runs, discovery adapters, repairs
  GUARD   a refusal (protocol_refused, repeat_refused, action_rejected, a policy denial)
  OWNER   an approval or a human answer
The model's share is reported two ways: over every decision, and over TURNS WHOSE MENU HAD AT LEAST THREE ENTRIES (a choice from one or two entries is mostly
the protocol's design and is reported separately). Each run is labeled REAL (a target on this machine or a device that exists), FIXTURE (a replay transcript or a
pseudo-terminal serving a recorded transcript) or NO-MODEL (discovery runs and skill-reuse runs, which have no model call). Labels are never summed together."""
from __future__ import annotations

import collections
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from envrec.contracts.tags import derive_tag  # noqa: E402

SCRIPT_TYPES = {"first_rung_identify", "skill_reuse", "discovery_adapter", "model_repaired"}


def classify_run(name: str, handles: list[str], model_calls: int) -> str:
    if model_calls == 0:
        return "NO-MODEL"
    if any(h.startswith(("replay:", "/dev/ttys", "/dev/pts")) for h in handles) or name.startswith(("prove-", "ladder-")):
        return "FIXTURE"
    return "REAL"


def count_db(db: Path) -> dict:
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    ev = [(t, json.loads(j).get("payload", {}), json.loads(j).get("tag")) for t, j in c.execute("select type, json from events order by seq")]
    handles = [json.loads(j).get("handle", "") for (j,) in c.execute("select json from targets")]
    dial_on = any(t == "run_start" and p.get("walk_for_model") for t, p, _ in ev)
    n = collections.Counter()
    menu_len = None
    for t, p, tag in ev:
        tag = tag or derive_tag("loop" if t not in ("approval", "human_question", "human_answer", "human_unavailable") else "human", t, p)
        if t == "view_built":
            menu_len = len(p.get("menu", []))
        if t == "model_call":
            n["model_calls"] += 1
        elif t == "action_chosen":
            if p.get("fallback"):
                n["SCRIPT_fallback"] += 1
            else:
                n["MODEL_action"] += 1
            if menu_len is not None:
                key = "menu3plus" if menu_len >= 3 else "menu1or2"
                n[f"TURNS_{key}"] += 1
                if not p.get("fallback"):
                    n[f"TURNS_{key}_model"] += 1
        elif t == "protocol_state":
            if tag == "GUARD":
                n["GUARD_evidence_advance"] += 1  # the step's evidence was in; code recorded the move (not counted as a decision)
            else:
                n["MODEL_advance" if tag == "MODEL" else "SCRIPT_advance"] += 1
        elif t == "ring_extended":
            n["MODEL_ring_move" if tag == "MODEL" else "SCRIPT_ring_move"] += 1
        elif t == "unknown_recorded" and tag == "SCRIPT" and not p.get("fallback"):
            n["SCRIPT_unknown"] += 1
        elif t in SCRIPT_TYPES:
            n["SCRIPT_" + t] += 1
        elif t in ("protocol_refused", "repeat_refused", "action_rejected") or (t == "policy_decision" and p.get("decision") == "deny"):
            n["GUARD_refusal"] += 1
        elif t in ("approval", "human_answer", "human_question"):
            n["OWNER"] += 1
    return {"handles": handles, "dial_on": dial_on, **n}


def shares(n) -> tuple[int, int, str, str]:
    m = sum(v for k, v in n.items() if k.startswith("MODEL_"))
    s = sum(v for k, v in n.items() if k.startswith("SCRIPT_"))
    overall = "n/a" if m + s == 0 else f"{100 * m / (m + s):.0f}%"
    t3, t3m = n.get("TURNS_menu3plus", 0), n.get("TURNS_menu3plus_model", 0)
    s3 = "no evidence (0 turns)" if t3 == 0 else f"{100 * t3m / t3:.0f}% ({t3m}/{t3})"
    return m, s, overall, s3


def main(root: str = "runs") -> int:
    rows = []
    for db in sorted(Path(root).glob("*/envrec.db")) + sorted(Path(root).glob("*/*/envrec.db")):
        try:
            r = count_db(db)
        except sqlite3.Error:
            continue
        run_name = db.parent.name if db.parent.parent.name == Path(root).name else db.parent.parent.name
        label = classify_run(run_name, r.pop("handles"), r.get("model_calls", 0))
        rows.append((label, str(db.parent.relative_to(root)), r))
    agg: dict[str, collections.Counter] = {}
    for label, name, r in rows:
        dial = r.pop("dial_on")
        agg.setdefault(label, collections.Counter()).update(r)
        m, s, overall, s3 = shares(r)
        print(f"{label:9} {name:48} calls={r.get('model_calls', 0):3} MODEL={m:3} SCRIPT={s:3} GUARD={r.get('GUARD_refusal', 0):3} OWNER={r.get('OWNER', 0):2} "
              f"share-all={overall:>4} share-menu>=3={s3} single-or-pair-menu-turns={r.get('TURNS_menu1or2', 0)}{' DIAL ON' if dial else ''}")
    print("\nTOTALS BY LABEL (never summed across labels):")
    for label, n in sorted(agg.items()):
        m, s, overall, s3 = shares(n)
        print(f"  {label:9} runs={sum(1 for r in rows if r[0] == label):3} model_calls={n.get('model_calls', 0):4} MODEL={m:4} SCRIPT={s:4} share-all={overall}  "
              f"share-over-menu>=3-turns={s3}  turns-with-1-or-2-entry-menu={n.get('TURNS_menu1or2', 0)}")
        print(f"            detail={dict(sorted((k, v) for k, v in n.items() if k != 'model_calls'))}")
    return 0


if __name__ == "__main__":
    sys.exit(main(*(sys.argv[1:2] or ["runs"])))
