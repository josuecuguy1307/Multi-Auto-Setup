# Project overview: envrec

A harness where an AI model works out where it runs and what is around it, and, on devices the human owns, sets up a model server and proves it works. The model never senses anything: code probes return typed, evidence-tagged facts, the model picks one action from a closed menu, and code does the rest. Say "workspace self-setup", never "self-replication".

Size: about 6,300 lines of source in 17 packages, about 4,100 lines of tests in 23 files, 430 tests (429 pass, 1 skipped by default), 16 commits.

## 1. What has been achieved

| Stage | What exists | Evidence |
|---|---|---|
| Scouting | 9 repos read, licenses and SHAs recorded, 3 wrong claims in the research notes corrected | `SCOUTING.md`, `THIRD_PARTY.md` |
| Core | 12 strict contracts, SQLite store with append-only events and hash-checked raw files, pure policy function | `contracts/`, `store/`, `policy/` |
| Models | One adapter interface for OpenAI-compatible endpoints (profiles in config) and a scripted fake. No model name in the source (a test enforces it) | `model/`, `config/models.yaml` |
| Recognition (Ring 0) | 11 read-only probes on this machine; two independent providers can agree and make a fact `verified` | `tests/`, `ground_truth/` |
| Scoring | Per-fact outcomes, false-verified rate, every report labelled PROVISIONAL until the ground truth is confirmed | `scoring/`, `ground_truth/` |
| Fingerprinting | usb.ids, IEEE OUI, Bluetooth, IANA service names, plus our own serial protocol signatures with checksum checks | `fingerprint/`, `DATA_SOURCES.md` |
| Loop | View capped by `view_budget_facts`, closed menu, one validated action per turn, policy, run, record, stop. Raw output never enters a prompt | `loop/` |
| Unknown ladder | Rungs 1, 2, 3 and 6 on replay devices (tested / honestly unknown / contradicted), then rungs 4 and 5: research, model-written probes, independent verifier, skills reused on the next encounter | `ladder/`, `tests/test_ladder.py`, `tests/test_rung45.py` |
| REA | Real tool installed and run on fixtures we wrote; adapter allows 3 static commands; mapping card verdict: lossy | `adapters/rea.py`, `docs/mapping_cards/rea.md` |
| Fleet provisioning | Mission card (read-only context), `targets.yaml` (only source of valid targets), interactive enrollment, connection planner, size table, template-only SSH and docker runners, approval gate, pinned checksummed worker, localhost-only server, SSH tunnel, nonce-based server check, one-approval undo | Docker-tested, see below |
| Safety extras | Escalation to the controller's model at an exact threshold, per-device skill re-verification | `loop/escalate.py`, `skills/` |

### Live results
- **Fleet (real containers):** full flow over real SSH including tunnel and undo; RAM tiers 1/2/4 GB gave 0.5B/1.5B/3B; read-only filesystem, offline box without Python, truncated and killed transfers all failed cleanly and undid completely; a 2 GB container on a 7.6 GB host used the stricter limit; five boxes (Debian and Alpine, aarch64 and emulated x86_64) scored all-correct against a hand-written ground truth.

### Bugs that real runs found (all fixed, each with a regression test)
Confirm tool hid itself after one use; stale target in the loop; a retry message that made a model repeat one mistake dozens of times; a lost comma in generated ground truth; "no saved skill" reported as unknown; zipapp stamping the current time into the worker archive; `docker exec -i` swallowing approval answers typed at the terminal.

## 2. Structure

```
env-recognition/
  src/envrec/                 the controller (about 5,700 lines)
    contracts/                Fact, Evidence, Target, ToolSpec, Action, Skill, ...; supersede and contradiction rules
    store/                    SQLite + raw files by sha256; events cannot be edited
    policy/                   decide(); config_gate.py = the targets.yaml gate (cannot see the mission card)
    model/                    adapters (OpenAI-compatible, scripted), parsing, retry, narrow repair
    adapters/                 Adapter base; builtin Ring 0; rea.py
    probes/                   Ring 0 probe functions
    fingerprint/              public-database loaders + serial signatures
    scoring/                  ground truth, scorer, rungs, per-source stats
    loop/                     view, menu, run loop, prompts, escalate.py
    ladder/                   replay devices, ladder tools, generate.py (rungs 4/5, verifier, runner)
    skills/                   per-device re-verification
    mission/                  MissionCard, TargetEntry/TargetConfig (strict schema)
    inventory/  enroll/       report (writes nothing); the only code that writes targets.yaml
    remote/                   templates, runner, planner, sizing, preflight, approval, recipes, provision, fleet
    verify/                   "server is running" check with fresh nonces
    guard.py  cli.py  cli_fleet.py
  src/envrec_worker/          what runs on a device: stdlib only, no deploy/enroll/planner/scan code
  build/worker/               build.py (reproducible .pyz) and release.json (pinned sha256)
  config/                     models.yaml, mission/targets examples
  ground_truth/               this-machine (unconfirmed), replay fixtures, software fixtures, fleet.yaml
  fixtures/                   replay devices, docs, software we wrote, recorded real REA output
  playbooks/                  one-page cards per target kind (host, usb)
  docs/                       MISSION_CARD, CONNECTING_DEVICES, PROVISIONING_SAFETY, LADDER_NEXT, REA_ALIGNMENT, mapping_cards, DEMO stub
  tests/                      23 modules + docker/ (fixture images) + docker_helpers.py
  scripts/                    data fetch, fixture generator, REA wrappers
  context/                    the nine source-of-truth documents
  SCOUTING.md  THIRD_PARTY.md  DATA_SOURCES.md  README.md
```

## 3. How a turn flows

```
store --> view (capped facts) + menu (closed, capped) --> model --> one JSON Action
      <-- facts + events <-- run via adapter <-- policy decision <-- validate (retry once, then fallback)
```

For devices: `targets.yaml` entry --> planner picks a transport from the allowed list --> read-only preflight --> size table --> approvals one by one --> pinned worker --> localhost server --> nonce check --> `verified`.

## 4. Fixed limits (shipped policy)
Targets only from `targets.yaml`; every write needs a fresh approval; a worker cannot deploy (the code is not in it); server bound to localhost; no autostart; no credentials in files. Details: `docs/PROVISIONING_SAFETY.md`.

## 5. Not done or not real yet
- The worker's "model runtime" is a stub that echoes; no real language model is provisioned yet.
- Thin phone worker, worker-side loop client, `adb_usb` and `serial` runners, Docker runner for generated probes.
- No OS-level network ban on REA (`sandbox-exec` cannot launch `ps`); replay probe runner is a restricted subprocess, not a security sandbox.
- Real SSH to a second physical machine; the swarm; the small-vs-strong experiment as a controlled study.
- `ground_truth/this-machine.yaml` still needs your confirmation.
