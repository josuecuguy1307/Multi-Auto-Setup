"""The router's facts, read from the coordinator: its registry of paired machines, their stored analysis, the setup that is (or is no longer) running on each, the credential names present, and the
admission measurements in its log. It reads and decides nothing: every field of `MachineFacts` is a fact a probe or a record already holds, and a machine without one says so by leaving it out."""

from __future__ import annotations

import hashlib
from collections.abc import Callable

from envrec_coordinator.admission.facts import AdmissionFacts
from envrec_coordinator.admission.router_bridge import RouterAdmissionFacts
from envrec_coordinator.admission.suite import load_suite
from envrec_coordinator.router.facts import MachineFacts
from envrec_net import envrun as E


class CoordinatorFacts(RouterAdmissionFacts):
    def __init__(self, coord, store: AdmissionFacts, envs: dict[str, dict], *, suite_for: Callable = load_suite, aliases: dict[str, str] | None = None, leases: Callable[[str], int] | None = None) -> None:
        super().__init__([], store, self._current, suite_for=suite_for)
        self.coord, self.envs = coord, envs
        self.aliases = aliases if aliases is not None else E.load_policy().get("os_family_aliases", {})
        self.credential_names: dict[str, set[str]] = {}  # node id -> credential NAMES the node reported present (a presence probe or an access added/removed through the harness)
        self._leases = leases
        self.model_lists = None  # set by Services: the models an API lists (selectable_models)

    def selectable_models(self, env_id: str, access_id: str) -> list[str] | None:
        entry = self.envs.get(env_id)
        return self.model_lists.selectable(entry, access_id) if (self.model_lists is not None and entry is not None) else None

    # ---- machines ----------------------------------------------------------------------------------------------------------------------------------

    def machines(self) -> list[MachineFacts]:
        return [self._machine(rec) for rec in self.coord.registry.all()]

    def _machine(self, rec) -> MachineFacts:
        nid = rec.node_id
        doc = self.coord.analysis_of(nid) or {}
        facts = doc.get("facts") or {}
        val = lambda k, d=None: (facts.get(k) or {}).get("value", d)  # noqa: E731
        fam = str(val("machine.os.family") or rec.os or "").lower()
        fam = self.aliases.get(fam, fam)
        clis = val("machine.ai_clis") or []
        seen = {str(c.get("id", "")).lower() for c in clis} | {str(c.get("name", "")).lower() for c in clis}
        signed = {str(c.get("id", "")).lower() for c in clis if c.get("set_up")} | {str(c.get("name", "")).lower() for c in clis if c.get("set_up")}
        installed, signed_in = set(), set()
        for e in self.envs.values():
            exe = (E.adapter_of(e).get("exe") or {}).get(fam)
            if e.get("needs_program", True) and exe and exe.lower() in seen:
                installed.add(e["id"])
                signed_in |= {a["id"] for a in e.get("accesses", []) if a.get("kind") == "plan" and exe.lower() in signed}
        if self.coord.runs.stored_setup(nid) is not None:  # a setup left something on this machine: a local model environment is installed there, whether or not its server is up now
            installed |= {e["id"] for e in self.envs.values() if e.get("subtype") == "local_model"}
        installed |= set(self.coord.runs.stored_tools(nid))  # tools the harness itself installed here (the owner approved the plan; the node recorded where they are)
        names = {str(k.get("name")) for k in (val("machine.api_key_variable_names") or []) if k.get("name")} | self.credential_names.get(nid, set())
        endpoint, model = self._local_server(nid)
        return MachineFacts(machine_id=nid, os_family=fam, online=nid in self.coord._sessions, installed=installed, signed_in=signed_in, env_names=names,
                            sandbox_backends=list(val("machine.sandbox_backends") or []), model_endpoint=endpoint, model_id=model)

    def _local_server(self, nid: str) -> tuple[str | None, str | None]:
        """The loopback model server a setup left on this machine, only while its start step is still done (undoing it, or never running it, leaves none), and the model the owner picked."""
        rec = self.coord.runs.stored_setup(nid)
        pick = self.coord.runs.stored_pick(nid)
        if rec is None or pick is None:
            return None, None
        steps = rec["plan"].get("steps", [])
        status = {str(k): v for k, v in rec["status"].items()}
        started = next((s for s in steps if s.get("op") == "start_server"), None)
        if started is None or status.get(str(started["n"])) != "done":
            return None, None
        return (rec["plan"].get("verification") or {}).get("endpoint"), str(pick["option"].get("model_file") or pick["option"].get("model") or pick["option"]["id"]).split("/")[-1]

    # ---- the rest of the facts the rule reads -------------------------------------------------------------------------------------------------------

    def active_leases(self, machine_id: str) -> int:
        return self._leases(machine_id) if self._leases else 0

    def _current(self, route_key: str) -> tuple[str | None, str | None]:
        """(identity of the model now, version of the environment now) for a route: what a measurement is valid for beyond its key. An API has no model file."""
        env_id, _model, access, machine = route_key.split("|")
        entry = self.envs.get(env_id)
        if entry is None:
            return None, None
        pick = self.coord.runs.stored_pick(machine) if E.uses_local_endpoint(entry, access) else None
        sha = hashlib.sha256(f"{pick['option']['id']}|{(pick['option'].get('build') or {}).get('sha256')}|{pick['option'].get('model')}".encode()).hexdigest() if pick else None
        return sha, E.recipe_sha256(entry)
