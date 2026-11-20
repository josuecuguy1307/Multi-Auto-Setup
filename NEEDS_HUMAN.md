# NEEDS_HUMAN.md — things only Josué can do

Each item: what, why, exact steps. The run keeps going on other units meanwhile.

## Open

1. **Fable 5.2 advisor in the unattended loop (opt-in).** Under `claude -p`, Fable is billed to usage credits **without a consent prompt**
   (official model-config docs), so since reviewer R-1 M8 `run.sh` runs with the advisor **off** and uses the `reviewer` subagent instead.
   To opt in: `MAS_ADVISOR=fable bash run.sh`. Interactive sessions can still use Fable (`/model fable`).
2. **Claude Code on PATH is 2.1.79** (winget). Fable 5.2 needs ≥ 2.1.321. Either update it (`winget upgrade Anthropic.ClaudeCode`, or
   `claude update`) or keep using the app's bundled build: `run.sh` looks for it automatically, or set `CLAUDE_BIN`.
3. **Starting the unattended loop is your decision.** `bash run.sh` runs fresh sessions with `--permission-mode bypassPermissions` until every
   criterion is PASS or `AGENT_STOP` exists. Recommended: container mode (`MAS_LOOP_MODE=container bash run.sh`, next unit builds the image).
   Authentication inside the container: run `claude setup-token` yourself and pass it as an environment variable at `docker run`; it must
   never be written to the repo or logs.
4. ~~Pushing `device/v0`~~ — done 2026-11-17 (Git Credential Manager sign-in).
5. **Confirm v0 compute = the host's own CPU/GPU** (DECISIONS D-003): a Pi 5 cannot be powered from most host USB ports, so Pi mode is
   VM-emulated only and the Jetson option is cut. Reply in STEER.md if you disagree.
6. ~~**Hardware license**~~ — decided 2026-11-19: CERN-OHL-S-2.0 (`LICENSE-HARDWARE.md`). Still to do: copy the full licence text verbatim from https://ohwr.org/cern_ohl_s_v2.txt into `LICENSE-CERN-OHL-S-2.0.txt` (a download: ask Josué first).

## Later (from MISSION section 10; instructions get filled in when the phase reaches them)
- Real test between two physical machines (Phase 2 end).
- Buying boards (STM32U585 dev board + SSD1306 + button) — never bought by the agent.
- Human review of the PCB before any fabrication (`HUMAN_REVIEW_PCB.md`, Phase 5).
- arXiv submission and endorsement by an established author (Phase 6).

7. **Docker Desktop: start it yourself from the Start menu** (2026-11-19). Root cause of the missing `dockerDesktopLinuxEngine` pipe:
   Docker had been started from inside the Claude app, whose AppData virtualization leaves socket files Windows cannot delete. The
   stale sockets were removed and the engine came up (29.2.1). If it breaks again, run from a normal PowerShell window:
   `powershell -ExecutionPolicy Bypass -File scripts\windows\start-docker.ps1`. Claude must never start or restart Docker itself.

8. **KiCad for Phase 5 (PCB checks).** The ERC/DRC gates need `kicad-cli`. Either get the Docker engine running (Docker Desktop opens but
   `docker info` still cannot reach `dockerDesktopLinuxEngine`; usually fixed by `wsl --update` then restarting Docker Desktop), and the
   run uses the official `kicad/kicad:9.0` image; or allow a native install: `winget install KiCad.KiCad`. Say which in STEER.md.

9. ~~Renode for P4-FW-RENODE~~ — done 2026-11-17: portable zip, unpacked into runs/toolchain/renode (D-019).

10. **Secure boot and lock review (P4-SECBOOT, DESIGN-ONLY).** `device/firmware/SECURE_BOOT.md` names the STM32U585 option bytes
    (TZEN, SECBOOTADD0, BOOT_LOCK, SECWM, WRP, HDP, RDP level 2) from memory: check each name and value against RM0456 and the STM32U5
    security application notes, then provision one sacrificial part and try to read it with a debugger at each RDP level. RDP level 2
    is permanent: never set it on a part you want to reprogram.

11. ~~Local model for P2-MODEL~~ — Josué said OK on 2026-11-19 (STEER.md).
