"""The seams between admission (this package) and the router (`envrec_coordinator.router`), as code that either side can be built with. Nothing here decides anything: it reads admission facts for the
router (`RouterAdmissionFacts`, ROUTER_API section 4), turns the sandbox entry into the router's `run_tests` runner (`sandbox_runner`, section 10), and maps an option to its admission view for the
options list (`option_lookup`)."""

from __future__ import annotations

import hashlib
import re
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


FILE_BLOCK = re.compile(r"```[ \t]*(?:python|py)?[ \t]*\r?\n#\s*file:\s*(?P<path>[A-Za-z0-9_./-]+)[ \t]*\r?\n(?P<body>.*?)```", re.S | re.I)


def extract_files(reply: str) -> dict[str, str]:
    """The files a reply returns: each is a fenced block whose first line is `# file: <relative path>`. Nothing else in the reply is code."""
    return {m.group("path"): m.group("body") for m in FILE_BLOCK.finditer(reply)}


def sandbox_runner(rec, spec: dict) -> dict:
    """The router's `run_tests` runner: the stored output is the solution, `spec["value"]` the hidden checks, `spec["n_checks"]` how many must finish. Model-written code runs only through
    `sbx.run_exam_code`, which refuses before starting anything when no backend is ready."""
    if spec.get("kind") == "project_tests":  # the files the reply returned laid over the project's own, then the project's tests, all inside the sandbox
        returned = extract_files(rec.text)
        if not returned:
            return {"passed": False, "log_sha256": hashlib.sha256(b"no_files_returned").hexdigest()}
        files = {**dict(spec["files"]), **returned}  # a returned file replaces the project's; the project's tests are laid back on top so the reply cannot weaken them
        files.update({k: v for k, v in spec["files"].items() if k in spec.get("protected", [])})
        try:
            res = sbx.run_project_tests(files, spec.get("pattern", "test_*.py"), min_tests=int(spec.get("min_tests", 1)), timeout_s=float(spec.get("timeout_s", 20.0)))
        except sbx.SandboxUnavailable as e:
            raise SandboxGone(str(e)) from None
        except sbx.SandboxRefused:
            return {"passed": False, "log_sha256": hashlib.sha256(b"file_refused").hexdigest()}
        return {"passed": bool(res["ok"]), "log_sha256": hashlib.sha256(f"{res['kind']}:{res['checked']}:{res['detail']}".encode()).hexdigest()}
    code = extract_code(rec.text)
    n = spec.get("n_checks") or str(spec.get("value", "")).count("check(")  # a check written as text counts its own `check(` calls
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

    def identity(self, route_key: str, task_type: str | None = None) -> dict:
        env, model, access, machine = route_key.split("|")
        tail = f".{machine}.{env}.{model}.{access}"
        for e in reversed(self.store.log.events(FACT_EVENT)):
            k = str(e.payload.get("fact", ""))
            if k.startswith("admission.") and k.endswith(tail) and (task_type is None or k == f"admission.{task_type}{tail}"):  # a fact is valid for ITS task type's suite: another type's newer fact says nothing about it
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
