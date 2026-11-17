# DECISIONS.md — MAS portable device

Each decision: what, why, alternatives dropped, and who reviewed it. Advisor advice is kept verbatim at the end (A-1, …).
Dates are absolute. "Advisor" = Fable 5.2 (`claude-fable-5-2`) consulted as a read-only subagent; "reviewer" = `.claude/agents/reviewer.md`.

---

## D-001 Where the device lives (2026-11-17)
- Code goes into the MAS repo on branch `device/v0`: `src/mas_device/` (Python: gate, key protocol, log, runtime), `device/` (tests,
  acceptance, firmware, hardware, medium, evals, paper). Existing `envrec*` packages are reused, not forked.
- MISSION 8.1 says the repo shows no license: **outdated**. `main` carries Apache-2.0 (`LICENSE`, `NOTICE` naming Josué Arcos). No license
  decision is needed for the software; the hardware license is proposed in D-014 and left to Josué.

## D-002 Harness and Claude Code features adopted (2026-11-17)
Installed: the session runs the desktop app's bundled Claude Code **2.1.325**; the `claude` on PATH (winget) is still **2.1.79**. I read the
official advisor and model-config pages and the part of the changelog the fetch returned (2.1.323–2.1.326); I did **not** read every entry
from 2.1.79 to 2.1.322, so features from that range may be missing here.

Adopted:
- **Advisor = Fable 5.2.** It exists and requires ≥ 2.1.321; the `fable` alias resolves to `claude-fable-5-2`; Opus 5.5 accepts Fable as
  advisor. In this interactive session it is consulted as a read-only subagent (`model: fable`), which ran as Fable 5.2 (A-1). In the loop,
  `run.sh` passes `--advisor fable` (env `MAS_ADVISOR`, set it to `off` to disable). Caveat: under `-p`, Fable is billed to usage credits
  **without asking** — recorded in NEEDS_HUMAN.md.
- **`run.sh` picks a Claude binary ≥ 2.1.321** (env `CLAUDE_BIN`, else the newest found), because the PATH one cannot use Fable 5.2.
- **Fresh `claude -p` sessions in a loop**, stopped by the oracle (`device_accept.py status` exit 0) or `AGENT_STOP`. Considered `/goal` and
  `--bg`/`respawn`: kept the loop because its stop condition is a deterministic script, not the model's judgment; `/goal` may be added inside a
  session later.
- **Hooks** (`.claude/settings.json` → `scripts/device_hook.py`): SessionStart injects STEER.md, AGENT_STOP and the open criteria;
  PreToolUse(Bash) is the commit gate (refuses `git commit` while a PASS is stale or results were hand-edited); Stop auto-commits only when
  device tests and `verify` are green, never pushes, never blocks (a blocking Stop hook stops applying after 8 blocks; and 2.1.326 made
  `decision: block` hold in more cases, so no hook here ever blocks except the commit gate).
- **Subagents** in `.claude/agents/`: `evaluator` (Read/Glob/Grep/Bash, no Write/Edit, FAIL by default, runs the product) and `reviewer`
  (advisor stand-in). Parallel work only for independent units, each in its own worktree (`isolation: worktree`) with a lock file in
  `current_tasks/`.
- **Sandbox:** the Claude Code Bash sandbox is macOS/Linux/WSL only, so on this Windows host the container is the sandbox
  (`device/harness/Dockerfile`, next unit). `sandbox.autoAllowBashIfSandboxed` (2.1.325) is set inside the container profile only.

Oracle design: `scripts/device_accept.py` — default FAIL; skip/missing/zero tests = FAIL; a PASS stores the content hash of the files it
covers and turns STALE when they change; `verify` detects hand edits (checksum) and an out-of-date ACCEPTANCE.md table. The oracle has its own
tests (`device/tests/test_harness.py`). Planned hardening from A-1 §7: saboteur patches that must flip the suite red; run-time seeds.

## D-003 Architecture: key + compute (2026-11-17; advisor A-1 §1, §8.2)
- **Key**: small MCU with a physical button and a display; holds the Ed25519 signing key and the counter; signs only after a press. It is
  the only approval path for writes.
