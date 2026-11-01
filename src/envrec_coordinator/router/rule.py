"""The written selection rule (docs/NETWORK_CONTRACT.md 10.2-10.3, docs/ROUTER_API.md section 3). Pure: the same candidates, facts and policy give the same choice, and every step is recorded.

This is a GUARD policy, not a model decision: nothing here is learned, no model is asked, and the order and the tie margin are data (policy.json)."""

from __future__ import annotations

from envrec_coordinator.router import strings
from envrec_coordinator.router.catalog import Candidate
from envrec_coordinator.router.facts import Facts, Policy, admission_key
from envrec_coordinator.router.model import Step


def measured(c: Candidate, facts: Facts, task_type: str, policy: Policy) -> tuple[int, int] | str | None:
    """(passed, of) when a valid admission fact exists and passes; 'not_admitted' when it exists, is valid and is below the threshold; None when there is none, it is not tested, not code-graded or
    stale (the model, environment, machine, suite or access changed since it was measured)."""
    fact = facts.admission(admission_key(task_type, c.route))
    if not fact or fact.get("status") != "tested" or fact.get("graded_by") != "code" or facts.identity(c.route.key, task_type) != fact.get("identity", {}):
        return None
    passed, of = int(fact["passed"]), int(fact.get("of", policy.of))
    return (passed, of) if passed >= policy.threshold else "not_admitted"


def sensitivity_step(cands: list[Candidate], sensitivity: str, facts: Facts, task_type: str, policy: Policy, tried: set[str]) -> tuple[list[Candidate], dict[str, str], Step]:
    """Step 1. confidential: local environments on the owner's paired machines, measured here (R1, R3). internal: providers only if the owner approved them. public: the owner policy alone."""
    kept, dropped = [], {}
    for c in cands:
        k = c.route.key
        if k in tried:
            dropped[k] = "already_tried"
        elif sensitivity == "confidential" and (c.cloud or c.route.subtype in ("provider", "embedded")):
            dropped[k] = "cloud"
        elif sensitivity == "confidential" and not isinstance(measured(c, facts, task_type, policy), tuple):
            dropped[k] = "provisional"
        elif sensitivity == "internal" and c.cloud and c.entry["id"] not in policy.approved_providers_for_internal:
            dropped[k] = "provider_not_approved"
        else:
            kept.append(c)
    step = Step(step="sensitivity", candidates_in=[c.route.key for c in cands], candidates_out=[c.route.key for c in kept], values={k: v for k, v in dropped.items()}, note=f"level {sensitivity}")
    return kept, dropped, step


def exclusion_step(cands: list[Candidate], exclude) -> tuple[list[Candidate], dict[str, str], Step]:
    """Step 1b. The task's own hard filter: machines and environments it must not use. Applied after the sensitivity step, so it can only remove candidates, never add or relax anything."""
    machines, envs = set(exclude.machine_ids), set(exclude.environment_ids)
    kept, dropped = [], {}
    for c in cands:
        if c.route.machine_id in machines:
            dropped[c.route.key] = "excluded_machine"
        elif c.route.environment_id in envs:
            dropped[c.route.key] = "excluded_environment"
        else:
            kept.append(c)
    return kept, dropped, Step(step="exclusion", candidates_in=[c.route.key for c in cands], candidates_out=[c.route.key for c in kept], values=dict(dropped),
                                note=f"machines {sorted(machines)}, environments {sorted(envs)}; never relaxed")


def run_rule(cands: list[Candidate], facts: Facts, task_type: str, policy: Policy) -> tuple[Candidate, list[Step], str]:
    """Steps 2-6 over candidates the sensitivity step let through (never empty). Returns the choice, the trace of these steps and the evidence label."""
    trace: list[Step] = []
    keys = lambda cs: [c.route.key for c in cs]  # noqa: E731
    meas = {c.route.key: measured(c, facts, task_type, policy) for c in cands}
    adm = [c for c in cands if meas[c.route.key] != "not_admitted"]  # a combination that failed the exam for this task type is not approved (spec 8.1)
    dropped = {k: v for k, v in meas.items() if v == "not_admitted"}
    note = "dropped: failed the admission exam"
    if policy.unmeasured == "measured_first" and any(isinstance(meas[c.route.key], tuple) for c in adm):
        dropped.update({c.route.key: "unmeasured" for c in adm if not isinstance(meas[c.route.key], tuple)})  # a measured, admitted candidate exists: one with no measurement does not compete with it
        adm = [c for c in adm if isinstance(meas[c.route.key], tuple)]
        note += "; dropped: no measurement while another candidate is measured and admitted"
    trace.append(Step(step="admission_filter", candidates_in=keys(cands), candidates_out=keys(adm), values=dropped, note=note))
    if not adm:
        return None, trace, ""  # type: ignore[return-value]
    rank = {name: i for i, name in enumerate(policy.cost_order)}
    best = min(rank[c.cost_class] for c in adm)
    cost = [c for c in adm if rank[c.cost_class] == best]
    trace.append(Step(step="cost_class", candidates_in=keys(adm), candidates_out=keys(cost), values={c.route.key: c.cost_class for c in adm}, note="local < API < subscription"))
    rates = {c.route.key: (meas[c.route.key][0] / meas[c.route.key][1] if isinstance(meas[c.route.key], tuple) else None) for c in cost}
    have = [r for r in rates.values() if r is not None]
    if have:
        top = max(have)
        rated = [c for c in cost if rates[c.route.key] is not None and rates[c.route.key] >= top - policy.tie_margin]
    else:
        rated = cost
    trace.append(Step(step="admission_rate", candidates_in=keys(cost), candidates_out=keys(rated), values={k: (strings.EVIDENCE_MEASURED.format(passed=meas[k][0], of=meas[k][1]) if isinstance(meas[k], tuple) else strings.EVIDENCE_NONE) for k in rates},
                      margin=policy.tie_margin, note="measured here ranks above no data; differences inside the margin are a tie"))
    times = {c.route.key: facts.time(c.route.key) for c in rated}
    known = [t for t in times.values() if t is not None]
    fastest = [c for c in rated if times[c.route.key] == min(known)] if known else rated
    trace.append(Step(step="time", candidates_in=keys(rated), candidates_out=keys(fastest), values={k: v for k, v in times.items()}, note="median measured time; none measured keeps all"))
    load = {c.route.key: facts.active_leases(c.route.machine_id) + (facts.quota(c.access["plan_group"]) if c.access.get("plan_group") else 0) for c in fastest}
    low = min(load.values())
    idle = [c for c in fastest if load[c.route.key] == low]
    trace.append(Step(step="load", candidates_in=keys(fastest), candidates_out=keys(idle), values=load, note="active leases on the machine plus the shared quota counter of the plan"))
    chosen = min(idle, key=lambda c: c.route.key)
    trace.append(Step(step="tie_break", candidates_in=keys(idle), candidates_out=[chosen.route.key], note="lexicographic route key, so the decision is deterministic"))
    m = meas[chosen.route.key]
    label = strings.EVIDENCE_MEASURED.format(passed=m[0], of=m[1]) if isinstance(m, tuple) else strings.EVIDENCE_NONE
    return chosen, trace, label
