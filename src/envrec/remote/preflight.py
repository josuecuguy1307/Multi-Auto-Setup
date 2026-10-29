"""Read-only preflight over the template library (tier observe): arch, OS, RAM, disk, GPU, package manager.

RAM is the stricter of what the kernel reports and the cgroup limit the process is actually held to, so a
container that sees its host's RAM but is capped lower is sized by the cap.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from envrec.contracts import CreatedBy, Evidence, Fact, FactStatus, Target
from envrec.mission.models import Transport
from envrec.remote.runner import RemoteRunner
from envrec.store import Store

GIB = 2**30
UNLIMITED_CUTOFF = 2**50  # cgroup v1 reports ~2^63 for "no limit"


class PreflightError(RuntimeError):
    pass


@dataclass
class Preflight:
    arch: str
    os_name: str
    os_id: str | None
    ram_reported_gib: float
    ram_limit_gib: float | None  # None = no cgroup limit
    disk_free_gb: float
    gpu: bool
    pkg_manager: str
    python: str | None
    cpus: int = 1
    cpu_features: tuple[str, ...] = ()
    accel: str = "none"
    raw: dict[str, str] = field(default_factory=dict)

    @property
    def ram_effective_gib(self) -> float:
        return round(min(self.ram_reported_gib, self.ram_limit_gib) if self.ram_limit_gib is not None else self.ram_reported_gib, 2)


def parse_meminfo(text: str) -> float:
    for line in text.splitlines():
        if line.startswith("MemTotal:"):
            return round(int(line.split()[1]) * 1024 / GIB, 2)
    raise PreflightError("MemTotal not found in /proc/meminfo")


def parse_cgroup(text: str) -> float | None:
    t = text.strip()
    if not t or t == "max":
        return None
    try:
        n = int(t)
    except ValueError:
        return None
    return None if n >= UNLIMITED_CUTOFF else round(n / GIB, 2)


def parse_df(text: str) -> float:
    rows = [ln.split() for ln in text.strip().splitlines()]
    if len(rows) < 2 or len(rows[-1]) < 4:
        raise PreflightError("could not read df output")
    return round(int(rows[-1][3]) * 1024 / 1e9, 2)


def parse_os_id(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith("ID="):
            return line.split("=", 1)[1].strip().strip('"')
    return None


def parse_cpu_flags(text: str) -> tuple[str, ...]:
    """'flags : fpu avx2 ...' or 'Features : fp asimd ...' -> a sorted tuple of the flags."""
    if ":" not in text:
        return ()
    return tuple(sorted(set(text.split(":", 1)[1].split())))


def run_preflight(runner: RemoteRunner, target_id: str, transport: Transport) -> Preflight:
    def get(template: str, values: dict | None = None, *, required: bool = True) -> str:
        r = runner.run(target_id, transport, template, values)
        if not r.ok:
            if required:
                raise PreflightError(f"{template} failed (exit {r.exit_code}): {r.stderr.strip()[:160]}")
            return ""
        return r.stdout.strip()

    raw = {t: get(t) for t in ("uname_m", "uname_s", "os_release", "meminfo", "cgroup_mem", "gpu_probe", "pkg_manager")}
    raw["df_free"] = get("df_free", {"path": "/opt"})
    py = get("python_version", required=False) or None
    raw["nproc"] = get("nproc", required=False) or "1"
    raw["cpu_flags"] = get("cpu_flags", required=False)
    raw["accel_probe"] = get("accel_probe", required=False) or "none"
    return Preflight(
        arch=raw["uname_m"], os_name=raw["uname_s"], os_id=parse_os_id(raw["os_release"]),
        ram_reported_gib=parse_meminfo(raw["meminfo"]), ram_limit_gib=parse_cgroup(raw["cgroup_mem"]),
        disk_free_gb=parse_df(raw["df_free"]), gpu=raw["gpu_probe"] == "nvidia", pkg_manager=raw["pkg_manager"],
        python=py, cpus=int(raw["nproc"]) if raw["nproc"].isdigit() else 1,
        cpu_features=parse_cpu_flags(raw["cpu_flags"]), accel=raw["accel_probe"], raw=raw)


def preflight_facts(pf: Preflight, target: Target, store: Store, transport: Transport, event_id: str | None = None) -> list[Fact]:
    raw_ref = store.put_raw("\n".join(f"{k}: {v}" for k, v in pf.raw.items()))
    ev = Evidence(provider=f"remote:{transport.value}", probe="preflight", event_id=event_id, raw_ref=raw_ref, confidence=0.9,
                  limitations=["read over the transport with fixed read-only templates"])
    pairs = [("os.name", pf.os_name), ("os.arch", pf.arch), ("hw.ram_total_gib", pf.ram_effective_gib),
             ("hw.ram_host_reported_gib", pf.ram_reported_gib), ("hw.disk_free_gib", pf.disk_free_gb),
             ("host.package_manager", pf.pkg_manager), ("hw.gpu_present", pf.gpu),
             ("hw.accelerator", pf.accel), ("hw.cpu_threads", pf.cpus)]
    if pf.ram_limit_gib is not None:
        pairs.append(("hw.ram_limit_gib", pf.ram_limit_gib))
    facts = [Fact(target_id=target.id, key=k, value=v, status=FactStatus.tested, evidence=[ev], created_by=CreatedBy.adapter)
             for k, v in pairs]
    for f in facts:
        store.write_fact(f)
    return facts
