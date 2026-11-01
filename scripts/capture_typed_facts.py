"""Run the six read-only analysis probes of the per-OS layer on THIS machine and print their typed facts as a fixture (the shape of fixtures/probes/windows-real).

Used to capture a stored analysis for the draft tests: once on a Mac, once inside a Linux container. Nothing is written on the machine; the output is the probes' own typed result
(no host name, user name or address is among these facts). Run with `python -I scripts/capture_typed_facts.py <label>`."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parent.parent / "src")]

from envrec_os import probes  # noqa: E402

PROBES = (("node_os_details", probes.os_details), ("node_gpu", probes.gpus), ("node_memory_fit", probes.memory_for_models), ("node_runtimes", probes.model_runtimes),
          ("node_ai_clis", probes.ai_clis))


def main(label: str) -> None:
    facts: dict = {}
    for tool, fn in PROBES:
        out = fn()
        for key, value in out["facts"].items():
            facts[key] = {"value": value, "status": "tested", "probe": tool, "limits": out["limits"]}
    print(json.dumps({"label": label, "facts": dict(sorted(facts.items()))}, indent=1, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "capture")
