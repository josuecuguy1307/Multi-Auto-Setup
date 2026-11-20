"""Host profiler: what this host can give the model, and which compute mode the session uses (D-003, D-034).

v0 rule: compute is the host's own CPU when the host offers at least MIN_HOST_MEM of memory and MIN_HOST_CPUS CPUs *to this process*
(cgroup v2 and v1 limits count, not only the machine's totals); otherwise the session needs device compute, which in v0 exists only as a
VM-emulated Pi (D-003), so the gate runs without a local model. Anything unreadable or malformed counts as unknown, and unknown memory
means device mode. GPUs are reported, not used, in v0.

Reads only files (procfs, sysfs, /dev entries): no command is run and nothing is written. `root` lets tests feed fixed machines.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

MIN_HOST_MEM = 4 << 30
MIN_HOST_CPUS = 2
_NO_LIMIT_V1 = 1 << 62  # cgroup v1 reports "no limit" as a huge page-aligned number


def _read(root: Path, rel: str) -> str | None:
    try:
        return (root / rel).read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None


def _mem(root: Path) -> int | None:
    text = _read(root, "proc/meminfo")
    m = re.search(r"^MemTotal:\s+(\d+) kB$", text or "", re.MULTILINE)
    mem = int(m.group(1)) * 1024 if m else None
    for rel, unlimited in (("sys/fs/cgroup/memory.max", lambda v: v == "max"),
                           ("sys/fs/cgroup/memory/memory.limit_in_bytes", lambda v: v.isdigit() and int(v) >= _NO_LIMIT_V1)):
        v = _read(root, rel)
        if v is None or unlimited(v):
            continue
        if not re.fullmatch(r"\d+", v):
            return None  # a limit we cannot read: unknown, not "unlimited"
        if mem is not None:
            mem = min(mem, int(v))
    return mem


def _cpus(root: Path, os_cpus: int) -> int:
    cpus = os_cpus
    quota = period = None
    v2 = _read(root, "sys/fs/cgroup/cpu.max")
    m = re.fullmatch(r"(\d+) (\d+)", v2 or "")
    if m:
        quota, period = int(m.group(1)), int(m.group(2))
    else:
        q, p = _read(root, "sys/fs/cgroup/cpu/cpu.cfs_quota_us"), _read(root, "sys/fs/cgroup/cpu/cpu.cfs_period_us")
        if q and p and q.isdigit() and p.isdigit():
            quota, period = int(q), int(p)
    if quota and period:
        cpus = min(cpus, max(1, -(-quota // period)))
    return cpus


def _gpu(root: Path) -> list[str]:
    found = []
    if (root / "dev" / "nvidia0").exists():
        found.append("nvidia")
    dri = root / "dev" / "dri"
    try:
        if any(n.startswith("renderD") for n in os.listdir(dri)):
            found.append("dri")
    except OSError:
        pass
    return found


def probe(root: Path = Path("/"), *, os_cpus: int | None = None) -> dict:
    root = Path(root)
    if os_cpus is None:
        os_cpus = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else (os.cpu_count() or 1)
    return {"os": sys.platform, "machine": os.uname().machine if hasattr(os, "uname") else "", "cpus": _cpus(root, os_cpus),
            "mem_bytes": _mem(root), "gpu": _gpu(root)}


def choose(profile: dict) -> tuple[str, list[str]]:
    """("host" | "device", the reasons, in words the owner reads)."""
    mem, cpus = profile.get("mem_bytes"), profile.get("cpus")
    short = []
    if type(mem) is not int:
        short.append("memory unknown")
    elif mem < MIN_HOST_MEM:
        short.append(f"memory {mem >> 20} MiB < {MIN_HOST_MEM >> 20} MiB")
    if type(cpus) is not int or cpus < MIN_HOST_CPUS:
        short.append(f"cpus {cpus} < {MIN_HOST_CPUS}")
    if short:
        return "device", short + ["v0 has device compute only as a VM-emulated Pi: this session runs the gate without a local model"]
    return "host", [f"memory {mem >> 20} MiB >= {MIN_HOST_MEM >> 20} MiB", f"cpus {cpus} >= {MIN_HOST_CPUS}"]
