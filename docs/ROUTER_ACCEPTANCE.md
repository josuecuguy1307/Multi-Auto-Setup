# Router acceptance examples (phase4d-wiring)

**FROZEN before any code (2026-10-31).** These examples are never edited to make them pass. The file's SHA-256 is recorded in `docs/ROUTER_ACCEPTANCE.sha256` and a test fails if the file changes. A change the owner approves goes in `docs/ROUTER_ACCEPTANCE_AMENDMENTS.md` (dated, with the owner's words), never here.

What is checked is the route's **reasoning**, not a fixed destination: candidates, then which filter cut what and why (typed reason), then what the written rule chose and on which step. Where a destination is named, it is what the reasoning yields from the stated facts. Change the facts and the reasoning changes with them (the variants).

## 0. How every example is run and judged

Labels: every result is REAL, SIMULATED-LOCAL, LINUX-CONTAINER or UNTESTED. STOP 2 runs are SIMULATED-LOCAL (fake model servers and fake provider answers, real router, board, admission, guards, coordinator and MCP tools, on this Mac). No real Lenovo run until the owner says so.

Entry: through the MCP tools of the Harness App (create task, explain route, run admission, board, draft, add/remove access, secret entry). Not by calling Python from a test script. The test drives the MCP server over its stdio protocol as a host would.

Reading the reasoning: `explain route` returns, for the task, `candidates` (environment, model, access, machine), `left_out` (candidate to typed reason), then the rule trace steps `sensitivity`, `exclusion`, `admission_filter`, `cost_class`, `admission_rate`, `time`, `load`, `tie_break`, the evidence label (measured n/10 or provisional) and the escalation step. Each example lists what that output must show.

### 0.1 The fixed world (unless an example says otherwise)

| Machine | Role | Facts |
|---|---|---|
| MAC | coordinator, grades | Docker sandbox ready. No model server. Not a place environments run. |
| LEN | paired node, Windows | No sandbox backend. Online unless said. |

| Id | Environment / access | Subtype | Cost class | Leaves the network? | On |
|---|---|---|---|---|---|
| E1 | llama.cpp loopback server, Llama 3.1 8B Instruct Q4_K_M, access `local-hardware`, configured context 8192 tokens | local_model | local | no | LEN |
| E2 | Meta Model API, model `muse-spark-1.3`, access `meta-model-api:api-key` (needs `MODEL_API_KEY`), base `https://api.meta.ai/v1` | provider | api | yes | LEN (the key is stored there) |
| E3a | OpenCode with the loopback llama.cpp backend, access `opencode:local` | own_harness | local | no | LEN |
| E3b | OpenCode with the Meta Model API, access `opencode:meta-api` (needs `MODEL_API_KEY`) | own_harness | api | **yes, because of the access, although OpenCode is an environment that can also be local** | LEN |

