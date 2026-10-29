"""Pick a (runtime, model) pair for one device: rule table first, the model may refine inside a closed menu.

The menu holds only entries that already satisfy every hard constraint, so a model reply can never pick something
too big, unlisted, or outside the manifest. A bad reply (not JSON, not a menu key) is retried once, then the
rule-table baseline stands. Every outcome is logged as an Event. Nothing is downloaded here.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from envrec.catalog import Catalog, CatalogEntry
from envrec.contracts import Actor, Event
from envrec.model.base import ModelAdapter, ModelError
from envrec.pairing.profile import DeviceProfile
from envrec.pairing.rules import binding_constraint, covers_platform, feasible
from envrec.store import Store

MENU_MAX = 6
SYSTEM = ("You choose which language model a small device should run. Reply with ONE JSON object and nothing else: "
          '{"choice": "<key from the menu>", "why": "<one short sentence>"}. Pick only a key from the menu.')


@dataclass
class PairChoice:
    entry: CatalogEntry | None
    source: str  # baseline | model | fallback_invalid | refused
    baseline_id: str | None = None
    menu: dict[str, str] = field(default_factory=dict)  # key -> entry id
    reason: str = ""
    rejected: dict[str, int] = field(default_factory=dict)
    needs_proposal: bool = False
    errors: list[str] = field(default_factory=list)


def _menu_prompt(p: DeviceProfile, menu: dict[str, CatalogEntry]) -> str:
    lines = [f"Device: {p.os}/{p.arch}, {p.ram_gib} GiB RAM, {p.cpu_threads} CPU threads, accelerator {p.accel}, "
             f"{min(p.free_disk_gb, p.max_disk_gb):.1f} GB usable disk.", "Menu (every option already fits this device):"]
    for k, e in menu.items():
        lines.append(f'  "{k}": {e.model.params_b:g}B parameters, {e.model.quantization}, {e.runtime.backend} backend, '
                     f'{e.model.bytes / 1e9:.2f} GB model, needs {e.min_ram_gib:g} GiB RAM')
    lines.append('A larger model is usually better; a smaller one leaves more headroom. Reply {"choice": "<key>", "why": "..."}.')
    return "\n".join(lines)


def _parse(text: str, menu: dict[str, CatalogEntry]) -> tuple[str | None, str]:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None, "reply has no JSON object"
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError as exc:
        return None, f"reply is not valid JSON ({exc.msg})"
    key = str(obj.get("choice", "")) if isinstance(obj, dict) else ""
    return (key, "") if key in menu else (None, f"choice {key!r} is not a menu key")


async def choose_pair(cat: Catalog, p: DeviceProfile, *, store: Store | None = None, run_id: str | None = None,
                      model: ModelAdapter | None = None, requested_runtime: str | None = None,
                      menu_max: int = MENU_MAX) -> PairChoice:
    def log(kind: str, res: PairChoice, extra: dict | None = None) -> PairChoice:
        if store is not None:
            store.append_event(Event(actor=Actor.policy, type=kind, run_id=run_id, payload={
                "target": p.target_id, "signature": p.signature, "source": res.source, "baseline": res.baseline_id,
                "chosen": res.entry.id if res.entry else None, "menu": res.menu, "reason": res.reason,
                "rejected": res.rejected, "errors": res.errors, **(extra or {})}))
        return res

    sources = {a.source for a in cat.lock.runtime_assets}
    if requested_runtime is not None and requested_runtime not in sources:
        return log("pair_refused", PairChoice(None, "refused", needs_proposal=True,
                                              reason=f"runtime {requested_runtime!r} is not in the approved manifest/catalog; it is never fetched. A written proposal is the only way in"))
    if p.unfamiliar_reasons or not covers_platform(cat, p):
        why = p.unfamiliar_reasons or [f"no catalog runtime for {p.os}/{p.arch}"]
        return log("pair_refused", PairChoice(None, "refused", needs_proposal=True, reason="unfamiliar hardware: " + "; ".join(why)))

    fit = feasible(cat, p)
    if not fit.entries:
        dominant = max(fit.rejected, key=fit.rejected.get) if fit.rejected else "empty_catalog"
        return log("pair_refused", PairChoice(None, "refused", rejected=fit.rejected, reason=
                   f"nothing in the catalog fits this device (most entries failed on {dominant}; strictest stated limit: {binding_constraint(p)})"))

    top = fit.entries[:menu_max]
    base = top[0]
    menu = {str(i): e for i, e in enumerate(top, 1)}
    res = PairChoice(base, "baseline", base.id, {k: e.id for k, e in menu.items()}, "rule table: largest model that fits, cpu backend first",
                     fit.rejected)
    if model is None or len(top) == 1:
        return log("pair_choice", res)

    prompt, errors = _menu_prompt(p, menu), []
    for attempt in (1, 2):
        try:
            done = await model.complete(SYSTEM, prompt if attempt == 1 else f"{prompt}\nYour last reply was rejected: {errors[-1]}. Use a key from the menu.")
        except ModelError as exc:
            errors.append(f"backend error: {exc}")
            continue
        key, err = _parse(done.text, menu)
        if key is not None:
            res.entry, res.source, res.errors = menu[key], "model", errors
            res.reason = "model refined the baseline inside the closed menu" if menu[key].id != base.id else "model agreed with the baseline"
            return log("pair_choice", res)
        errors.append(err)
    res.source, res.errors = "fallback_invalid", errors
    res.reason = "model reply invalid twice; the rule-table baseline stands"
    return log("pair_choice", res)
