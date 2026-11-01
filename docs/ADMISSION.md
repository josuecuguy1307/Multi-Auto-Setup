# Admission exam (T4c)

Implements `docs/RECOMMENDATION_SPEC.md` section 8 against `docs/ROUTER_API.md` section 4. Code: `src/envrec_coordinator/admission/`. Data: `data/admission/<suite>/suite.yaml`. Every owner-facing string: `admission/strings.py` (English).

## Suites
`code-v1` and `text-v1`: 10 project-written tasks each, graded by code only. Code tasks run model-written code in the sandbox gateway `envrec.skills.sbx.run_exam_code` (no network, read-only root, nothing mounted; the solution and the hidden checks travel on stdin). With no sandbox backend the exam aborts `sandbox_missing` before the environment is asked anything. The suite hash is the SHA-256 of the canonical JSON of the whole suite file. Every task has a `source.license`; only verified ones load. External benchmarks come later, after a code check reads their license.

## Rules in force
* Pass mark 8/10 (`threshold`, configurable, printed in every label). Stop early when the first 3 tasks fail, or (on by default, `stop_unreachable`) as soon as the mark can no longer be reached; the label says which.
* One run per task per measurement (`samples_per_task: 1`): a stochastic model's 9/10 is one measurement.
* A timeout of the environment on a task is that task's failure. An environment that cannot be asked at all (`RunnerAborted`) aborts the exam, stores no fact and does not use up the owner's approval when nothing was asked.
* Only code-graded results count and can be stored (A1). A suite with fewer code-graded tasks than the mark cannot be passed.
* Stale: the fact is valid only for the suite hash, model file hash, environment version and threshold it was measured with. Anything else shows `stale: ...` and counts as provisional. Never silently reused.
* Confidential goes only to a fresh, admitted fact (`facts.allows`); a fact below the mark drops the combination for every task.

## Owner decision
`AdmissionDesk.propose` makes a pending decision `approve_admission` with its cost (tasks, environment, model, machine, estimated time or "not estimated", quota). `answer` counts only with `by="owner"` and a gesture in `OWNER_GESTURES` (`os_dialog`, `host_dialog`, `panel`); a bare owner answer is refused (`owner_authority_refused`), the operator's is written CLAUDE-CODE and ignored. `start` looks the OWNER approval up (for that combination, suite hash and threshold; single use) before anything is asked. Progress and result leave as Harness App intents through the `outbound` callable (T4a's `filter_outbound`).

## Seams (wired at integration, `admission/router_bridge.py`)
* **Router reads:** `RouterAdmissionFacts` serves the stored fact for a key and computes `identity(route_key)` from the model hash and environment version NOW (supplied by the caller) and the suite hash of the fact's task type. A fact measured against anything else is absent for the router.
* **Runner for `run_tests`:** `sandbox_ready` / `sandbox_runner` of the router are `sbx.docker_ready` / `sbx.run_exam_code`; no sandbox gives `sandbox_missing`.
* **Exam runner:** the real `Runner` is the router's `dispatch` of an `environment_run`; tests use stand-ins (UNTESTED with a real environment until the final integrated test).
* **Options:** `option_lookup(store, combo_of)` is the `admission_lookup` of `build_options`; "measured here" appears only from fresh facts. Mapping an option to its combination is the caller's (`combo_of`).
* **Gestures:** one `OWNER_GESTURES` set, defined in `runs.py` and imported by the admission desk.

## Coverage matrix
| | macOS | Windows | Linux |
|---|---|---|---|
| Suite load, hash, text graders, exam, facts, desk (unit + conformance) | SIMULATED-LOCAL (this Mac) | UNTESTED | UNTESTED |
| Code grading in the sandbox | LINUX-CONTAINER (Docker on this Mac) | UNTESTED (sandbox undecided on Windows: exam aborts `sandbox_missing`) | UNTESTED (Docker path is the same container) |
| Real model / real environment admission | UNTESTED | UNTESTED | UNTESTED (final integrated test) |