Owner policy `P-open` (the draft's output): subtypes `local_model`, `own_harness`, `provider` open; `approved_providers_for_internal` empty; `automated_use_cleared` not needed (no subscription CLI in the world). Policy value `unmeasured` = `measured_first`. Rule data: `policy.json` as committed at the start of this branch except the new `unmeasured` value.

Admission facts are stated per example as `adm(Ex, type) = p/10` (fresh, code-graded) or `none`. A fact is fresh when its identity (model hash, environment version, suite hash) is current. "Stale" means measured against another identity: it counts as `none`.

### 0.2 Invariants that hold in every example (checked on every run's event log)

| # | Invariant |
|---|---|
| I1 | No secret value anywhere: no event, log, tool result, card or error holds the sentinel key value planted in the fixture credential store (a string `LLM_0_SENTINEL_...`); the model-side views only ever say "access present" or "access missing: NAME". |
| I2 | A confidential task (its own level, or inherited from an input) is never dispatched to, nor does a candidate list keep, an environment/access that leaves the network. This holds for every escalation step. |
| I3 | `entry_chat` kind comes from the channel adapter's configuration (Claude Desktop MCP = cloud, Open WebUI pipe = local, owner panel = local). No tool argument sets or changes it. |
| I4 | Escalation never goes past 2 steps; a route already tried is never offered again for that task. |
| I5 | The route is chosen by the written rule (actor ROUTER); refusals, dispatches, grades, notices are GUARD; owner choices and approvals are OWNER and exist only with a human gesture. CLAUDE-CODE = 0 for decisions. A count per actor is part of the report. |
| I6 | Model-written code runs only inside a sandbox backend: never on LEN (it has none). |
| I7 | Every owner-facing string, including each tool result's `text_fallback`, is English and stands alone with no card. |
| I8 | No code path in the diff names an example, a task text, a model name, a machine or a vendor outside data, skills and the per-OS layer (reviewed with the harness-builder self-review on every diff, plus a grep on the diff for the ids and names of this document). |

## 1. Router and board examples (the owner's nine)

### Example 1: "Summarize this article in 5 bullets" (public, task type `summarize`)

Fixture: a 900-word public article. Check: the reply has exactly 5 bullet lines and holds the 5 key figures of the fixture (code check, no model grades).
Reasoning to verify (note: the owner wrote `text-v1` when only `text` existed; the type is now `summarize` and its suite is the 10-task `summarize-v1`):
- Candidates: E1, E2, E3a, E3b, all on LEN; each with its access; none left out.
- Sensitivity step: public, nothing cut.
- Variants, each its own run:
  1. `adm(E1, summarize) = 9/10`, E2 `none`: admission filter keeps E1; cost class `local` before `api`; **E1 chosen**; the reason text shows the cost class and "measured 9/10".
  2. `adm(E1, summarize) = 6/10` (fresh, code-graded): the admission filter **drops E1** as `not_admitted`, with the number; with E2 `none` and E3b `none`, the rule picks the cheapest unmeasured by cost class (`api`), labeled **provisional**; the reason shows the cost class `api`. **E2 vs E3b** is decided by later rule steps and the tie-break, shown.
  3. `adm(E1) = none`, `adm(E2) = 8/10`: **E2 chosen** (a measured-admitted candidate beats an unmeasured one, although E1 is cheaper), evidence "measured 8/10".
  4. `adm(E1) = none`, `adm(E2) = none`: cheapest unmeasured (E1, or E3a: tie decided by the later steps), labeled **provisional**.
- Every variant: the explain output states the cost class of the chosen route and of each candidate.

### Example 2: "Add a --dry-run flag to this CLI and make test_cli.py pass" (public, `code`)

Fixture: a 120-line argparse CLI `tool.py`, `test_cli.py` with 6 tests (one new test the fix must satisfy, `test_dry_run_changes_nothing`).
Check: after the patch comes back, `test_cli.py` passes (old and new tests) when run **in the Mac sandbox**.
Reasoning to verify:
- Candidates include E1, E3a, E3b, E2; `left_out` has none of these for the sandbox reason, because none of them executes model code (E1 and E2 return text; E3a/E3b run OpenCode with bash denied).
- An environment whose mode needs a sandbox (confinement `unverified` or `sandbox_required`) on LEN is left out `sandbox_missing` (put Pi in the world for this check: Pi@LEN `sandbox_missing`).
- The patch is graded by `run_tests` in the Mac sandbox: the grade event is on MAC, actor GUARD, and the event log has **no** sandbox-requiring run, and no execution of model-written code, on LEN (I6).
- The chosen route follows the written rule from the stated admission facts (state them in the run).

### Example 3: "Summarize these client payment notes" (confidential, fake data, from the panel or a local chat; `summarize`)

Fixture: 12 fake payment notes (invented names, invented amounts). `adm(E1, summarize) = 9/10`, `adm(E3a, summarize) = none`.
Reasoning to verify:
- Entry chat local (panel / Open WebUI pipe): accepted. The same task from the Claude Desktop MCP channel: refused `confidential_from_cloud_chat` (I3), nothing else runs.
- Sensitivity step: E2 and E3b are cut with reason `cloud` (E3b because of its access, not its environment); E3a is cut `provisional` (unmeasured, R3).
- **E1 only** survives; the rule shows one candidate.
- Variant: `adm(E1) = none` ⇒ typed refusal `provisional_only`, naming E1; nothing routed.

### Example 4: Input longer than Llama's configured context (public, `summarize`)

Fixture: a public text of 12,000 tokens (context of E1 is 8192). Check: reply contains the 5 key figures.
Facts: `adm(E1) = 9/10`, `adm(E2) = 8/10`.
Reasoning to verify:
- Step 0: E1 is chosen (local). The simulated server answers with its natural context-overflow error; the dispatch result is `failed` with the node's typed reason; the code grade fails (no figures). No pre-emptive check by the router.
- Step 1: escalation event (ROUTER) from E1 to E2 (E1 is "already tried" and gone from the list); E2's reply is correct ⇒ the task closes.
- Variant: E2 also fails the grade ⇒ the next decision refuses `escalation_exhausted` (2 steps taken), and an **owner notice** (GUARD) says so, in English, without task content for a cloud chat. No third attempt.

### Example 5: The Meta access is removed through the chat, with approval

Setup: the credential for `MODEL_API_KEY` is stored on LEN (fixture store with the sentinel value). Via the MCP tool "remove access", the harness shows a plan (what is removed, how to undo it), the owner approves with a human gesture (OS dialog), and the plan runs.
Reasoning to verify:
- Before: E2 and E3b are candidates. After: both are in `left_out` with reason text **"access missing: MODEL_API_KEY"** (typed code `no_access`, the variable named, from data).
- The plan, its events and the result never contain the value (I1; the sentinel is searched in every artifact). The model-side view says "access present" before and "access missing" after.
- Without the OS approval, nothing is removed (a bare model call changes nothing, GUARD refusal `no_owner_approval`).
- Undo (the plan's own undo) restores presence without the value ever passing through the harness.

### Example 6: The local server is stopped through the chat

Setup: the owner stops the E1 server on LEN through the chat (an approved write plan, as in setup). Facts: `adm(E1) = 9/10`, `adm(E2) = 8/10`.
Reasoning to verify:
- Public task: E1 and E3a are in `left_out` with a typed reason that does not claim anything false (a stopped server is not "not installed"); **only E2** (and E3b) remain; E2 chosen.
- Confidential task (fake data, local chat): E2/E3b cut `cloud`; no local candidate exists ⇒ typed refusal `no_local_candidate`; nothing is sent anywhere.

### Example 7: "Transcribe this audio file"

Fixture: a small audio file reference. No environment in the catalog declares the task type.
Reasoning to verify: typed refusal `no_candidate`; `candidates` is empty (zero); the details say no environment declares this task type; nothing is invented (no route, no "closest" type, no model asked).

### Example 8: LEN is offline

Setup: LEN's session is down. All environments live on LEN.
Reasoning to verify: every candidate is left out with the typed reason `machine_offline`, and the task is refused with the typed refusal text **"machine offline"**, English. No silent retry: exactly one refusal event, no dispatch event, no later attempt unless the owner creates the task again. When LEN comes back, a new task routes normally.

### Example 9: Board: "Build a small CLI that renames photos by date, with tests" (public)

Fixture: a folder of 8 fake photo files with EXIF dates. Director = the board's director menu (closed). Check: the final tests pass in the Mac sandbox.
Reasoning to verify:
- The director posts subtasks (design, code, tests, review) as a typed plan; the owner approves the plan **once** (human gesture, OWNER event bound to the plan hash); a subtask not in the approved plan is refused.
- Each subtask is routed by the router (ROUTER decision per subtask, with its own trace); the results are code-checked.
- Verification is on **another environment or machine** than the producer: the board's exclude filter removes the producer's environment/machine for the verification subtask (shown in the trace as the `exclusion` step); tests run on MAC.
- After **2 failed** attempts of one subtask the board raises the escalation warning to the owner (GUARD); no third escalation step.
- A write to the workspace without the approved plan is blocked by the GUARD with a typed refusal (negative run in the same example).

## 2. The fourteen tasks

All run SIMULATED-LOCAL through the MCP tools. For each: fixture (generated by `scripts/make_acceptance_fixtures.py` with a fixed seed into `tests/fixtures/acceptance/<id>/`; confidential fixtures are invented data), the code check, sensitivity, task type, and the route reasoning to verify. Expected values are computed by independent code in the generator (never by a model), and the fixture hashes are recorded before the first run. In all of them: invariants I1 to I8; the stated admission facts are set before the run, `adm(Ex, type)` per the example.

Unless stated, the world is 0.1 and the admission facts are `adm(E1, T) = 9/10`, `adm(E3a, T) = 8/10`, `adm(E2, T) = 8/10`, `adm(E3b, T) = none`, with `T` the task's type. The variants change them.

### Finance

**F1. Bank reconciliation** (confidential, `numeric-analysis`). Fixture: `bank.csv` and `ledger.csv`, 40 rows each, invented, with 5 planted differences (a missing deposit, an amount typo, a duplicate, a timing item, a fee). Check: the reply's JSON list of differences equals the fixture's list (`json`, canonical). Reasoning: E2/E3b cut `cloud`; E3a candidate; the rule shows E1 vs E3a under the same cost class by admission rate then the later steps. Variant: all local candidates unmeasured ⇒ `provisional_only`.

**F2. 12-month cash flow** (internal, `numeric-analysis`). Fixture: 12 monthly inflow/outflow pairs. Check: JSON with monthly net totals and the list of negative months equals the fixture's. Reasoning: internal ⇒ local allowed; E2/E3b cut `provider_not_approved` (policy default). Variant: the owner approves provider E2 for internal ⇒ E2 is a candidate again but E1 still wins on cost class; E3b stays cut (its environment is not the approved provider).

**F3. Journal entries** (confidential, `numeric-analysis`). Fixture: 20 invented transactions and a chart of 14 accounts; each transaction maps to exactly one entry by the chart's rules. Check: (a) the reply's entries equal the expected entries (canonical JSON `equals`), and (b) an independent verifier re-checks debit = credit in every entry and that every account is in the chart. Reasoning as F1.

**F4. Loan amortization** (public, `numeric-analysis`). Fixture: principal 10,000; 12% per year (1% per month); 24 months; French method. Check: payment equals 470.73 (±0.01) and the final balance is 0 within cents (`json` numeric within tolerance, or `answer_line`). Reasoning: public; cheapest by cost class from the stated facts. Variant: `adm(E1)=6/10` ⇒ E1 dropped `not_admitted`.

**F5. OLS** (public, `numeric-analysis`). Fixture: 60 rows of invented sales, price and ads (seeded noise). Expected coefficients, R² and the price p-value come from `statsmodels` run offline in the generator. Check: the model-written code is run **only in the Mac sandbox** (`run_tests`), and its printed coefficients, R² match within 1e-6 and the "price significant at 5%" flag matches. Reasoning: the grade event is on MAC; no execution on LEN (I6); the route follows the rule.

**F6. Month-end close board** (confidential). Director plan: reconcile (F1 fixture), journal entries (F3 fixture), cash-flow summary (F2 fixture), one-page report. One owner approval of the plan. Reasoning: every subtask is `confidential`, so every subtask's trace shows E2/E3b cut `cloud` at every escalation step; the report subtask **inherits** the highest level of its inputs (`result-of` inputs); each subtask is code-checked; the verification subtasks use `exclude` (other environment, or MAC grading). Check: every number in the one-page report appears in a verified subtask output and the report's totals equal the fixture's expected totals (regex/`contains` checks per figure, code).

### Code

**C7. Leap-year bug** (public, `code`). Fixture: `dates.py` with `is_leap` using only `% 4`, and `test_dates.py` (7 tests, 2 failing). Check: `test_dates.py` passes in the Mac sandbox. Reasoning: as Example 2.

**C8. `--format csv`** (public, `code`). Fixture: a CLI printing JSON, 5 passing tests. Check: the 5 old tests and the 3 new tests (written by the generator, hidden from the model) pass in the Mac sandbox. Reasoning: as Example 2; the new tests are hidden checks, not part of the prompt.

**C9. Split a 600-line module in 3** (public, `code`). Fixture: `inventory.py` of 600 lines and a 25-test suite. Check: the suite passes unchanged against the three new modules plus a re-export shim, in the Mac sandbox. Reasoning: step 0 E1; with E1's output failing the check, escalation event to the next by the rule (E3a, E2, E3b by the facts); at most 2 steps; a third failure ⇒ `escalation_exhausted` and the owner notice.

**C10. Confidential script double-counting invoices** (confidential, `code`). Fixture: `invoices.py` with a bug counting an invoice twice when it appears in two files, invented data, and 6 tests. Add Pi to the world (mode `sandbox_required`). Reasoning to verify: Pi@LEN is left out `sandbox_missing` (LEN has no sandbox); Pi@MAC is left out `no_access` (no model server there); E2/E3b cut `cloud`; the reasoning states **whether a valid local route exists**: if a measured local candidate (E1 or E3a) exists it is routed (they return a patch and run no code on LEN) and the tests run on MAC, which is the owner's paired machine, in its sandbox; if none is measured, the typed refusal is `provisional_only`; if none exists, `no_local_candidate`. The explain output must say which of these it is and why. Never a cloud route.

### Data and documents

**D11. Extraction** (internal, `extraction`). Fixture: 15 invented invoice texts in different layouts. Check: JSON array, schema {supplier, date (ISO), total, tax}, values equal the expected ones (`json`). Reasoning: internal ⇒ local allowed, cloud cut `provider_not_approved`.

**D12. Cleaning** (public, `data-cleaning`). Fixture: a messy CSV (40 rows: mixed date formats, stray spaces, 6 duplicates, mixed case). Check: the reply's CSV lines equal the expected rows (`lines`), zero duplicates, all dates ISO. Reasoning: public; the rule from the facts.

**D13. Long summary** (public, `summarize`). Fixture: a 30-page invented public report (about 14,000 tokens) with 5 key figures. Check: a brief of at most 350 words containing the 5 exact figures. Reasoning: as Example 4: E1 fails naturally on context, escalation to E2, the check passes; E1 is not offered again.

### Multi-machine

**M14. Compare 3 local models** (internal, `text`). World: LEN has three local models served one at a time (A, B, C: three distinct model ids and file hashes) and `text-v1` (10 tasks). Fake servers return scripted answers of different accuracy. Check: admission runs executed on the node for each model (real exam code, code-graded) write admission facts; the director merges them into a comparison; the recommendation names the model with the highest measured pass rate, ties by measured time, and **uses no number that is not a measured admission fact** (a model with no fact is listed "not measured", never ranked). Reasoning: the explain output for a following `text` task shows the chosen model's evidence "measured n/10 here".

## 3. Generality check (held out, after the examples pass)

Done as DATA only, with the diff showing no file under `src/` changed:
1. Add one environment that is not in this document (another CLI with a documented non-interactive mode; for example a second OpenCode-like harness) as a catalog row with its fixtures. Run Examples 1, 3, 5 and 6 again: it appears in `candidates` or `left_out` with the correct typed reason, the sensitivity result is unchanged, and Example 3 still cuts it if it leaves the network.
2. Add one access to an existing environment as data (a second API key name). Run Example 5 again with it removed.
3. Add one task type that is not in this document, as a suite plus a catalog `task_types` entry only (name chosen at STOP 2; default `classification`). Route one task of it.
If any of the three needs a code edit, that is a harness failure: it is reported with the file and the layer it should have lived in, and not patched per case.

## 4. What the STOP 2 report holds, per example

The recorded route reasoning (the explain output); pass/fail; the layer of each fix (skill, data, view, protocol, tool, guard, bug); the held-out result; the actor count; the labels; "harness check: passed / why".