- **Compute v0 = the host's own CPU/GPU**, launched by hand by the host's owner from a read-only USB mass-storage LUN (write-protect set by
  the device, never CD-ROM, never HID, never autorun). **Pi 5 mode is VM-emulated only in v0**: a Pi 5 wants up to 5 V/5 A and many host ports
  give 2.5–7.5 W, and its gadget port is its power input. **Jetson is cut.** (Recorded in NEEDS_HUMAN.md for Josué to confirm.)
- **Policy gate** is a separate process from the model; the model has zero ambient authority (llama-server is inference only) and can
  only *request* actions through a narrow RPC. Defence in depth where the OS offers it (Landlock/seccomp, restricted token, sandbox-exec).
- Phase 2 precedes the hardware: a **software key** speaking the identical protocol, labelled SIMULATED. MAS `chat_choice` approvals are
  disabled in the device build.
- Reuses MAS: `envrec_net` identities and pairing, `prepare_write` (shown action + undo recorded before the write), the policy function.
  Fixes required before reuse (A-1 §2): undo arguments must come from a signed token, not from store events `Actor.loop` can append;
  `plan: shown[:600]` truncation replaced by logging the full structured action.

## D-004 Approval token (2026-11-17; advisor A-1 §3; implemented in `src/mas_device/approval.py`)
Changed from the builder's first draft (JSON with `issued_at` and a persisted `last_counter`) after the advisor's review:
- The key signs a **fixed-width 177-byte body**, no JSON on the MCU:
  `"MAS-APPROVE-v1\0" | key_id[8] | counter u64 | session_id[32] | nonce[16] | action_hash[32] | undo_hash[32] | summary[32 ASCII] | class u8 | press_window_s u8`
  + pure Ed25519 (RFC 8032, SHA-512) signature, 64 bytes. Firmware must use Monocypher's optional `monocypher-ed25519`, not its default BLAKE2b EdDSA.
- **Single use = a random nonce the gate holds in memory** and deletes on first success; a restart forgets every outstanding nonce. No
  persistent `last_seen` is needed on a host that must keep no trace. The counter is for ordering and gap detection; within a session it must increase.
- **TTL on the gate's monotonic clock** from `begin` (120 s max); the key enforces only its press window on its own tick. No `issued_at`.
- **Host binding**: `session_id = sha256("MAS-SESSION-v1\0" | len | session_pub | pairing_transcript)`.
- **action_hash** = sha256(domain | strict canonical JSON) on the host only; canonical form refuses floats, bytes, sets, tuples, non-NFC
  strings, non-string keys, ints beyond 64 bits; duplicate JSON keys are refused at parse time. The action includes the target's
  **pre-state hash** (TOCTOU).
- **undo_hash** is bound at approval; a WRITE or JOIN_FLEET without an undo is never offered. Executing an undo is a new action of class UNDO
  with its own nonce and press, accepted only if its hash matches an undo_hash from a consumed token (Phase 2 gate).
- **Classes**: WRITE, UNDO, JOIN_FLEET, EXPORT_DATA, NET_UP, LOG_HEAD, INSPECT_SESSION (no cross-protocol reuse).
- **Summary**: derived deterministically from the action (`summary_for`), shown on the key with the counter, signed; the gate rejects a token
  whose summary differs from what it re-derives. A lying host gets a token the gate refuses.
- A rejected attempt with the wrong action does **not** burn the token (the token only ever covers its own action); expiry does burn it.

## D-005 Threat model (2026-11-17; advisor A-1 §2)
Boundary: the key protects the host **from the model and from injected data**, not from a compromised host. In host-compute mode the gate
and the model share an OS user; the separation is what the OS provides. Table (attack → mitigation → test) is in A-1 §2 and becomes
`device/tests/test_guardrails.py` in Phase 2; each attack gets a positive control against a deliberately weakened build.

