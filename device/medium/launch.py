"""MAS portable device launcher. Runs in the medium's own interpreter, started by launch.sh (python3.13 -I -B). EMULATED in v0 (D-034).

Order: refuse a medium that does not match its manifest (before the log is opened), profile this host and pick the compute mode, then
start the gate with the medium's own pairing record. The gate shows the owner the terms first (D-021). Nothing is written to the medium.

Limit: the check guards against a corrupted or altered medium copy; it runs with the medium's own interpreter, so it is not a defence
against someone who can rewrite the medium (the device exposes the medium read-only, D-003).
"""

import os
import sys

RUNTIME = os.path.dirname(os.path.abspath(__file__))
MEDIUM = os.path.dirname(RUNTIME)
sys.path[:0] = [RUNTIME, os.path.join(RUNTIME, "site")]


def main(argv: list[str]) -> int:
    from mas_device import medium, profiler

    if any(a == "--pairing" or a.startswith("--pairing=") for a in argv):
        sys.stderr.write("ERROR the pairing record is the medium's own; --pairing is not accepted\n")
        return 2
    problems = medium.verify(MEDIUM)
    if problems:
        sys.stderr.write("ERROR the medium does not match its manifest: " + "; ".join(problems[:5]) + "\n")
        return 3
    if argv and argv[0] == "inspect":  # D-037: read-only inspection, one INSPECT_SESSION press, report on the device
        from mas_device import inspect

        return inspect.main(list(argv[1:]), pairing=os.path.join(MEDIUM, "pairing.json"))
    if argv and argv[0] == "fleet-join":  # D-036: the host's owner asks this computer to join a fleet
        from mas_fleet import node

        return node.main(list(argv[1:]), runtime=RUNTIME)
    if argv and argv[0] == "fleet-coordinator":  # D-036: on the device owner's own machine
        from mas_fleet import coordinator

        return coordinator.main(list(argv[1:]), pairing=os.path.join(MEDIUM, "pairing.json"))
    probe = profiler.probe()
    mode, reasons = profiler.choose(probe)
    from mas_device import gated

    return gated.main(list(argv) + ["--pairing", os.path.join(MEDIUM, "pairing.json")],
                      profile={"mode": mode, "reasons": reasons, "probe": probe})


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
