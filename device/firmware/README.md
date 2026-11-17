# Key firmware (MAS portable device)

The firmware of the approval key: one physical press, one signature, for exactly the request shown on the key's display.
Spec: `DECISIONS.md` D-004 (token), D-007 (MCU, language, emulation path), A-2 (advisor's firmware spec).

| Path | What |
|---|---|
| `core/` | Portable core in C99, static memory only: state machine (`mas_core.c`), framing (`mas_frame.c`), SHA-256, counter log and key page (`mas_nv.c`, the only flash writer), HAL interface (`mas_hal.h`). |
| `host/` | Host HAL mock and stdin driver. Flash is a file with NOR semantics, TRNG is a file, tick and button come from the test harness. |
| `vendor/monocypher/` | Monocypher 4.0.2, unmodified (BSD-2-Clause or CC0). Pure Ed25519 with SHA-512. |
| `tests/` | Small host-only tools (vectors, Ed25519 smoke check). |

## Build and test (host, SIMULATED)

The compiler is `zig cc` from PyPI, kept inside the repo's git-ignored `runs/toolchain`:

```
uv venv runs/toolchain --python 3.13
uv pip install --python runs/toolchain/Scripts/python.exe "ziglang==0.13.0"
.venv/Scripts/python device/firmware/build.py
.venv/Scripts/python -m pytest -q device/tests/test_firmware_host.py
```

Or set `MAS_CC` to any C99 compiler command.

## What is and is not verified

- **SIMULATED (host build):** protocol, press rules, counter durability under power cuts at every flash write, byte-identical tokens
  with the Python reference model, acceptance of the tokens by the policy gate. See `device/tests/test_firmware_host.py`.
- **Planned, not built:** QEMU (`mps2-an505`) and Renode runs (P4-FW-EMU); a libFuzzer target (needs a Linux container; a deterministic
  10,000-frame Python fuzz runs instead).
- **Not verifiable without hardware:** secure boot and RDP lock, TrustZone partitioning, glitch and side channels, TRNG quality, USB
  enumeration, button debounce and ESD, flash endurance, and STM32U5 flash ECC behaviour on a torn write.
