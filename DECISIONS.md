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

## R-1 Reviewer pass — fresh-context reviewer (built-in Plan agent with `.claude/agents/reviewer.md`), 2026-11-17 (verbatim)

Scope: read MISSION.md, DECISIONS.md (D-001..D-014, A-1), ACCEPTANCE.md, criteria.yaml, scripts/device_accept.py, scripts/device_hook.py, run.sh, device/tests/test_{harness,docs,unit_cycle}.py. Ran `device_accept.py verify` (exit 0) and the harness suite (24 passed). Did not run `device_accept.py run`.

### Must fix before Phase 1 closes

1. **P1-DOCS can never hold a stable PASS.** `ACCEPTANCE.md` is in P1-DOCS `covers`, but the oracle rewrites its generated table after it takes the fingerprint: the PASS row changes a covered file, so the criterion turns STALE; `status` then writes STALE, which restores the hash, so the true state is PASS while the table says STALE. `verify` fails every round and the commit gate blocks every commit. The SessionStart hook runs `status`, which writes `ACCEPTANCE.md`, so every session starts dirty. Why: Phase 1 cannot close green, and an always-red gate trains people to bypass it. Change: fingerprint `ACCEPTANCE.md` with the generated block removed; make `status` read-only (only `run` writes); add a harness test where a passing criterion covers its own table.
2. **A recorded PASS is trusted and can be forged.** `status` exit 0 is the stop condition in `run.sh` and trusts `test-results.json`; the checksum is an unkeyed sha256, so three lines calling `fingerprint()` and `_checksum()` forge any PASS. `git_head_at_run` is recorded but never checked (A-1 §7 asked for results bound to the commit SHA and a clean tree). Why: a builder optimising against the oracle can "finish" without running anything — the 56 % failure mode. Change: add `device_accept.py confirm`: refuse a dirty tree, create a clean `git worktree` at HEAD, ignore recorded results, re-run every criterion, print the HEAD SHA. `run.sh` stops, and phases close, only on `confirm`; keep `test-results.json` as a cache for the commit gate.
3. **Code that runs inside pytest is neither fingerprinted nor controlled.** A `conftest.py` at the root or under `device/`; `pyproject.toml` addopts (`-p envrec_timeout` from `testplugins/`); any entry-point plugin in `.venv`; `PYTEST_ADDOPTS`/`PYTEST_PLUGINS` inherited through `os.environ`. Any of these can deselect failing tests (deselected tests never appear in the JUnit report; the oracle only checks `tests == 0`), rewrite reports, or overwrite `runs/acceptance/<id>.xml`. None is in a P2–P6 fingerprint. Why: one added conftest turns every criterion green. Change: hermetic run — `-c device/acceptance/pytest.ini` (no addopts), `--rootdir=.`, `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, strip `PYTEST_*`; fail on any unpinned `conftest.py` on a test's path; add oracle, ini and conftests to every fingerprint; `min_tests` per criterion; record passed node ids. Harness test per attack (conftest deselect, `PYTEST_ADDOPTS=-k`, xfail, plugin).
4. **No saboteur controls exist yet.** A-1 §7 makes saboteur patches mandatory for all phases; ACCEPTANCE names them only for Phase 2 and the oracle has no mechanism. Tests that `return` early when QEMU/Renode/a VM image is missing pass silently. Change: a `saboteurs:` field (patch files) per criterion, applied in a temp copy, each must FAIL; P1-UNIT saboteurs for signature check, nonce deletion, summary re-derivation, TTL. A `requires:` field (tools/images) checked by the oracle, FAIL when missing.
5. **The P1-UNIT cycle evidence can be forged.** `test_unit_cycle.py` accepts any commit where the test file exists without the implementation (an empty file qualifies), never runs the tests at the red commit, does not stop later weakening; the evaluator report only needs `VERDICT: PASS` and is not bound to the evaluated code. Change: run the red commit's `test_approval.py` against the red tree (must fail) and against HEAD's implementation (must pass); the report carries the P1-UNIT fingerprint, checked equal to the current one.
6. **The criteria are incomplete and their numbers are not pinned.** Missing: Phase 2 — the local model actually answers from the read-only medium; portable memory in an open format round-trips and is encrypted at rest; the gate accepts only the paired key_id and MAS `chat_choice` is absent from the device build. Phase 3 — report readable by the owner and consumable by the agent; no credentials collected (planted-secret fixtures must not appear in the report or model context). Phase 4 — documented secure-boot design. Phase 5 — enclosure fit check. Phase 6 — manual-README baseline, error bars, outside judge, LaTeX source, authors, arXiv variant. Repo-wide — secret scan. Unpinned: P3 thresholds (no field in `criteria.yaml`), k, fuzz count, residue-allowlist path, minimum number of VMs. P2-ZEROTRACE's title ("empty outside approved writes") contradicts D-010. Change: add these criteria or tests and `thresholds:` fields now; retitle P2-ZEROTRACE to the D-010 wording.
7. **Host-owner consent and rule 7 authorization are undefined.** Rule 4.1.1 needs an explicit action by the host machine's owner, but the button is on the device; D-005/D-006 never say whose press counts when the device owner is not the host owner. Rule 7's "solo máquinas propias o autorizadas de forma explícita" has no mechanism or test. Why: a device owner could approve writes or a fleet join on a borrowed machine. Change: a decision that the host owner's launch is the session consent and shows them the terms; JOIN_FLEET also needs a host-side confirmation recorded in the log; test both under P2-FLEET.
8. **Unattended spending.** `run.sh` defaults to `--advisor fable`, billed to usage credits without asking (NEEDS_HUMAN #1); conflicts with MISSION 4.2 "no compres nada" in an unattended loop. Change: default `MAS_ADVISOR=off` until Josué answers.

### Should fix later

- Commit gate fails open (any exception returns 0), matches only `Bash` (this host also has PowerShell), and `git ci` aliases or `sh -c` get past the regex. Use `Bash|PowerShell`, fail closed, treat `confirm` as the real guard.
- Covers are incomplete: P1-DOCS reads `NEEDS_HUMAN.md`; P1-HARNESS reads `MISSION.md`, `PROGRESS.md`, `run.sh`, `device_hook.py`; none covered. The criterion's own definition is not in its fingerprint (narrowing `tests` keeps the old PASS). Hash the canonical criterion dict; record files the tests open (`sys.addaudithook`).
- P2-LOG title overclaims "model process cannot write it"; tests prove detection only. Retitle or add an OS-separation test.
- A-1 basis gap: the advisor did not read `policy.py`, PROVISIONING_SAFETY.md or NETWORK_CONTRACT.md, yet D-003 reuses "the policy function". Have the reviewer read them before P2-GATE work.
- Credential collection in inspection: osquery `shell_history`, process command lines and environment, `authorized_keys`. Ship a table/field denylist enforced in code.
- INSPECT_SESSION scope (D-012) unbounded: sign the collector list and TTL inside the action.
- Oracle robustness: no `subprocess.run` timeout, no network-off run, no run-time seeds (A-1 §7).
- Weak harness tests: `test_hand_edit_is_detected` only shows a sha256 changes; no tests for an out-of-date table, xfail/xpass, fixture errors, `pytest.exit`. Test through `main(["verify"])` on a temp root.
- Emulator claims in D-007 come from memory: confirm QEMU `mps2-an505` and Renode STM32U5/L552 before Phase 4 code.
- "Evaluator cannot write" is half true (Bash can write): word it as "no Write/Edit tools; writes only under `runs/eval/`".

### Fine as is

D-004 approval token (fixed-width signed struct, pure Ed25519 with monocypher-ed25519 named, in-memory nonce, gate-clock TTL, session binding, classes, re-derived signed summary, pre-state hash). D-003/D-008/D-010/D-011 (key + host compute; Pi emulated only, Jetson cut; display yes, ATECC608B no; honest "no trace" with three-arm diffs; inspection cuts). D-006 maps every rule to a mechanism and a criterion. Oracle core: default FAIL; missing, zero collected, import errors, failures, skips and xfail are FAIL; fingerprint normalises CRLF and includes the test files. Labels carry REAL/SIMULATED/EMULATED/DESIGN-ONLY.

### R-1 resolutions (builder; P1-DOCS stays FAIL while any line says OPEN)

- M1: OPEN
- M2: OPEN
- M3: OPEN
- M4: OPEN
- M5: OPEN
- M6: OPEN
- M7: OPEN
- M8: RESOLVED — `run.sh` advisor is opt-in (`MAS_ADVISOR` default off); device/tests/test_harness.py::test_unattended_loop_spends_no_advisor_credits_by_default

## A-2 Advisor — Fable 5.2, 2026-11-17: firmware core spec for P4-FW-HOST (condensed faithfully; builder follows it)

1. **Modules** (`device/firmware/`, all static, no malloc/stdio in core): `core/mas_core.{c,h}` state machine (`mas_init`, `mas_rx_byte`,
   `mas_button_isr`); `core/mas_frame.c` COBS + CRC16; `core/mas_nv.c` counter log and key page, the only caller of flash program/erase;
   `core/sha256.c` own (~70 lines, tested against hashlib; Monocypher has no SHA-256); `core/mas_hal.h` (`hal_tick_ms`, `hal_tx`,
   `hal_display(summary[32], next_counter)` — all-zero clears, `hal_trng`, `hal_flash_ptr/program/erase`); `host/hal_host.c`, `host/main_host.c`.
   The button is a HAL→core call only; nothing in the transport path reaches it. The 146-byte request is exactly body bytes 31..176.
2. **Wire**: `COBS(op | payload | crc16_be) 0x00`, CRC-16/CCITT-FALSE (`binascii.crc_hqx(d, 0xFFFF)`); responses `COBS(status | payload | crc16) 0x00`;
   RX overflow discards to the next 0x00 and replies BAD_FRAME. **Opcode allowlist = {0x01 GET_INFO, 0x02 REQUEST (146 B), 0x03 SIGN (seq u32)}.**
   Status: 0 OK, 1 MALFORMED, 2 SUMMARY_NOT_PRINTABLE, 3 STALE_REQUEST, 4 NO_PRESS, 5 PRESS_WINDOW, 6 UNKNOWN_OPCODE, 7 BAD_FRAME, 8 NV_FAULT.
   REQUEST checks in keymodel order (length → printable → cls 1..7 / pw ≠ 0); a failed request changes nothing; success: seq++, clear the
   latch, store t_req, show the display. SIGN: no pending / seq mismatch → STALE (latch untouched); take+clear latch (interrupts masked), none →
   NO_PRESS and keep pending; clear pending and display; `(uint32_t)(press.t − t_req) > min(pw,30)*1000` → PRESS_WINDOW (strict, press time
   not SIGN time); commit counter+1 → NV_FAULT on failure; build body, sign, transmit.
3. **Counter**: two flash pages, append-only 16-byte records `counter u64 LE | ~counter u64 LE` (a torn record fails the complement check;
   NOR only clears bits). Boot: max valid record over both pages, else 0. Write the slot after the last non-erased slot of the active page;
   never program a non-0xFF slot. Page full: erase the other page, then write there (the max survives in the old page during the erase).
   Commit = program, read back, compare, and only then `crypto_ed25519_sign`. Host sweep: mock flash file with AND-on-program, flushed per byte;
   `MAS_CUT_AFTER=k` exits 77 after k programmed bytes or mid-erase (variants untouched / all 0xFF / garbage); sweep over a full sign including a
   page rollover; assert counter ∈ {N, N+1}, ≥ any emitted token's counter, next token's counter > every counter ever emitted.
   Unsure: STM32U5 flash ECC may NMI on a torn word; program granularity not remembered; 16-byte aligned records are safe; label not verified.
4. **Key**: key page `seed[32] | pub[32] | crc16`; first boot with an erased page: TRNG seed, key pair on a copy, write, continue. Every boot:
   derive sk, `key_id = sha256(pub)[:8]`. No opcode reads, writes or regenerates the key. Host build SIMULATED: flash is a file, `hal_trng`
   reads `MAS_TRNG_FILE`; no TrustZone, RDP or real entropy.
5. **Tests** (`device/tests/test_firmware_host.py`): host binary on stdin with tagged records (`F` frame bytes, `P` press, `T` tick, `D`
   display dump; only `F` reaches `mas_rx_byte`). Vectors (sha256, CRC, COBS; key_id); differential ~200 seeded scripts against KeyModel
   (tokens byte-equal, codes equal, display equal); rejections (no press, press before request, window+1 ms rejected / exactly window accepted,
   pw 255 capped at 30, second SIGN stale, newer request stales the old seq, one press one token, summary 0x1F/0x7F/0x80, cls 0 and 8, pw 0,
   145/147-byte payloads); replay: the key has no replay defence by design — same request twice needs two presses, two counters, and
   `Gate.consume` rejects the second; frames (bad CRC, bad COBS, oversize, empty, all 253 unknown opcodes); display = `token[143:175]`;
   immutability (10k random frames without a press: flash hash unchanged, zero tokens; opcode enum = {1,2,3}; `hal_flash_*` only in mas_nv.c);
   power-cut sweep and NV_FAULT emits no token; libFuzzer target (Linux container) plus an always-on deterministic Python fuzz; saboteurs
   (no latch clear on request, sign before commit, no printable check) must each turn the suite red.
6. **Pitfalls**: `crypto_ed25519_key_pair` wipes the seed (pass a copy; sk = seed‖pk); never `crypto_eddsa_*` (BLAKE2b); wipe temporaries;
   signing stack is several KB (measure on QEMU). zig cc on Windows: `_setmode(..., _O_BINARY)` on stdin/stdout and fflush per frame; debug
   builds trap on UBSan (use -O2, sanitizers as a separate run); `-Werror` on core only; libFuzzer/ASan on Windows uncertain — run in Linux;
   serialise byte by byte, no struct casts.

## A-3 Advisor — Fable 5.2, 2026-11-17: key board spec for P5-PCB (condensed faithfully) — amends D-009

**Amendment to D-009.** No Docker on this host, KiCad installed natively (winget gave **KiCad 10.0.8**, not 9). Zener/atopile/KiBot are
replaced by: one Python source of truth `device/hardware/design.py` (parts + pin-NAME→net map; MCU pin numbers are resolved from the stock
symbol, never hardcoded) → `gen_sch.py` writes a real `.kicad_sch` (stock symbols embedded, a global label at every pin end, no_connect on
unused pins, PWR_FLAG on GND and VBUS, deterministic uuid5 UUIDs) → `kicad-cli sch export netlist` → `gen_pcb.py` under KiCad's bundled
Python builds the board **from that netlist** (parity by construction) with placement from design.py → routing without Java: GND only through
pours (solid B.Cu + F.Cu) with a via beside each GND pad; other nets by a small deterministic two-layer grid router; zone fill; DRC judges.
Every lib:name came from memory: the generator must assert each exists in the installed libraries.

**Parts.** U1 STM32U585CIT6 (LQFP48, LDO variant, not "Q"); J1 GCT USB4105-GF-A USB-C (USB 2.0); U2 USBLC6-2SC6; U3 AP2112K-3.3 (≈250 mV
dropout at 600 mA, load < 50 mA); SW1 C&K PTS645; J2 1x4 2.54 mm OLED socket; J3 2x5 1.27 mm SWD (Cortex Debug: 1 VTref, 2 SWDIO, 3/5/9 GND,
4 SWCLK, 6 SWO, 10 NRST); D1 0603 LED; 0603 passives. R1/R2 5.1k CC1/CC2→GND separately; 1 µF on VBUS (total ≪ 10 µF); LDO out 1 µF + 4.7 µF;
100 nF per VDD, VDDUSB, VBAT (tied to +3V3); VDDA 100 nF + 1 µF to +3V3 (ADC unused; flag); VCAP 4.7 µF per VCAP pin (UNVERIFIED for the LDO
variant: check AN5373); BOOT0 (PH3) 10k→GND; NRST 100 nF→GND; button to GND, 10k pull-up, 1k + 100 nF RC into the MCU (open reads "not
pressed"); I²C 4.7k pull-ups (modules may carry their own; flag); LED 1k. ATECC608B DNP footprint only if ERC stays clean.
**Clock:** no crystal; HSI48 + CRS on USB SOF (confident for U575/U585 USB FS device); PH0/PH1/PC14/PC15 no-connect.

**Pin map** (functions high confidence; every LQFP48 pin number UNVERIFIED): USB_DM/DP PA11/PA12; SWDIO/SWCLK PA13/PA14; SWO PB3;
I2C1 SCL/SDA PB6/PB7 (AF4); BTN PA0 (EXTI0/WKUP1, not a strap, TrustZone-assignable); LED PA8; BOOT0 PH3; NRST; VDD, VSS, VDDA/VSSA,
VDDUSB, VBAT, VCAP.

**Board.** 20 × 48 mm, 2-layer 1.6 mm FR4 1 oz, parts on F.Cu. Rules above JLCPCB minimums: clearance 0.15, signal 0.2, power 0.4, via
0.6/0.3, copper-to-edge 0.3, hole-to-hole 0.5 (mm). GND pours both layers with stitching. Order along the long axis: J1 at the edge → U2 within
3 mm → R1/R2 → U3 + caps → U1 centre with decoupling at each pin → SW1 → J2 far end; J3 and D1 on the long edge. USB D± short, paired, top
only, over unbroken bottom GND; no impedance control (flag). Silk `NO FABRICAR sin revisión humana` on both sides.

**Tests (P5-PCB).** ERC `--severity-all --exit-code-violations` JSON zero; DRC `--schematic-parity --severity-all --exit-code-violations`
with zero violations, zero unconnected, zero parity issues; no exclusions or lowered severities in `.kicad_pro`, capped no_connect count,
rules ≥ fab values; generators deterministic (byte-identical rerun); gerbers, drill, pos, BOM exported by kicad-cli, each stamped and
suffixed `_NO_FABRICAR`, no zip; BOM rows have MPNs, `bom_snapshot.json` dated with source and stock/lifecycle per MPN, and says so if no
live lookup was done (no invented stock numbers); HUMAN_REVIEW_PCB.md has the ten items, the UNVERIFIED list and DESIGN-ONLY; netlist checks
(every VDD*/VDDA/VDDUSB/VBAT/VCAP pin has a cap to GND, BTN has exactly one IC pin, CC1/CC2 distinct with one 5.1k each, BOOT0 10k, NRST
100 nF, D± only U2 and U1, one pull-up each on SCL/SDA); saboteurs: delete a track → DRC unconnected; change a pad net → parity fails;
remove a decoupling cap → net check fails.
