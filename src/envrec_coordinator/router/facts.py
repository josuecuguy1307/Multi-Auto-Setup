"""What the router reads: the machines it can use, the owner's policy, and the stored facts (admission, time, load, quota). The router never writes any of them (T4c writes admission; the board
counts leases). `DictFacts` is the in-memory form used by tests and by the SIMULATED-LOCAL runs; the coordinator's store adapter implements the same methods."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from envrec_net import envrun as E


@dataclass
class MachineFacts:
    machine_id: str
    os_family: str
    online: bool = True  # the machine answers now (the registry's session); an offline machine is left out with the typed reason `machine_offline`, never retried silently
    paired: bool = True  # only the owner's paired computers are ever machines; a third-party machine is not representable here
    installed: set[str] = field(default_factory=set)  # environment ids found installed (presence only)
    signed_in: set[str] = field(default_factory=set)  # access ids whose session exists (presence and the tool's own status output; never a token)
    env_names: set[str] = field(default_factory=set)  # NAMES of environment variables that exist (never values)
    sandbox_backends: list[str] = field(default_factory=list)
    model_endpoint: str | None = None  # the loopback server T3a installed: "127.0.0.1:port"
    model_id: str | None = None


UNMEASURED = ("measured_first", "cost_first")


@dataclass
class Policy:
    """The owner's policy as the router reads it. Defaults come from policy.json (the conservative option) until the owner's draft supplies the real values."""
    open_subtypes: set[str]
    approved_providers_for_internal: set[str]
    automated_use_cleared: set[str]
    closed_accesses: set[str] = field(default_factory=set)
    tie_margin: float = 0.1
    cost_order: tuple[str, ...] = ("local", "api", "subscription")
    max_escalation: int = 2
    threshold: int = 8
    of: int = 10
    outbound: dict[str, Any] = field(default_factory=dict)
    unmeasured: str = "measured_first"  # what an unmeasured candidate is worth against a measured one (policy.json): `measured_first` or `cost_first`

    @classmethod
    def defaults(cls, doc: dict | None = None) -> Policy:
        d = doc or E.load_policy()
        r, o = d["rule"], d["owner_policy_defaults"]
        if r.get("unmeasured", "measured_first") not in UNMEASURED:
            raise ValueError(f"policy.json rule.unmeasured must be one of {UNMEASURED}, not {r.get('unmeasured')!r}")
        return cls(set(o["open_subtypes"]), set(o["approved_providers_for_internal"]), set(o["automated_use_cleared"]), set(), float(r["tie_margin"]), tuple(r["cost_order"]), int(r["max_escalation"]),
                   int(r["admission_threshold"]), int(r["admission_of"]), d.get("outbound", {}), unmeasured=r.get("unmeasured", "measured_first"))


class Facts(Protocol):
    def machines(self) -> list[MachineFacts]: ...
    def admission(self, key: str) -> dict | None: ...
    def identity(self, route_key: str, task_type: str | None = None) -> dict: ...
    def time(self, route_key: str) -> float | None: ...
    def active_leases(self, machine_id: str) -> int: ...
    def quota(self, plan_group: str) -> int: ...


def admission_key(task_type: str, route) -> str:
    return f"admission.{task_type}.{route.machine_id}.{route.environment_id}.{route.model_id}.{route.access_id}"


class DictFacts:
    def __init__(self, machines: list[MachineFacts], *, admission: dict[str, dict] | None = None, identity: dict[str, dict] | None = None, times: dict[str, float] | None = None,
                 leases: dict[str, int] | None = None, quotas: dict[str, int] | None = None) -> None:
        self._m, self._adm, self._id, self._t, self._l, self._q = machines, admission or {}, identity or {}, times or {}, leases or {}, quotas or {}

    def machines(self) -> list[MachineFacts]:
        return self._m

    def admission(self, key: str) -> dict | None:
        return self._adm.get(key)

    def identity(self, route_key: str, task_type: str | None = None) -> dict:
        return self._id.get(route_key, {})

    def time(self, route_key: str) -> float | None:
        return self._t.get(route_key)

    def active_leases(self, machine_id: str) -> int:
        return self._l.get(machine_id, 0)

    def quota(self, plan_group: str) -> int:
        return self._q.get(plan_group, 0)
