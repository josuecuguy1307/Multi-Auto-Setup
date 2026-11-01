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
        return E.is_cloud(self.entry, self.access["id"])


def _missing_names(access: dict, m: MachineFacts) -> list[str]:
    """The variable NAMES an api_key access lacks on the machine (never a value): every one of `env_names`, and, when none of `env_names_any` exists, all of those."""
    every, any_of = list(access.get("env_names", [])), list(access.get("env_names_any", []))
    lack = [n for n in every if n not in m.env_names]
    if any_of and not (set(any_of) & m.env_names):
        lack += any_of
    return lack


def _access_present(access: dict, m: MachineFacts) -> str | None:
    """None when the access exists on the machine, else the typed reason. Presence only: a session or a variable NAME, never a token or a value."""
    kind = access["kind"]
    if kind == "plan":
        return None if access["id"] in m.signed_in else "not_signed_in"
    if kind == "api_key":
        return None if not _missing_names(access, m) else "no_access"
    if kind == "local":
        return None if m.model_endpoint else "no_access"
    return "no_access"


def knows_task_type(envs: dict[str, dict], task_type: str) -> bool:
    return any(task_type in e.get("task_types", []) for e in envs.values())


def build_candidates(task: Task, envs: dict[str, dict], facts: Facts, policy: Policy, detail: dict[str, dict] | None = None) -> tuple[list[Candidate], dict[str, str]]:
    """Candidates and, for every environment left out on a machine, the typed reason. `detail` (optional) receives, per `environment@machine`, facts a reason text needs (the NAMES of the variables an access lacks)."""
    out: list[Candidate] = []
    left_out: dict[str, str] = {}
    detail = detail if detail is not None else {}
    for entry in envs.values():
        if task.task_type not in entry.get("task_types", []):
            continue
        for m in facts.machines():
            tag = f"{entry['id']}@{m.machine_id}"
            reason = None
            if not m.paired:
                reason = "not_paired"
            elif not m.online:
                reason = "machine_offline"
            elif m.os_family not in entry.get("os", []):
                reason = "unsupported_os"
            elif entry["subtype"] not in policy.open_subtypes:
                reason = "closed_by_owner"
            elif entry.get("automated_use") == "to review" and entry["id"] not in policy.automated_use_cleared:
                reason = "automated_use_unreviewed"
            elif entry["subtype"] == "local_model":
                reason = None if m.model_endpoint else ("server_not_running" if entry["id"] in m.installed else "not_installed")  # installed but its loopback server is not up is not "not installed"
            elif entry.get("needs_program", True) and entry["id"] not in m.installed:
                reason = "not_installed"
            if reason is not None:
                left_out[tag] = reason
                continue
            first_fail, found, partial = None, False, {}
            for acc in entry["accesses"]:
                if acc["id"] in policy.closed_accesses:
                    first_fail = first_fail or "closed_by_owner"
                    continue
                why = _access_present(acc, m)
                if why is None and E.adapter_of(entry, acc["id"]).get("needs_model_endpoint") and not m.model_endpoint:
                    why = "no_access"
                mode = E.mode_of(entry, task.task_type, acc["id"])
                if why is None and mode is None:
                    why = "unsupported_os"
                if why is None and mode["confinement"] in E.NEEDS_SANDBOX and not m.sandbox_backends:
                    why = "sandbox_missing"
                if why is not None:
                    if first_fail is None and why == "no_access" and acc["kind"] == "api_key":
                        detail[tag] = {"missing": _missing_names(acc, m), "any": bool(acc.get("env_names_any"))}
                    part = f"{entry['id']} via {acc['id']}@{m.machine_id}"  # an access that is not a candidate says why, even when another access of the same environment is
                    partial[part] = (why, {"missing": _missing_names(acc, m), "any": bool(acc.get("env_names_any"))} if why == "no_access" and acc["kind"] == "api_key" else None)
                    first_fail = first_fail or why
                    continue
                found = True
                declared = E.declared_model(entry, acc["id"])  # a model the data names (an API's model); else the machine's own local model
                model = declared or ((m.model_id or "unknown") if entry["subtype"] in ("local_model", "own_harness") else "default")
                out.append(Candidate(Route(environment_id=entry["id"], subtype=entry["subtype"], model_id=model, access_id=acc["id"], machine_id=m.machine_id), entry, mode, m, acc))
            if not found:
                left_out[tag] = first_fail or "no_access"
            else:  # some access works: the ones that do not are still reported, one line each
                for part, (why, det) in partial.items():
                    left_out[part] = why
                    if det:
                        detail[part] = det
    return out, left_out
