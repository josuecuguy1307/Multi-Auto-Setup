"""The scoring function used to rank candidates BEFORE any trial. Owned by the human: edit the numbers, not the model.

It scores catalog entries that already satisfy every hard constraint (platform, RAM, disk, limits). It is a prior,
not a measurement: the trials replace it with facts. Each term is a documented reason, no learned weights.
"""

from __future__ import annotations

import math

from envrec.catalog import CatalogEntry
from envrec.pairing.profile import DeviceProfile

QUANT_BONUS = {"Q8_0": 0.4, "Q4_K_M": 0.0, "Q4_0": -0.3}  # higher fidelity helps, but costs memory (headroom term pays for that)
UNKNOWN_QUANT = -0.5
ACCEL_BONUS = 0.8  # an accelerated backend is only here because the hardware matches; worth a trial
HEADROOM_FLOOR = 0.25  # less than 25% of RAM left free is penalised
HEADROOM_PENALTY = 8.0
DISK_PENALTY_PER_GB = 0.05  # downloads cost time and space
MAX_PER_MODEL = 2  # top-K should not be one model in K quantizations


def score(e: CatalogEntry, p: DeviceProfile) -> float:
    s = math.log2(max(e.model.params_b, 0.1) * 10)  # quality prior: each doubling of parameters is worth +1
    s += QUANT_BONUS.get(e.model.quantization, UNKNOWN_QUANT)
    if e.runtime.backend != "cpu":
        s += ACCEL_BONUS
    headroom = (p.ram_gib - e.min_ram_gib) / p.ram_gib if p.ram_gib > 0 else 0.0
    if headroom < HEADROOM_FLOOR:
        s -= (HEADROOM_FLOOR - headroom) * HEADROOM_PENALTY
    s -= e.min_disk_gb * DISK_PENALTY_PER_GB
    return round(s, 4)


def top_k(entries: list[CatalogEntry], p: DeviceProfile, k: int = 3, max_per_model: int = MAX_PER_MODEL) -> list[tuple[CatalogEntry, float]]:
    """Highest score first, ties broken by id; no more than `max_per_model` entries of the same model repo."""
    scored = sorted(((e, score(e, p)) for e in entries), key=lambda t: (-t[1], t[0].id))
    out, per_model = [], {}
    for e, s in scored:
        if per_model.get(e.model.repo, 0) >= max_per_model:
            continue
        per_model[e.model.repo] = per_model.get(e.model.repo, 0) + 1
        out.append((e, s))
        if len(out) == k:
            break
    return out
