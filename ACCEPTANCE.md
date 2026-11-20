# ACCEPTANCE.md — MAS portable device

Criteria per phase. The machine-readable source is `device/acceptance/criteria.yaml` (each criterion → its tests → the files its PASS is bound to).
Everything starts **FAIL**. Only `scripts/device_accept.py run` moves a criterion to PASS, and only when every listed test ran and passed:
a skip, an error, a missing test file or zero collected tests is FAIL. A PASS turns **STALE** as soon as a covered file changes.

One command per check: `python scripts/device_accept.py run --phase N` (or `--id ID`); `python scripts/device_accept.py status` for the whole table.

## Phase gates (what the tests must prove)

**Phase 1.** The oracle is under test itself (skip/missing/zero → FAIL; stale detection; hand-edit detection). DECISIONS.md carries the
architecture, threat model, guardrail spec, PCB spec and the advisor's advice. One unit (signed approvals) went through the whole cycle:
red test → build → oracle PASS → evaluator report with VERDICT: PASS → commit; the history shows the red commit before the green one.

**Phase 2.** Gate: a write runs only with a valid token for the recomputed action and only after its undo is recorded; a forged undo entry
is refused. Log: hash chain detects edit/truncate/reorder; the model's process has no handle to it. Guardrail battery: every rule of MISSION 4.1
has scripted attacks, each with a positive control (a deliberately weakened build must fail the battery). Saboteur patches (skip signature
check, skip nonce deletion, skip pre-state check) must each turn the suite red. Portable: one command from a read-only medium in a clean
container/VM; the profiler picks compute mode. Zero trace (DECISIONS D-010): three-arm snapshot diff (blank-stick control, session, session
plus approved writes); device minus control within the declared allowlist; approved arm minus session arm equals the approved set, and
equals the session arm again after undo. Fleet: pairing + per-write approval + setup + undo across ≥2 isolated containers/VMs with no
packets to non-peers.

**Phase 3.** Precision and recall against planted fixtures, thresholds fixed in `criteria.yaml` before the run. Block-device / tree hash
identical before and after; read-only mount; no network-scanning skill is present in the device registry or invokable.

**Phase 4.** Identical framed transcripts on host build, QEMU (mps2-an505) and Renode. Rejected: replay, every single-bit flip, expired,
wrong session, no press, press before request, press after window. Power-cut sweep at every flash write never reuses a counter. Fuzzing
never yields a signature without a press. The opcode set equals the allowlist.

**Phase 5.** ERC/DRC exit 0 with zero exclusions and zero unrouted; every fabrication file stamped `NO FABRICAR sin revisión humana`;
BOM resolved against a dated snapshot; `HUMAN_REVIEW_PCB.md` holds the review list.

**Phase 6.** `make paper` (or the documented one command) regenerates the PDF from raw results; no hand-typed number in results sections;
k consecutive passing runs of each eval; limitations section lists what was only emulated or not tested.

## Status (generated — do not edit)