## D-006 Guardrails → mechanism → criterion (2026-11-17)
| MISSION 4.1 rule | Mechanism | Criterion |
|---|---|---|
| 1 nothing without the owner's explicit action; no HID, no autorun | owner-launched runtime; USB descriptors limited to MSC (+NCM after a press); no service/autostart created | P2-PORTABLE, P2-GUARDRAILS, P4-FW-EMU (descriptor allowlist) |
| 2 read-only by default; one fresh approval per write | gate refuses any write tool without a token; one press = one request; INSPECT_SESSION press for reads (D-012) | P2-GATE, P2-GUARDRAILS |
| 3 signed, bound to the action hash, single-use, expiring | D-004 | P1-UNIT, P4-FW-HOST |
| 4 limits outside the model's reach | gate is a separate process; policy and pinned keys on the RO LUN; key firmware has no opcode to change them | P2-GUARDRAILS, P4-FW-EMU (opcode allowlist) |
| 5 complete append-only log visible to both owners | hash chain, head countersigned by the key (LOG_HEAD), shown on screen; export only with EXPORT_DATA approval | P2-LOG |
| 6 nothing installed after unplug unless joined; every write records its undo | D-010 three-arm diff; undo_hash bound | P2-ZEROTRACE, P2-GATE |
| 7 propagation always human-initiated | node build has no deploy-further code (MAS `worker_can_deploy_further: false`); JOIN_FLEET is its own class | P2-FLEET, P2-GUARDRAILS |
| 8 no scanning, no firewall/router changes, no credential collection, no evasion | device registry excludes `lan-sweep` and any socket-scanning skill; denylist of tools; static scan of the device build | P3-READONLY, P2-GUARDRAILS |
| 9 inspection reports, never exploits | no exploit code (source scan); report only | P3-READONLY |
| 10 inspection data stays local | no egress in inspection (pcap / no-network container); EXPORT_DATA class for any export | P3-READONLY |

## D-007 Key MCU, firmware language, emulation path (2026-11-17; advisor A-1 §4)
**STM32U585 (Cortex-M33, TrustZone), C portable core (no heap, ~300 lines), Monocypher + monocypher-ed25519.** Path: host build + libFuzzer
→ QEMU `mps2-an505` (M33, core only, UART transport) → Renode STM32 platform (if no U5 model, L552, labelled). Same Python harness drives
framed (COBS) transcripts on all three. Dropped: RP2350 (emulation uncertain; published glitch breaks on early steppings), TKey (touch not
button, FPGA BOM, reciprocal hardware license — its "key identity derives from firmware hash" idea is documented and copied in spirit),
TamaGo (application SoCs), Rust (no gain for this size). Not verifiable in emulation: RDP/secure-boot lock, glitch and side channels, TRNG,
real USB enumeration, debounce/ESD, flash endurance — all labelled and listed in the paper's limitations. Advisor said its emulator claims
are from memory: verify QEMU/Renode support on day one of Phase 4.

## D-008 Display yes, secure element no (2026-11-17; advisor A-1 §5)
SSD1306 OLED behind the HAL showing the signed summary and counter (a bare code derived from the hash is theatre: the host computes both
sides). ATECC608B cannot do Ed25519: DNP footprint only; physical key extraction is out of scope for v0, mitigated by revocation.

## D-009 PCB as code (2026-11-17; advisor A-1 §6)
KiCad files are canonical; gates = `kicad-cli` ERC/DRC + KiBot, zero exclusions, capped no-connects, schematic parity, zero unrouted,
fab rule file. Authoring with Zener (diodeinc/pcb) after a time-boxed headless spike; fallback atopile; Freerouting as an external tool.
`HUMAN_REVIEW_PCB.md` starts from the ten items in A-1 §6.

## D-010 "No trace" defined honestly (2026-11-17; advisor A-1 §1)
"Nothing installed: no service, driver, autostart, PATH change or file outside a declared temp dir removed on exit. OS-generated residue is
limited to an enumerated allowlist of classes" (Windows USBSTOR/setupapi/Prefetch/Amcache/…, macOS unified log/LaunchServices, Linux
journald/udev). Measured by offline snapshot diffs with three arms (control blank stick, device session, session + approved writes).
README and paper must not say "sin dejar rastro" without this definition.

