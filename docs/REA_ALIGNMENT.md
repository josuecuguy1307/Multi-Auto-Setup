# REA alignment

Our contracts are the authority. This maps REA's record concepts onto them. **Provisional:** it is written from REA's docs (`docs/cli.md`, `docs/mcp-contracts.md` at commit d87e73f2). It is checked against real REA output in M10 and corrected in `docs/mapping_cards/rea.md`.

| REA concept (from its docs) | Our contract | Notes |
|---|---|---|
| Evidence `evidence_id` | `Evidence.event_id` (the event that recorded it) | REA ids look like `ev_…`; we keep ours as events |
| Evidence `provider` | `Evidence.provider` | We set `rea` for everything that came through the adapter; REA's own provider name goes in `Evidence.probe` |
| Evidence `confidence` | `Evidence.confidence` (0..1) | REA's scale must be read from real output; if it is categorical it is mapped in the card |
| Evidence `limitations[]` | `Evidence.limitations[]` | Copied as-is |
| Evidence `raw_result` / `normalized_result` | `RawRef` (path + sha256) | The whole REA JSON is saved by hash. Nothing from it is inlined into a Fact value except short scalars |
| `coverage.scope` (`root`, `container-entries`) | `Coverage.scope` | |
| `coverage.status` (`complete` / `unknown`) | `Coverage.status` | We add `partial` for adapters that know they stopped early |
| `record_unknown` / `update_unknown` (residual question text) | `ProbeResult.unknowns[]` → `Unknown(key, question)` and a fact with status `unknown` | Unknown keys must sit in our namespace (`app.*`, `fw.*`, `signal.*`) |
| "An empty list with partial or unknown coverage is not proof of absence" | Facts from empty results are written `unknown`, never `false` or `[]`, when coverage is not `complete` | Rule enforced in the REA adapter parser |
| Target categories (34) | `Target.kind` (`app`, `repo`, `signal`, …) | Only `app` is used in this phase |
| Per-target reference files | `playbooks/<kind>.md` | Same idea, one page per target kind, shown to small models only |

What REA has that we do not copy: managed evidence bundles, snapshots, workflow profiles, provider sessions (Hopper/Ghidra/IDA). Out of scope.
