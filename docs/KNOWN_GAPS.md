# Known gaps (recorded for after the experiment; none of these is built yet)

Each item is a limit of the current release, said plainly. They are not promises.

## Machines and pairing
- After `mas join` pairs, the node service is not installed in the same step: the terminal tells the owner to run `mas node run` by hand. It should offer to install the service right there.
- `Pairing code shown on the coordinator:` prints an empty value on the node. It should say "look at the coordinator's screen".
- Goal: the new machine's terminal only announces itself, and pairing is driven from the chat (the code is shown on the node and typed into a native dialog on the Mac).
- The coordinator is bound to a fixed IP in `coordinator.json`; if the router hands the Mac another address (DHCP) the service stops until the setting is changed.
- If a node disconnects, a server it was running keeps running, and an undo waits for the node to come back.

## What the owner sees
- Live progress as a checklist (step, bytes, percent, time left). It needs byte progress from the node, which is a node change.

## Packaging and tests
- `pytest` is a runtime dependency of the package; it should be a development-only extra.
- `tests/test_tools.py::test_registry_adapter_matches...` reads live hardware (`hw.display_present`) twice and is flaky.
- `config/targets.example.yaml` still carries old fixture addresses; they should be documentation ranges.

## Approvals
Decided 2026-11-01 and built in node-preview-9: the native dialog is ONLY for pairing a computer and for storing or removing a key (`access_add`, `access_remove`); a task's route, an admission exam, a board decision, a draft, an unpair, a connection and a setup or its undo are ONE chat question bound to the plan hash (`approval-policy.json`). The question is relayed by the chat model, so its assurance is weaker than a system dialog; the record says `chat_choice` and that a model relayed it.

## What the chat does on its own (said plainly, README text later)
- A chat in the cloud can answer by itself instead of calling `mas_create_task`. The harness governs only what it is sent; it cannot make a chat use it.
- The chat model re-types the text of a task and can alter it (a figure changed in transit was seen in a real test). For a serious input, use a file reference with its hash: for a folder of one of the owner's computers `mas_create_task` takes `inputs`, and the harness reads it itself; a general file reference with a hash echoed back is NOT built yet.
- What the chat says about what fits a computer is its own claim unless it quotes the harness (the analysis lines and the options list). The harness prints the analysis' own figure in `mas_propose`; it cannot stop the chat saying otherwise.

## Models
- The options list orders by cost class, measurement and written order, then by the owner's preferred publisher (profile `preferred_publisher`, default Meta's models for this release), then by size. The five newest local models and a full re-ordering of the catalog are NOT in this release.

## The flagship flow (node-preview-9)
- When no usable analysis of the computer is stored, or it is older than 7 days (data: `analysis_max_age_s` in `skills/harness-chains/references/chains.json`), the chain reads the computer itself (an `analyze` step, started by the harness and recorded as its own, not as the owner's press) and goes on. If that read fails it stops and says why.
- The setup run and the measurement the chain proposes are the existing ones; a real install of a model on the Lenovo through the chain is UNTESTED here (the owner runs it).
- Only the model's answer comes back to the chat, with every name of the listing replaced by `[name withheld]`; a name the model spells differently (a typo, a translation) is not masked.
- The folder listing is capped at 500 entries and never recursive.

## First-run menu
- The first-run `mas` menu (worktree `../env-recognition-menu`, branch `phase5-menu`) is paused.


## Recommender v2 and v3 (2026-11-03)
**What is built, SIMULATED-LOCAL unless a line says REAL:** model slots with a guarded cloud second opinion through the node (`slots.json`); research checks by code with typed rejections; task type checked by independent picks and one owner question when they do not settle; the first-run question on subscriptions and the clearance offer; public evidence from the Hub, BFCL and LiveCodeBench by one extractor; installing a tool from GitHub release assets and a skill pack through a plan (REAL download, SHA-256 and unpack of the three shipped tools on a Mac, in a throwaway folder); domain tags from each entry's own source; PyPI and npm read from real payloads; the models an API lists read through the node; Llama 3.2 3B and Gemma 4 E4B through the generator and verifier; the flagship chain starting the analysis itself; the cloud measurement end to end against a mock.

**Not done, and why (each with its capture or its external fact):**
- The cloud conditions of the measurement and the live list of the Meta API models need the Lenovo online with the key; nothing in this tree touches it.
- SWE-rebench has no plain scores: the Hub dataset holds the benchmark's tasks (parquet), the results table is drawn by a 7.8 MB script bundle (captures in `evidence_captures/`, hashes in `evidence.json` coverage).
- LiveCodeBench is read (sha256 `17f9a1d6…`, 28 models) and holds no model of the catalog.
- No Meta API model has a public Hub id in the sources read: the Hub's Muse model is a different, open-weights model and only has its own card (vendor-reported, dropped).
- The Nemotron 3.5 Bolt E4B and LFM3 1.7B repositories stay out: their licenses (openmdw-1.1, lfm1.0) are not in the table of known licenses (`catalog.excluded.md`).
- A model used only to build or test this project verifies (8.95B, header and build support) but is NOT in the shipped catalog: it is a model used only to build or test this project, so it waits for the owner's decision (kept locally).
- A skill pack is installed (its files placed in a folder of its own) but no environment run mounts it yet.
- Muse Code stays a hypothesis: no GitHub release with a permissive license was found.

**For the conductor (what to run on the Mac and the Lenovo, in order):**
1. Update the Lenovo's node (the node code changed: `verify_program`, the finder of self-installed tools, `model_list`, the recipe file).
2. Answer the cloud-use question (`mas_profile ask field=cloud_spend`; "allowed" or "only if local cannot") so the harness may ask the Meta API.
3. `mas_refresh_models environment=meta-model-api` (REAL list through the Lenovo; read the job; the dated list is in the state folder `model_lists/`). Check that the Muse Wonder family appears by its id from the list and the router offers it.
4. Analyse the Lenovo, then `mas_install_tool environment=opencode`, answer the plan, `mas_job`; `mas_run_admission environment=opencode task_type=text access=opencode:local`; then `action=undo`.
5. `mas_measure_recommender` (owner answers the one question showing the call cap); read the job and `<state>/measurements/recommender-cloud-*.json`.
