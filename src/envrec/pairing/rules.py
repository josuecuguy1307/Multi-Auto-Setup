"""Hard constraints and the rule-table baseline. Pure code: the model never sees or edits any of this."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from envrec.catalog import Catalog, CatalogEntry
from envrec.pairing.profile import DeviceProfile
from envrec.remote.sizing import ram_cap_params_b

QUANT_PREFERENCE = ["Q4_K_M", "Q4_0", "Q8_0"]  # smaller and the usual default first; the discovery step may try the others
GPU_ACCEL = {"nvidia", "rocm", "dri"}


def backend_ok(backend: str, p: DeviceProfile) -> bool:
    return (backend == "cpu" or (backend == "cuda" and p.accel == "nvidia") or (backend == "vulkan" and p.accel in GPU_ACCEL)
            or (backend == "metal" and p.os == "macos"))


def rejection(e: CatalogEntry, p: DeviceProfile) -> str | None:
    """The first hard constraint this entry breaks on this device, or None when it fits."""
    if e.runtime.os != p.os or e.runtime.arch != p.arch:
        return "platform"
    if not e.runtime.runnable:
        return "runtime_not_supported_by_worker"
    if not backend_ok(e.runtime.backend, p):
        return "backend_needs_hardware"
    if e.model.params_b > min(ram_cap_params_b(p.ram_gib), p.max_params_b):
        return "params_over_limit"
    if e.model.min_ram_gib > p.ram_gib:
        return "ram"
    if e.min_disk_gb > min(p.free_disk_gb, p.max_disk_gb):
        return "disk"
    return None


@dataclass
class Feasible:
    entries: list[CatalogEntry]
    rejected: dict[str, int]


def feasible(cat: Catalog, p: DeviceProfile) -> Feasible:
    ok, why = [], Counter()
    for e in cat.entries():
        r = rejection(e, p)
        if r is None:
            ok.append(e)
        else:
            why[r] += 1
    return Feasible(rank(ok), dict(why))


def rank(entries: list[CatalogEntry]) -> list[CatalogEntry]:
    """Rule-table order: cpu backend first (always works; accelerators are for the discovery step to try), then the largest
    model that fits, then the preferred quantization, then ids for a stable tie-break."""
    def key(e: CatalogEntry):
        q = QUANT_PREFERENCE.index(e.model.quantization) if e.model.quantization in QUANT_PREFERENCE else len(QUANT_PREFERENCE)
        return (e.runtime.backend != "cpu", -e.model.params_b, q, e.id)
    return sorted(entries, key=key)


def binding_constraint(p: DeviceProfile) -> str:
    """Which stated limit is the strictest one, for the log and the refusal text."""
    caps = {"ram": ram_cap_params_b(p.ram_gib), "target_params_limit": p.max_params_b}
    return min(caps, key=lambda k: caps[k])


def covers_platform(cat: Catalog, p: DeviceProfile) -> bool:
    return any(a.os == p.os and a.arch == p.arch for a in cat.lock.runtime_assets)