## D-011 Inspection scope v0 (2026-11-17; advisor A-1 §8.7)
osquery/Syft/Grype-style inventory and SBOM with an offline vulnerability DB whose date stamps every report. Cut for v0: fwupd, EMBA,
binwalk, Ghidra (root, device access, GPL). `lan-sweep` and every network-scanning skill are excluded from the device registry.

## D-012 Reads and button fatigue (2026-11-17; advisor A-1 §8.4)
One press of class INSPECT_SESSION authorises a read-only inspection session; writes stay one press per action.

## D-013 Platforms (2026-11-17; advisor A-1 §8.8)
Build and test in Linux containers. Windows host support is a small cross-platform launcher and executor, not a port of all of envrec. On
this Windows dev host the existing MAS suite fails to collect 8 modules (termios, fcntl, `time.tzset`, one import) — MAS was built on
macOS/Linux; not fixed here, out of scope.

## D-014 Hardware license (proposal, Josué decides) (2026-11-17)
The key board is an original design (no TKey/SoloKeys PCB derived), so no reciprocal obligation is inherited. Proposed: CERN-OHL-P-2.0 for
hardware. In NEEDS_HUMAN.md.

---

## A-1 Advisor review — Fable 5.2, 2026-11-17 (verbatim)

> Basis stated by the advisor: read MISSION.md, envelope.py, ctx.py and KNOWN_GAPS.md; did not read policy.py, PROVISIONING_SAFETY.md or
> NETWORK_CONTRACT.md; ran nothing; emulator and toolchain support claims are from memory and must be verified.

**1. Mass storage + owner-launched runtime.** Yes for v0, with three changes. Read-only enforced by the device, not the filesystem: use the
write-protect bit on the LUN (gadget `ro=1`); otherwise the host writes System Volume Information, .Spotlight-V100, .fseventsd and .DS_Store
onto the medium and the stick becomes a cross-host malware carrier. Never present as CD-ROM/ISO (autorun semantics). Linux exec trap: udisks
typically mounts vfat/exfat with `showexec`, so ELF files are not executable — use a filesystem with real permission bits, or have
`sh launcher.sh` load the runtime into memfd or `$XDG_RUNTIME_DIR` (tmpfs). Python hygiene: `-I`, `PYTHONDONTWRITEBYTECODE`, no user site,
state in a temp dir removed on exit. Keep logs on the key/Pi side, or on a separate RW LUN from which nothing is ever executed.
"Zero trace" is false as written; the OS records the event regardless — Windows: USBSTOR/MountedDevices, setupapi.dev.log, Prefetch,
Amcache/ShimCache, BAM, UserAssist, SRUM, shellbags, event logs, Defender scan history, USN journal; macOS: unified log, LaunchServices,
possible TCC prompts, arm64 binaries need at least ad-hoc signing; Linux: journald/dmesg, udev db, recently-used.xbel, shell history.
CDC-NCM adds a persistent network profile on Windows (NetworkList) and a new interface: bring it up only after a key press, never at
enumeration. Honest definition: "Nothing installed: no service, driver, autostart, PATH change or file outside a declared temp dir removed on
exit. Residual OS-generated artifacts are limited to an enumerated allowlist of classes." Measurement: offline diff of VM disk snapshots
(file manifest plus registry hive diff), three arms — control (blank USB stick plus a trivial exe), device session, device session plus
approved writes. Pass when (device − control) is within the allowlist and arm 3 − arm 2 equals the approved set, then equals arm 2 again after undo.

**2. Threat model.** State the boundary first: the key protects the host from the model and injected data, not from a compromised host. In
host-compute mode the policy process and model share a user, so separation is only what the OS gives you. Give the model zero ambient
authority: llama-server is pure inference, and only the harness holds capabilities. Add Landlock/seccomp, a restricted token and sandbox-exec
as defence in depth.

