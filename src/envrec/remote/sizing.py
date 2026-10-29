"""Size table, owned by the human (edit it yourself; the model never writes it).

Maps RAM brackets to the largest model that is safe to run, then applies the stricter of: free disk,
the target's max_disk_gb, and the target's max_model_params_b. Numbers are conservative q4 estimates.
"""

from __future__ import annotations

from dataclasses import dataclass

from envrec.skills.data import reference_json

_TABLE = reference_json("workspace-sizing", "size-table.json")  # the human-owned table lives in the workspace-sizing skill
RAM_TABLE: list[tuple[float, float]] = [(float(a), float(b)) for a, b in _TABLE["ram_table"]]  # (min RAM in GiB, max params in billions); first match from the top wins
CATALOG_PARAMS_B = [float(x) for x in _TABLE["catalog_params_b"]]  # model sizes we know how to fetch
QUANT = _TABLE["quant"]
GB_PER_B_PARAMS = float(_TABLE["gb_per_b_params"])  # q4 weights
RUNTIME_OVERHEAD_GB = float(_TABLE["runtime_overhead_gb"])


@dataclass(frozen=True)
class ModelChoice:
    params_b: float  # 0.0 means: no model fits
    quant: str
    disk_gb: float
    limited_by: str  # ram | disk | config_params | none


def disk_needed_gb(params_b: float) -> float:
    return round(params_b * GB_PER_B_PARAMS + RUNTIME_OVERHEAD_GB, 2)


def ram_cap_params_b(ram_gib: float) -> float:
    return next((cap for floor, cap in RAM_TABLE if ram_gib >= floor), 0.0)


def choose_model(ram_gib: float, free_disk_gb: float, max_disk_gb: float, max_model_params_b: float) -> ModelChoice:
    """Largest catalog model within RAM bracket, disk budget and configured parameter cap. Names what bound it."""
    disk_budget = min(free_disk_gb, max_disk_gb)
    caps = {"ram": ram_cap_params_b(ram_gib), "config_params": max_model_params_b,
            "disk": max((p for p in CATALOG_PARAMS_B if disk_needed_gb(p) <= disk_budget), default=0.0)}
    binding = min(caps, key=lambda k: caps[k])  # the stricter wins; ties go to the first listed (ram)
    limit = caps[binding]
    fit = max((p for p in CATALOG_PARAMS_B if p <= limit), default=0.0)
    if fit == 0.0:
        return ModelChoice(0.0, QUANT, 0.0, binding)
    # name the constraint that actually stopped a bigger catalog size
    bigger = next((p for p in CATALOG_PARAMS_B if p > fit), None)
    limited = "none" if bigger is None else min(
        (k for k in caps if caps[k] < bigger), key=lambda k: caps[k], default="none")
    return ModelChoice(fit, QUANT, disk_needed_gb(fit), limited)
