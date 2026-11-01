"""Candidates: the catalog (data) crossed with what is real on each machine. An environment that is left out says why, with a typed reason (docs/ROUTER_API.md section 1)."""

from __future__ import annotations

from dataclasses import dataclass

from envrec_coordinator.router.facts import Facts, MachineFacts, Policy
from envrec_coordinator.router.model import Route, Task
from envrec_net import envrun as E


@dataclass
class Candidate:
    route: Route
    entry: dict
    mode: dict
    machine: MachineFacts
    access: dict

    @property
    def cost_class(self) -> str:
        return self.access["cost_class"]

    @property
    def cloud(self) -> bool:
        return bool(self.entry.get("cloud"))


def _access_present(access: dict, m: MachineFacts) -> str | None:
    """None when the access exists on the machine, else the typed reason. Presence only: a session or a variable NAME, never a token or a value."""
    kind = access["kind"]
    if kind == "plan":
        return None if access["id"] in m.signed_in else "not_signed_in"
    if kind == "api_key":
        every, any_of = set(access.get("env_names", [])), set(access.get("env_names_any", []))
        return None if every <= m.env_names and (not any_of or any_of & m.env_names) else "no_access"  # variable NAMES only: all of `env_names`, and at least one of `env_names_any`
    if kind == "local":
        return None if m.model_endpoint else "no_access"
    return "no_access"


def knows_task_type(envs: dict[str, dict], task_type: str) -> bool:
    return any(task_type in e.get("task_types", []) for e in envs.values())


def build_candidates(task: Task, envs: dict[str, dict], facts: Facts, policy: Policy) -> tuple[list[Candidate], dict[str, str]]:
    out: list[Candidate] = []
    left_out: dict[str, str] = {}
    for entry in envs.values():
        if task.task_type not in entry.get("task_types", []):
            continue
        for m in facts.machines():
            tag = f"{entry['id']}@{m.machine_id}"
            mode = E.mode_of(entry, task.task_type)
            reason = None
            if not m.paired:
                reason = "not_paired"
            elif m.os_family not in entry.get("os", []):
                reason = "unsupported_os"
            elif entry["subtype"] not in policy.open_subtypes:
                reason = "closed_by_owner"
            elif entry.get("automated_use") == "to review" and entry["id"] not in policy.automated_use_cleared:
                reason = "automated_use_unreviewed"
            elif entry["subtype"] == "local_model":
                reason = None if m.model_endpoint else "not_installed"
            elif entry["id"] not in m.installed:
                reason = "not_installed"
            elif entry["adapter"].get("needs_model_endpoint") and not m.model_endpoint:
                reason = "no_access"
            if reason is None and mode is None:
                reason = "unsupported_os"
            if reason is None and mode["confinement"] in E.NEEDS_SANDBOX and not m.sandbox_backends:
                reason = "sandbox_missing"
            if reason is not None:
                left_out[tag] = reason
                continue
            first_fail, found = None, False
            for acc in entry["accesses"]:
                if acc["id"] in policy.closed_accesses:
                    first_fail = first_fail or "closed_by_owner"
                    continue
                why = _access_present(acc, m)
                if why is not None:
                    first_fail = first_fail or why
                    continue
                found = True
                model = (m.model_id or "unknown") if entry["subtype"] in ("local_model", "own_harness") else "default"
                out.append(Candidate(Route(environment_id=entry["id"], subtype=entry["subtype"], model_id=model, access_id=acc["id"], machine_id=m.machine_id), entry, mode, m, acc))
            if not found:
                left_out[tag] = first_fail or "no_access"
    return out, left_out