| Attack | Mitigation | Deterministic test |
|---|---|---|
| Malicious model | Only the executor writes; it requires a valid token for the recomputed hash | Scripted model emits every tool-call shape; zero writes without token; positive control against a weakened build |
| Compromised host | Out of scope for prevention; key-countersigned log gives detection | Doc test: claim list contains no host-malware claim |
| Stolen device | No host credentials stored; fleet revokes key_id | Revoked key's token rejected |
| Malicious host owner | The device owner is the asset here: memory encrypted at rest, inspection data never auto-exported | Session on a hostile VM: device RO LUN hash unchanged |
| TOCTOU | Hash covers argv, binary hash, input file hashes, target pre-state hash; executor works by fd, O_NOFOLLOW, re-verifies, temp plus rename | Swap symlink or content between approve and execute: refused |
| Display spoofing | Key shows a signed summary; host text rendered deterministically from the structured action | Summary differing from f(action): reject |
| Replay across hosts | Per-session id plus verifier nonce | Token from VM A rejected on VM B and after restart |
| Counter rollback by reflash | Counter is not the replay defence; update erases the key; commit before sign | Power cut at every flash write: counter never reused |
| Undo confused deputy | undo_hash bound inside the action; undo executor accepts only undos whose hash appears in a signed token, plus a fresh press | Forged undo_entry event: refused |
| Prompt injection from host data | Captures stay out of the model (MAS refs already do this); model output is only a request | Fixtures with injected instructions: zero unapproved writes, zero egress |

Two issues in ctx.py: today `undoing=True` skips recording and undo arguments come from store events that `Actor.loop` can append — the
confused-deputy hole; `plan: shown[:600]` truncates the record — log the full structured action.

**3. Token spec.** The key signs a fixed-width binary struct, not JSON; do not put a JSON parser on the MCU (MAS's `canonical()` signs
floats via json.dumps, which is not reproducible in C). Layout: `"MAS-APPROVE-v1\0" | key_id[8] | counter u64 | session_id[32] |
verifier_nonce[16] | action_hash[32] | undo_hash[32] | summary[32 ASCII] | class u8 | press_window_s u8`. Signature: pure Ed25519;
Monocypher's default EdDSA uses BLAKE2b — you need the optional monocypher-ed25519 (SHA-512) to interoperate with Python cryptography;
cross-check with RFC 8032 and Wycheproof vectors. action_hash: sha256 over a domain tag plus length-prefixed canonical encoding, host only.
Single use: the verifier nonce, not the counter — a "no trace" host has no persistent last_seen; the policy process issues a random nonce
per pending action, holds it in memory, deletes it on first use; restart kills all outstanding tokens; the counter is for audit ordering and
gap detection. TTL: drop issued_at — the MCU has no trusted clock; the verifier measures from nonce issuance on its monotonic clock; the key
enforces only its press window on its own tick. Host binding: session_id = sha256(ephemeral session pubkey ‖ pairing transcript). Undo:
bound at approval via undo_hash; executing it is a new action of class=UNDO with its own nonce and press. Classes: WRITE, UNDO, JOIN_FLEET,
EXPORT_DATA, NET_UP, LOG_HEAD — separate classes prevent cross-protocol reuse.

**4. MCU, language, emulator.** STM32U5 (U585), C portable core, Monocypher. Not RP2350: uncertain emulation, and its 2025 hacking challenge
published glitch breaks on early steppings. Not TKey: it has the best model (key identity derives from the firmware hash, so reflash gives
a different key) — copy that idea in documentation — but it is touch not button, an FPGA BOM, and reciprocal CERN-OHL if you derive the
PCBA. Not TamaGo: application-class SoCs. Not Rust: adds nothing a 300-line no-heap state machine needs. Emulator path with one Python harness
driving the same framed transcripts (COBS over UART/stdio): host build plus libFuzzer; QEMU mps2-an505 (Cortex-M33), core only; Renode STM32
platform (if the U5 model is missing, L552, labelled). Verifiable in emulation: state machine, signatures, counter atomicity under injected
power cuts, press window in virtual time, malformed-frame rejection, opcode surface. Not verifiable: secure boot and RDP actually locked,
glitch and side-channel resistance, TRNG quality, real USB enumeration, debounce and ESD, flash endurance. Emulate transport as UART and mark
USB CDC as hardware-only.