<!-- BEGIN GENERATED: device_accept.py -->
| ID | Phase | Status | Label | Criterion | Reason |
|---|---|---|---|---|---|
| P1-HARNESS | 1 | **PASS** | REAL | The acceptance checker treats skip/missing as FAIL and detects stale PASS; continuity files exist | 42 passed |
| P1-DOCS | 1 | **PASS** | REAL | DECISIONS.md has architecture, threat model, guardrail spec, PCB spec and recorded advisor/reviewer advice | 18 passed |
| P1-UNIT | 1 | **PASS** | SIMULATED | First unit through the full cycle: signed approval tokens (guardrail 3) with an evaluator report | 179 passed, 4 saboteurs killed |
| P2-GATE | 2 | **PASS** | SIMULATED | Policy gate runs writes only with a valid single-use token bound to the exact action; undo recorded first | 149 passed, 2 saboteurs killed |
| P2-LOG | 2 | **PASS** | SIMULATED | Append-only hash-chained log; any edit, rewrite, truncation or reorder is detected against a known head; a broken log is never extended (process separation from the model is P2-GUARDRAILS) | 44 passed, 2 saboteurs killed |
| P2-GUARDRAILS | 2 | **PASS** | SIMULATED | Scripted guardrail battery (all ten rules of MISSION 4.1) gives zero violations | 36 passed, 6 saboteurs killed |
| P2-PORTABLE | 2 | **FAIL** | EMULATED | One-command launch from a read-only medium in a clean VM/container; profiler picks compute mode | missing test files: device/tests/test_portable.py |
| P2-ZEROTRACE | 2 | **FAIL** | EMULATED | Nothing installed after unplug (no service, driver, autostart, PATH change or file outside the declared temp dir); OS residue only from the enumerated allowlist (D-010 three-arm diff) | missing test files: device/tests/test_zerotrace.py |
| P2-FLEET | 2 | **FAIL** | EMULATED | Pairing, per-write approval, auto-setup and undo between two or more isolated VMs/containers | missing test files: device/tests/test_fleet.py |
| P3-INSPECT | 3 | **FAIL** | SIMULATED | Inventory precision and recall against fixtures with known truth meet thresholds | missing test files: device/tests/test_inspect_accuracy.py |
| P3-READONLY | 3 | **FAIL** | EMULATED | Inspection makes zero writes (diff and read-only mount) and never touches the network | missing test files: device/tests/test_inspect_readonly.py |
| P4-FW-HOST | 4 | **PASS** | SIMULATED | Firmware core (host build, HAL mocks) rejects replayed, altered and expired approvals; signs only after a press | 328 passed |
| P4-FW-EMU | 4 | **PASS** | EMULATED | Same firmware runs in QEMU (mps2-an505, Cortex-M33) with transcripts identical to the host build; button is the only approval path | 27 passed |
| P4-FW-RENODE | 4 | **PASS** | EMULATED | Same firmware runs in Renode with transcripts identical to the host build and QEMU | 4 passed |
| P5-PCB | 5 | **PASS** | DESIGN-ONLY | ERC, DRC and fabrication checks green; fab outputs marked NO FABRICAR; HUMAN_REVIEW_PCB.md present | 53 passed |
| P6-EVALS | 6 | **FAIL** | REAL | Evals reproducible with one command; every number in the paper comes from a script over raw results | missing test files: device/tests/test_evals_paper.py |
| P6-REPO | 6 | **PASS** | REAL | README, THIRD_PARTY.md and reproduction instructions complete | 8 passed, 1 saboteurs killed |
| P0-SECRETS | 1 | **PASS** | REAL | No credentials, keys or tokens anywhere in the repository (secret scan over the whole tree and history) | 17 passed |
| P2-MODEL | 2 | **FAIL** | EMULATED | The local model answers from the read-only medium with nothing installed on the host | missing test files: device/tests/test_model_portable.py |
| P2-MEMORY | 2 | **FAIL** | SIMULATED | Portable memory in an open format round-trips and is encrypted at rest | 0 failed, 1 errors |
| P2-PAIRING | 2 | **PASS** | SIMULATED | The gate accepts only the paired key_id; MAS chat_choice approvals are absent from the device build; the host owner's launch is the session consent (D-021) | 12 passed, 4 saboteurs killed |
| P3-REPORT | 3 | **FAIL** | SIMULATED | The inspection report is readable by the owner and consumable by the agent (schema-checked) | missing test files: device/tests/test_inspect_report.py |
| P3-NOCREDS | 3 | **FAIL** | SIMULATED | Inspection never collects credentials; planted secrets never reach the report or the model context | missing test files: device/tests/test_inspect_nocreds.py |
| P4-SECBOOT | 4 | **PASS** | DESIGN-ONLY | Secure boot and lock design documented (RDP, TrustZone option bytes, key identity) and checked against the firmware's assumptions | 9 passed, 2 saboteurs killed |
| P5-ENCLOSURE | 5 | **PASS** | DESIGN-ONLY | Parametric enclosure fits the board (clearances, USB-C opening, button access) by automatic check | 16 passed, 2 saboteurs killed |
| P6-PAPER | 6 | **FAIL** | REAL | Paper regenerates from raw results (LaTeX source, authors, arXiv variant with methods statement, manual-README baseline, error bars, outside judge, limitations) | missing test files: device/tests/test_paper.py |
<!-- END GENERATED: device_accept.py -->
