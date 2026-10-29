#!/usr/bin/env python3
"""SIMULATED-LOCAL verification run: the coordinator and a node as two real processes on THIS machine, paired through the `mas` commands, heartbeats flowing, then the evidence:
what each process listens on, `mas nodes`, and the actor tags of every event in both logs. Real processes and TLS sockets, one machine, loopback: not a multi-machine run."""
from __future__ import annotations

import collections
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "conformance")]

from envrec.store import Store  # noqa: E402
from mas_procs import run_local_network  # noqa: E402


def tags(db_dir: Path) -> dict:
    with Store(db_dir) as st:
        ev = st.events()
        return {"events": len(ev), "tags": dict(collections.Counter(e.effective_tag for e in ev)), "types": dict(collections.Counter(e.type for e in ev))}


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="mas-local-"))
    res = run_local_network(tmp)
    res["coordinator_log"] = tags(tmp / "coordinator-state" / "coordinator" / "log")
    res["node_log"] = tags(tmp / "node-state" / "node" / "log")
    print(json.dumps(res, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
