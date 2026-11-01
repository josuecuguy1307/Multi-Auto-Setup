"""The seams between admission (this package) and the router (`envrec_coordinator.router`), as code that either side can be built with. Nothing here decides anything: it reads admission facts for the
router (`RouterAdmissionFacts`, ROUTER_API section 4), turns the sandbox entry into the router's `run_tests` runner (`sandbox_runner`, section 10), and maps an option to its admission view for the
options list (`option_lookup`)."""

from __future__ import annotations

import hashlib
from collections.abc import Callable

from envrec.skills import sbx

from envrec_coordinator.admission.facts import FACT_EVENT, AdmissionFacts, AdmissionView, Combo, identity_of
from envrec_coordinator.admission.graders import extract_code
from envrec_coordinator.admission.suite import Suite, load_suite
from envrec_coordinator.router.facts import DictFacts, MachineFacts
from envrec_coordinator.router.grading import SandboxGone

Current = Callable[[str], tuple[str | None, str | None]]  # route key -> (sha256 of the model file now, version of the environment now); (None, None) when unknown
RUN_TESTS_TIMEOUT_S = 10.0


def sandbox_ready() -> bool:
    """True only when a sandbox backend can start now. Same answer the admission exam uses, so the router and the exam never disagree."""
    return sbx.docker_ready()


def sandbox_runner(rec, spec: dict) -> dict:
    """The router's `run_tests` runner: the stored output is the solution, `spec["value"]` the hidden checks, `spec["n_checks"]` how many must finish. Model-written code runs only through
    `sbx.run_exam_code`, which refuses before starting anything when no backend is ready."""
    code = extract_code(rec.text)
    n = spec.get("n_checks")
    if code is None or not isinstance(n, int) or n < 1:
        return {"passed": False, "log_sha256": hashlib.sha256(b"no_runnable_input").hexdigest()}
    try:
        res = sbx.run_exam_code(code, str(spec["value"]), n, timeout_s=float(spec.get("timeout_s", RUN_TESTS_TIMEOUT_S)))
    except sbx.SandboxUnavailable as e:
        raise SandboxGone(str(e)) from None
    log = f"{res['kind']}:{res['checked']}:{res['detail']}"
    return {"passed": bool(res["ok"]), "log_sha256": hashlib.sha256(log.encode()).hexdigest()}


class RouterAdmissionFacts(DictFacts):
    """The router's `Facts` with admission read from the coordinator's event log. `identity(route_key)` is `identity_of` computed from the model hash, environment version and suite hash NOW, so a
    fact measured against anything else is absent for the router (it is never silently reused)."""

    def __init__(self, machines: list[MachineFacts], store: AdmissionFacts, current: Current, *, suite_for: Callable[[str], Suite] = load_suite, **rest) -> None:
        super().__init__(machines, **rest)
        self.store, self.current, self.suite_for = store, current, suite_for

    def admission(self, key: str) -> dict | None:
        return self.store.current(key)

    def identity(self, route_key: str) -> dict:
        env, model, access, machine = route_key.split("|")
        tail = f".{machine}.{env}.{model}.{access}"
        for e in reversed(self.store.log.events(FACT_EVENT)):
            k = str(e.payload.get("fact", ""))
            if k.startswith("admission.") and k.endswith(tail):
                task_type = k[len("admission."):-len(tail)]
                sha, version = self.current(route_key)
                try:
                    suite = self.suite_for(task_type)
                except ValueError:
                    return {}
                return identity_of(Combo(task_type, machine, env, model, access, sha, version), suite.hash)
        return {}


def option_lookup(store: AdmissionFacts, combo_of: Callable[[dict], Combo | None], suite_for: Callable[[str], Suite | None] = load_suite, threshold: int = 8) -> Callable[[dict], AdmissionView | None]:
    """The `admission_lookup` of `build_options`: option -> its admission view from FRESH facts, so "measured here" appears only for a measurement made against the current model, environment
    and suite. `combo_of` maps an option to its router combination (None when it has none)."""
    def lookup(option: dict) -> AdmissionView | None:
        combo = combo_of(option)
        if combo is None:
            return None
        try:
            suite = suite_for(combo.task_type)
        except ValueError:
            suite = None
        return store.view(combo, suite, threshold)
    return lookup
