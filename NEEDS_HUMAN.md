# NEEDS_HUMAN.md — things only Josué can do

Each item: what, why, exact steps. The run keeps going on other units meanwhile.

## Open

1. **Fable 5.2 advisor cost in the unattended loop.** `run.sh` passes `--advisor fable`. Under `claude -p`, Fable is billed to usage credits
   **without a consent prompt** (official model-config docs). If you do not want that: run `MAS_ADVISOR=off bash run.sh`, and the loop
   falls back to the `reviewer` subagent at the same decision points. To accept it explicitly once: in an interactive session run
   `/model fable` and choose to continue on Fable.
2. **Claude Code on PATH is 2.1.79** (winget). Fable 5.2 needs ≥ 2.1.321. Either update it (`winget upgrade Anthropic.ClaudeCode`, or
   `claude update`) or keep using the app's bundled build: `run.sh` looks for it automatically, or set `CLAUDE_BIN`.
3. **Starting the unattended loop is your decision.** `bash run.sh` runs fresh sessions with `--permission-mode bypassPermissions` until every
   criterion is PASS or `AGENT_STOP` exists. Recommended: container mode (`MAS_LOOP_MODE=container bash run.sh`, next unit builds the image).
   Authentication inside the container: run `claude setup-token` yourself and pass it as an environment variable at `docker run`; it must
   never be written to the repo or logs.
4. **Pushing `device/v0`.** The first push will use your git credentials on this machine. If it fails, the work stays committed locally and
   the run continues.
5. **Confirm v0 compute = the host's own CPU/GPU** (DECISIONS D-003): a Pi 5 cannot be powered from most host USB ports, so Pi mode is
   VM-emulated only and the Jetson option is cut. Reply in STEER.md if you disagree.
6. **Hardware license** (DECISIONS D-014): proposed CERN-OHL-P-2.0 for the key board. Confirm or pick another in STEER.md.

## Later (from MISSION section 10; instructions get filled in when the phase reaches them)
- Real test between two physical machines (Phase 2 end).
- Buying boards (STM32U585 dev board + SSD1306 + button) — never bought by the agent.
- Human review of the PCB before any fabrication (`HUMAN_REVIEW_PCB.md`, Phase 5).
- arXiv submission and endorsement by an established author (Phase 6).
