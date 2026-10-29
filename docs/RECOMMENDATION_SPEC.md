# Recommendation spec: catalog, evidence, admission, recommendation

Status: normative from Phase 1 on; linked from `docs/NETWORK_CONTRACT.md` (sections 9 and 10). MUST / MUST NOT / MAY in the RFC 2119 sense. **Nothing here is implemented.** Anything marked **OPEN** is undecided and MUST NOT be given a value by code or by a model; anything marked **to verify** is a claim nobody has checked yet. This document states no license, benchmark score or vendor term as fact.

The goal: on each machine, after the owner says which AI they want to use there, show a short list of working combinations of harness, model and inference mode, with honest labels for how much is known about each, and let the owner choose. After that, code (the router) picks per task among the approved combinations.

## 1. Vocabulary

- **Harness**: software that runs an agent loop with tools around a model. Any kind: coding agent, browser agent, data agent, general tool agent, our own setup harness, a commercial CLI the owner already has, or **no harness** (a direct model call).
- **Inference mode**: where the model runs: `local` (open-source weights on this machine), `api` (the owner's own API key), `subscription` (the owner's own account through the vendor's official CLI).
- **Combination**: harness x model x inference mode x machine, for a task type.
- **Task type**: a kind of work with an admission suite (section 8). The set of task types is the union of the task types claimed in manifests and the task types defined by task packs (section 9). A task type without an admission suite MUST NOT be routed.
- **Manifest**: the facts about one harness (section 3).
- **Measured here**: an admission result for the exact combination on this machine (section 8).
- **Provisional**: a recommendation that rests on public evidence and hardware fit only.

## 2. Catalog protocol (minimal version now)

2.1 **Written before searching.** The protocol is a file in the repository, committed before the first search, stating: the sources, the exact queries, the date, and the inclusion criteria. Changing it afterwards creates a new version; old results keep their version.

2.2 **Operational definition of a harness** (all must hold): it runs an agent loop with tools; it is headless (a CLI or an SDK); the model is configurable (or it is a current commercial product with a documented non-interactive mode, to verify per product); its license is known; and it has commits in the last 12 months (open source) or is a current commercial product.

2.3 **Sources of different kinds:** curated lists; the agent columns of Terminal-Bench and SWE-rebench; package registries; commercial vendor pages. A single kind of source MUST NOT be the only input.

2.4 **Extraction and de-duplication by code:** URLs normalized, renamed or moved repositories resolved, forks and mirrors merged to their upstream. A model MAY read a page and extract manifest fields (section 11), never decide inclusion.

2.5 **Published funnel:** found -> de-duplicated -> screened -> included, with the count at each step and an **exclusion reason for every excluded item**.

2.6 **Control set:** every agent listed by Terminal-Bench and by SWE-rebench MUST be recovered by the catalog procedure; the recovered percentage is reported. Missing ones are listed with the reason.

2.7 **Snapshot:** each catalog build carries its date and a hash of its content. Declared biases are written down (for example: sources are mostly in English).

2.8 **Future, not required now:** a capture-recapture estimate of coverage; two independent screeners with measured agreement.

## 3. Manifest

3.1 A harness enters the system only through a manifest. Fields:

| Field | Meaning |
|---|---|
| `task_types` | task types it claims |
| `os_support` | operating systems it claims to run on |
| `accepted_models` | models, providers and inference modes it accepts |
| `needs_sandbox`, `needs_docker`, `needs_gui` | requirements |
| `license` | SPDX id and the path of the LICENSE file read |
| `upstream` | open source: repository URL **and pinned commit**; commercial: vendor page URL |
| `automated_use` | commercial only: `verified` or `to review` (what the vendor's terms of use say about non-interactive or automated use) |

3.2 **Every field carries a label:** `verified by code` (a program derived it from a file or a command: the license text matched, the commit exists, the repository date) or `declared by the project` (taken from its README or documentation). A declared field MUST carry the **exact quote** and its **source** (URL and, for open source, commit). A field without a label MUST NOT be used by the router.

3.3 A manifest field never overrides what was measured: a harness that claims a task type and fails the admission exam is not approved for it.

3.4 **Licenses.** For anything we install, the license MUST be confirmed by code from the upstream LICENSE file at the pinned commit, and MUST be MIT or Apache-2.0. No clear license -> not included. Commercial harnesses are never installed, bundled or copied by us (contract section 9.4; section 10.4 here).

## 4. Evidence sources and tiers

Every number the system shows or uses carries **source, tier and date**.

| Tier | Sources | Use |
|---|---|---|
| 1: independent and reproducible | SWE-rebench, BFCL, LiveCodeBench | may support a decision |
| 2: independent, less reproducible | Epoch AI, Artificial Analysis, Vals AI | may support a decision, labeled as tier 2 |
| 3: useful, labeled "submitted by the team" | Terminal-Bench (it lists harness + model combinations) | may support a decision, always with that label |
| excluded from decisions | vendor-reported numbers (may be shown, marked "vendor-reported"); human-vote rankings; aggregators (used only to find the primary source) | never used to rank or filter |

4.1 **Saturated or contaminated benchmarks MUST NOT be used for decisions** (examples: HumanEval, SWE-bench Verified). The list of such benchmarks is a versioned data file with the reason for each; adding a benchmark to it is a decision of the owner of the project, not of code.

4.2 A tier is a property of the source, recorded with the source; code MUST NOT upgrade a tier.

## 5. Candidates and shortlist

5.1 **Candidates are generated by code** from: the owner's inference answer (section 7 of the contract) x the manifests x the machine (operating system, memory, which local models fit and at which quantization, sandbox availability) x the sensitivity policy. No model proposes candidates.

5.2 **Shortlist before measuring.** Measuring every candidate is not affordable, so a **written rule** limits the admission runs. Its form: at most **N combinations per task type**, each available inference mode represented at least once, chosen by best public evidence (section 6). The values (for example N = 6) are **OPEN**.

## 6. Evidence per candidate

For each candidate, the most specific evidence available is used, in this order:

1. **Measured here**: the exact combination on this machine (admission result). Always the last word.
2. **Public combination score**: the same harness + model (+ mode) on a public benchmark.
3. **Public model-only score**, labeled "model only, harness effect unknown".
4. **No data**: shown as such.

6.1 When the public number is for a different precision than the quantized local model that would run, the label MUST say so ("public score is for a different precision").

6.2 More specific evidence always replaces less specific evidence. Different levels MUST NOT be averaged together.

## 7. Two speeds

7.1 **Speed 1 (seconds): a provisional recommendation** from the owner's answer, hardware fit and public evidence, labeled "provisional, not measured here". The owner MAY choose immediately.

7.2 **Speed 2 (background): admission** runs when the machine is idle. Labels upgrade to "measured here: x/10", or the recommendation changes and the owner is notified.

7.3 A **provisional** combination MAY receive only `public` or `internal` tasks, and only with result verification. It MUST NOT receive a `confidential` task. Confidential tasks go only to combinations measured here.

## 8. Admission exam

8.1 **Pass threshold:** 8 of 10 by default, written down and configurable by the owner; the value in force is always displayed.

8.2 **Early stop:** if the combination fails the first 3 tasks, stop; it is not approved. Otherwise continue to 10. (OPEN: whether to also stop as soon as the threshold can no longer be reached.)

8.3 **Suite per task type** = a subset of open-benchmark tasks + task-pack tasks + optional owner tasks. Fresh or time-gated tasks are preferred.

8.4 **Only code-graded tasks count toward passing.** Tasks graded by humans or by a model are labeled **weaker** and MUST NOT count toward the pass threshold.

8.5 **Versioned and hashed suites.** A result is comparable only within the same suite hash. The exact tasks used are recorded by id.

8.6 **Re-run** admission when the model, the harness, the machine or the suite changes.

8.7 Admission runs inside the sandbox, respecting the task's sensitivity (a confidential suite never leaves the machine; its samples are local or synthetic).

## 9. Task packs

9.1 A **task pack** is a set of tasks for one kind of work, taken from published benchmark tasks together with their graders:

| Pack | Source of tasks | Note |
|---|---|---|
| programmer | SWE-rebench, LiveCodeBench | |
| tools / agenda | BFCL | |
| legal | LegalBench | license to verify |
| accounting / spreadsheets | SpreadsheetBench | license to verify |
| general office | GDPval public subset | demo or weak rubric only, **never counts toward passing**; license to verify |

9.2 **Every dataset's license is verified by code** from its LICENSE file or dataset card before inclusion. No clear license -> not included. The same applies to the first three rows.

9.3 Tasks translated into another language are labeled **translated**.

9.4 **Owner custom tasks** are optional. The system MAY propose them from what the owner describes; the owner approves each one. For confidential work they use synthetic or locally generated samples only.

## 10. Options shown, and the owner's choice

10.1 Options shown are the **Pareto front** over (pass rate, cost, time): a combination is left out only if another is at least as good on all three and strictly better on one. The front is usually 2-3 options. Pass rates inside the **tie margin** count as equal (margin value **OPEN**). A cap on how many options are shown is **OPEN**.

10.2 There is **no "best" label** and no ranking by a model. Each option shows its measured facts and its evidence label (section 6).

10.3 The **owner chooses**. After that, the router picks automatically per task among the approved combinations by the written selection rule in contract section 10.

10.4 **Commercial harnesses** enter only as options for what the owner already has installed and logged in with their own account; they count as cloud for sensitivity; and they are shown but never auto-routed while `automated_use` is `to review`.

## 11. What AI may and may not do here

11.1 A model MAY read a page and extract manifest fields with **exact quotes**; a clean-context verifier (a separate call) MUST check each quote against the source text before the field is kept. A model MAY suggest a task type or an owner task.

11.2 A model MUST NOT rank options, include or exclude a catalog item, set a tier, choose the route, grade a task that counts toward passing, or see a confidential task if it is a cloud model.

## 12. Conformance

Checks that exist or are reserved for this spec (see `docs/HARNESS_SPEC.md`, section NETWORK, ROUTER and ADMISSION):

- **R1** confidential tasks are never routed to cloud, commercial or third-party machines. *Placeholder until the router exists.*
- **R2** escalation never exceeds 2 steps. *Placeholder.*
- **R3** provisional combinations never receive confidential tasks. *Placeholder.*
- **A1** only code-graded tasks count toward the admission threshold. *Placeholder until admission exists.*

## 13. OPEN items

Tie margin; shortlist size and per-mode representation numbers; option display cap; whether admission also stops once the threshold is unreachable; how "measured time" and "measured cost" are normalized across machines; the exact list and versioning of saturated benchmarks; which commercial products qualify under 2.2 and what their terms say (**to verify** per product); dataset licenses for LegalBench, SpreadsheetBench and the GDPval subset (**to verify**).

## 14. Future (marked, not required)

Capture-recapture coverage estimate and two independent screeners (2.8); an optional hosted decision model for task-type suggestion (contract section 10.9).
