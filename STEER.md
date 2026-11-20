# STEER.md

> Josué: write direction changes below, newest first, one per line with a date. Every session reads this file first and it overrides earlier plans.
> Lines starting with `#` or `>` are ignored by the session-start hook.


2026-11-17 — Josué (in chat): go straight to the hardware work (firmware of the key, then PCB). Open software fixes wait.
2026-11-17 — Josué (in chat): install KiCad natively (winget) for Phase 5; Docker not needed for the PCB.
2026-11-19 — Josué (in chat): OK to download and run a local model for P2-MODEL (pick a small, openly licensed GGUF model; record its license and sha256 in THIRD_PARTY.md; keep it under runs/ or the medium folder, never committed).
2026-11-19 — Josué (in chat): hardware license = CERN-OHL-S-2.0. Docker will be fixed together at the start of the next session (engine never comes up; WSL 2 with Ubuntu is installed; try `wsl --update`, `wsl --shutdown`, restart Docker Desktop).
