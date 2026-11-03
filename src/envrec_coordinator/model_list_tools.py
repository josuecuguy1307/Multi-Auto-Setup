"""Read the models an API lists (a services tool, mixed into `Services`): the coordinator asks the node that holds the credential, stores the dated list, and says which models the router may now offer. No key
passes through the coordinator or the chat; a cloud stance of `never`, a closed access or an offline node refuses with its reason."""

from __future__ import annotations

import asyncio

from envrec_coordinator.router import EntryChat, Task
from envrec_coordinator.router.catalog import build_candidates
from envrec_net import envrun as E

ML_NO_LIST = "{environment} has no list of models to read: the catalog's data for it names none."
ML_NO_NODE = "No paired computer that is online has the access to {environment}, so its list cannot be read (the key stays on that computer)."
ML_CLOSED = "Your profile keeps cloud models closed, so {environment} was not contacted."
ML_FAILED = "The list could not be read: {why}."
ML_DONE = "{environment}: {n} models listed on {when} (read through {machine}; page sha256 {sha}). Chat models the router may offer: {chat}. Listed but not offered: {other}."


class ModelListTools:
    async def _read_models(self, entry: dict, route) -> dict:
        rec = self.coord.tasks.start(route.machine_id, "model_list", {"environment_id": entry["id"], "access_id": route.access_id}, by="GUARD")
        await asyncio.wait_for(rec.done.wait(), 60)
        if rec.status != "done":
            raise RuntimeError(rec.reason or rec.status)
        return rec.result

    def refresh_models(self, ch, *, environment: str, machine: str | None = None) -> dict:
        entry = self.catalog.get(environment)
        if entry is None or not any(E.adapter_of(entry, a["id"]).get("models_list") for a in entry.get("accesses", [])):
            return self._out(False, ML_NO_LIST.format(environment=environment))
        if self.policy.cloud_stance == "never":
            return self._out(False, ML_CLOSED.format(environment=environment))
        probe = Task(task_id="ml-probe", task_type=entry["task_types"][0], sensitivity="public", entry_chat=EntryChat(kind="local", name="models"), created_by="OWNER", payload_ref="models")
        cands, _ = build_candidates(probe, self.envs, self.facts, self.policy, {})
        node = self.coord.harness._resolve_node(machine) if machine else None
        route = next((c.route for c in cands if c.route.environment_id == environment and (node is None or c.route.machine_id == node)), None)
        if route is None:
            return self._out(False, ML_NO_NODE.format(environment=environment))
        job = self._job(f"models-{environment}", "models", "running", sensitivity="public", data={"environment": environment})
        asyncio.ensure_future(self._models_job(job, entry, route))
        return self._out(True, f"Reading the models {environment} lists through {self._name(route.machine_id)}: job {job.id} (mas_job).", job_id=job.id)

    async def _models_job(self, job, entry: dict, route) -> None:
        try:
            result = await self._read_models(entry, route)
            doc = self.model_lists.save(entry, route.access_id, self._name(route.machine_id), result, clock=self.coord.clock)
        except (RuntimeError, ValueError, TimeoutError) as exc:
            job.state, job.text = "failed", ML_FAILED.format(why=str(exc)[:160])
            self.coord.log.add("model_list_failed", {"environment": entry["id"], "reason": str(exc)[:120], "tag": "GUARD"})
            return
        other = [i for i in doc["ids"] if i not in doc["chat_ids"]]
        job.state, job.text = "done", ML_DONE.format(environment=entry["id"], n=len(doc["ids"]), when=doc["retrieved_at"], machine=doc["machine"], sha=doc["sha256"][:16], chat=", ".join(doc["chat_ids"]) or "none", other=", ".join(other) or "none")
        job.data.update(chat_ids=doc["chat_ids"], sha256=doc["sha256"])
        self.coord.log.add("model_list_read", {"environment": entry["id"], "machine": route.machine_id, "ids": len(doc["ids"]), "chat": len(doc["chat_ids"]), "sha256": doc["sha256"], "tag": "GUARD"})