**5. Display and secure element.** Display yes, secure element no. A bare short code derived from action_hash is theatre: the host computes
both sides. Show the signed summary (verb, target, file count, counter); the verifier rejects any summary that differs from f(action) —
auditable evidence of what the key showed. SSD1306 behind the HAL, framebuffer text tested on the host build. ATECC608B cannot do Ed25519
(P-256 only): DNP footprint, physical key extraction out of scope for v0, mitigated by revocation.

**6. PCB toolchain.** KiCad files canonical; gate with kicad-cli ERC/DRC plus KiBot; author with Zener (diodeinc/pcb) after a time-boxed
spike where a trivial board must go green headless; fallback atopile; Freerouting external. Anti-gaming gates: zero DRC exclusions, capped
no-connect count, schematic parity, zero unrouted, fab-specific rule file. HUMAN_REVIEW_PCB.md top 10: (1) every MCU pin against the
datasheet for the exact package and part number; BOOT0, NRST, VCAP/SMPS pins; (2) USB-C: separate 5.1k on CC1 and CC2, ESD part at the
connector, shield termination, D± routing; (3) power: LDO dropout and thermal, at most 10 µF on VBUS, per-pin decoupling, VDDA; (4) button
direct to a secure-world GPIO, fails to "not pressed" if open, not shared with a boot strap, no other driver on the net; (5) SWD access for
bring-up, lock plan, pads acknowledged; (6) clock: HSI48 with CRS, or crystal load capacitors; (7) footprints against manufacturer drawings,
pin 1, connector mechanics against board thickness; (8) OLED module pin order (VCC/GND varies by vendor), I2C pull-ups, LED currents;
(9) fab capability: stackup, trace/space, vias, test points, fiducials; (10) BOM: exact MPNs, lifecycle, stock date, alternates, DNP marks,
licence of any derived design.

**7. Acceptance tests.** One command, `make accept PHASE=n`, from a clean clone, in a container with network off; test-results.json bound to
the commit SHA and a clean tree. Anti-gaming for all phases: saboteur patches the grader applies (skip signature verify, skip nonce deletion,
skip pre-state check) — the suite must flip to FAIL for 100% of them; vectors generated at run time from a grader-supplied seed; tests
directory checksum-locked after Phase 1. Per phase: (1) history has a commit where the canary test fails and a later one where it passes;
the grader runs both; a hand-edited results file is rejected. (2) Three-arm disk diff in a Linux VM; Windows only if an image exists,
otherwise labelled unverified; pcap shows zero non-peer packets; guardrail battery zero violations, each attack with a positive control.
(3) Precision and recall against planted fixtures, thresholds fixed in advance; block-device sha256 identical before and after; lan-sweep
and any socket-scanning skill absent from the built registry and uninvokable. (4) Identical transcripts on host, QEMU and Renode; rejected:
replay, bit-flip in every field, expired, wrong session, no press, press before request, press after window; power-cut sweep; 10^6 fuzzed
frames produce no signature without a press; opcode set equals the allowlist. (5) ERC/DRC exit 0 under the gates of §6; every fab file
contains NO FABRICAR; BOM resolved against a dated snapshot; review file has the ten items. (6) `make paper` regenerates the PDF; no numeric
literal in results sections outside generated macros; k consecutive passing runs.

**8. Wrong, risky, or cut.** (1) "Sin dejar rastro": redefine as in §1. (2) Pi 5 from a host port is not viable (up to 5 V/5 A; many host
ports give 2.5–7.5 W; its gadget port is its power input): v0 is key plus host compute; Pi mode VM-emulated, to NEEDS_HUMAN; cut Jetson.
(3) Phase 2 precedes the key: software key speaking the identical protocol, labelled; disable MAS chat_choice approvals in the device
build. (4) Button fatigue: MAS asks approval for reads too; one press authorises a read-only inspection session, writes stay per action.
(5) Fleet join contradicts "nothing installed": its own class shown on the key. (6) The host-owner log copy is itself a write: show it on
screen and offer an approved export. (7) Cut for v0: fwupd, emba, binwalk, Ghidra (root, device access, GPL); stamp every report with the
vulnerability DB date. (8) Windows: build and test in a Linux container; Windows host support is a new small cross-platform launcher and
executor, not a port of all of envrec. (9) Enclosure: parametric model with a fit check only.
