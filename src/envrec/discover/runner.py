"""The real TrialRunner: controller cache + Provisioner + real-model verifier, for one device and one transport.

Everything it can fetch or install comes from the lockfile through the Fetcher's registry; it has no URL or file name of its
own. A deploy that fails is raised as an error so discovery records the trial as errored and removes what was installed.
"""

from __future__ import annotations

from envrec.catalog import Catalog
from envrec.mission.models import Transport
from envrec.remote.provision import SERVER_PORT, DeployPlan, Provisioner
from envrec.runtime.fetch import Fetcher
from envrec.verify.model_check import ExpectedModel, ModelVerification


class DeployFailed(RuntimeError):
    pass


class ProvisionerTrialRunner:
    def __init__(self, catalog: Catalog, fetcher: Fetcher, provisioner: Provisioner, target_id: str, transport: Transport, *,
                 context: int = 2048, port: int = SERVER_PORT) -> None:
        self.cat, self.fetcher, self.prov, self.tid, self.transport = catalog, fetcher, provisioner, target_id, transport
        self.context, self.port = context, port

    def _entry(self, entry_id: str):
        e = self.cat.get(entry_id)
        if e is None:
            raise KeyError(f"{entry_id!r} is not an entry of the catalog")
        return e

    def prepare(self, entry_id: str) -> int:
        e = self._entry(entry_id)
        got = 0
        for art_id, size in ((e.runtime.id, e.runtime.bytes), (e.model.id, e.model.bytes)):
            if self.fetcher.cached(art_id) is None:
                self.fetcher.fetch(art_id)
                got += size
        return got

    def deploy_and_verify(self, entry_id: str) -> ModelVerification:
        e = self._entry(entry_id)
        arts = self.cat.artifacts()
        pf = self.prov.check(self.tid, self.transport)
        caps = self.prov.caps_for(pf, self.prov.config.get(self.tid), self.context, self.port)
        plan = DeployPlan(runtime=arts[e.runtime.id], runtime_path=self.fetcher.cached(e.runtime.id),
                          model=arts[e.model.id], model_path=self.fetcher.cached(e.model.id),
                          kind=e.runtime.source, binary_name=e.runtime.server_binary, **caps)
        res = self.prov.deploy(self.tid, self.transport, plan)
        if not res.ok:
            raise DeployFailed(res.failure)
        return self.prov.verify(self.tid, self.transport, res, ExpectedModel(e.id, e.model.file))

    def undo(self) -> tuple[bool, str]:
        return self.prov.undo(self.tid, self.transport, self.port)
