# THIRD_PARTY

Role: dependency | transplanted | design-only | data | not evaluated. License is as read in the repo's own LICENSE file unless noted. Nothing is transplanted into `src/` so far.

| Name | Link | License | Commit SHA | Role | Verdict |
|---|---|---|---|---|---|
| pydantic | https://pypi.org/project/pydantic/ | MIT (PyPI) | n/a | dependency | in use |
| psutil | https://pypi.org/project/psutil/ | BSD-3-Clause (PyPI) | n/a | dependency | in use |
| cryptography | https://github.com/pyca/cryptography | Apache-2.0 OR BSD-3-Clause (LICENSE.APACHE and LICENSE.BSD read in the 50.0.2 sdist) | release 50.0.2 (Intel macOS: 48.x; Windows arm64: 46.x, the newest with a wheel) | dependency | in use: Ed25519, certificates, HKDF; platform pins in pyproject.toml |
| spake2 | https://github.com/warner/python-spake2 | MIT (LICENSE read in the 0.9 sdist) | release 0.9 | dependency | in use: the pairing key exchange; not constant-time per its README |
| cffi | https://github.com/python-cffi/cffi | MIT text in its LICENSE file (package metadata says MIT-0) | release 2.1.1 | dependency of cryptography | in use |
| (standard library) `http.server`, `http.cookies`, `secrets`, `hmac` | Python standard library | PSF license (part of Python) | the interpreter's version | dependency | in use: the owner's web screen (Phase 1 step 1b) adds NO third-party package; the page is plain HTML, CSS and JS with system fonts, nothing from a CDN |
| pycparser | https://github.com/eliben/pycparser | BSD-3-Clause (LICENSE read) | release 3.12 | dependency of cffi | in use |
| keyring | https://github.com/jaraco/keyring | MIT (PyPI license expression, 25.7.0) | release 25.7.0 | dependency: the node's OS credential store (secret entry). Wheel `py3-none-any`, all targets | in use |
| pywin32-ctypes | https://pypi.org/project/pywin32-ctypes/ | BSD-3-Clause (PyPI, 0.2.3) | release 0.2.3 | dependency of keyring on Windows (Credential Manager). Wheel `py3-none-any` | in use |
| SecretStorage | https://pypi.org/project/SecretStorage/ | BSD-3-Clause (PyPI, 3.5.0) | release 3.5.0 | dependency of keyring on Linux (Secret Service over D-Bus). Wheel `py3-none-any`; needs `cryptography`, already a dependency | in use |
| jeepney | https://pypi.org/project/jeepney/ | MIT (PyPI, 0.9.0) | release 0.9.0 | dependency of keyring and SecretStorage on Linux (D-Bus). Wheel `py3-none-any` | in use |
| jaraco.classes, jaraco.functools, jaraco.context | https://pypi.org/project/jaraco.classes/ | MIT (PyPI: 3.4.0, 4.6.0, 6.1.2) | releases 3.4.0, 4.6.0, 6.1.2 | dependencies of keyring on every OS. Wheels `py3-none-any` | in use |
| more-itertools | https://pypi.org/project/more-itertools/ | MIT (PyPI, 11.2.1) | release 11.2.1 | dependency of jaraco.classes and jaraco.functools. Wheel `py3-none-any` | in use |
| importlib_metadata, zipp, backports.tarfile | https://pypi.org/project/importlib-metadata/ | Apache-2.0, MIT, MIT (PyPI) | 10.1.0, 4.1.2, 1.2.0 | pulled by keyring and jaraco.context only below Python 3.12 (this project requires 3.11 or later). Wheels `py3-none-any` | in use on 3.11 |
| pytest | https://pypi.org/project/pytest/ | MIT (PyPI) | n/a | dependency | in use |
| PyYAML | https://pypi.org/project/PyYAML/ | MIT (PyPI) | n/a | dependency | in use |
| httpx | https://pypi.org/project/httpx/ | BSD-3-Clause (PyPI) | n/a | dependency | in use |
| packaging | https://github.com/pypa/packaging | Apache-2.0 OR BSD-2-Clause (LICENSE.APACHE and LICENSE.BSD read in the 26.3 distribution) | release 26.3 | dependency of pytest; read by `tests/test_packaging.py` | in use |
| bleak | https://github.com/hbldh/bleak | MIT (LICENSE read in the 3.0.2 distribution) | release 3.0.2 | optional extra `discovery` (Bluetooth), imported lazily | in use when installed |
| pyserial | https://github.com/pyserial/pyserial | BSD-3-Clause (the package's own classifier; its distribution carries no license file) | release 3.5 | optional extra `discovery` (serial), imported lazily | in use when installed |
| REA (`rea-agents`) | https://github.com/morluto/rea | MIT | d87e73f2db50be72d33a4b3c7199af35518b441c | tool run via CLI, not transplanted (npm rea-agents 6.7.0, installed with --ignore-scripts) | used; mapping card verdict: lossy (docs/mapping_cards/rea.md) |
| buspirate-mcp | https://github.com/mplogas/buspirate-mcp | MIT | 36589d5d9fbcb3018f8568a88cdad04a7c434299 | design-only | tier idea, rewritten |
| BootstrapAgent | https://github.com/Vossera/BootstrapAgent | MIT | a5ea466b23534d20baa97f12f3ec1f0071a38ac5 | design-only now, transplant later | verifier phase |
| Voyager | https://github.com/MineDojo/Voyager | MIT | 55e45a880755d0c8c66ca7fb5fe7962ac8974f89 | design-only | skill library pattern |
| Repo2Run | https://github.com/bytedance/Repo2Run | Apache-2.0 | 65042aa2d69a13746d7cd94ef1c5a29fcd19a692 | design-only now, transplant later | sandbox phase |
| open_deep_research | https://github.com/langchain-ai/open_deep_research | MIT | 1b7d2e80db9faa586165c60e09096dbbfd483a64 | design-only | swarm phase |
| smolagents | https://github.com/huggingface/smolagents | Apache-2.0 | 96f33faaf028479119ec8d34507b47694cf14e34 | not used | skip |
| pydantic-ai | https://github.com/pydantic/pydantic-ai | MIT | e3aba7adf6b9e4b952fc664c4ba9ae5ef1858a38 | not used | skip |
| RepoLaunch | https://github.com/microsoft/RepoLaunch | MIT | 099857f37e87e7bf09a2baa1c78809518c485c53 | not evaluated | license read only |
| awesome-mcp-hardware | https://github.com/beriberikix/awesome-mcp-hardware | Apache-2.0 | 50e72818ac70be677fe9c982abba2d77bb97925c | reference list | read |
| ble-mcp-server | https://github.com/es617/ble-mcp-server | MIT per GitHub API (LICENSE file not read) | not cloned | not evaluated | Ring 1 later |
| serial-mcp | https://github.com/bmdragos/serial-mcp | MIT per GitHub API | not cloned | not evaluated | Ring 1 later |
| arduino-mcp-server | https://github.com/hardware-mcp/arduino-mcp-server | MIT per GitHub API | not cloned | not evaluated | Ring 1 later |
| mcpd | https://github.com/quantumnic/mcpd | MIT per GitHub API | not cloned | not evaluated | Ring 1 later |
| sigrok-mcp-server | https://github.com/KenosInc/sigrok-mcp-server | MIT per GitHub API (sigrok data license unconfirmed) | not cloned | not evaluated | Ring 1 later |
| instrument-mcp | not found | unknown | n/a | not evaluated | not found |
| Octopus Protocol | https://arxiv.org/abs/2605.09055 | paper | n/a | design reference | read title only |
| CAI | not touched | research-only (per notes) | n/a | do not use | never |
| GhidraMCP | not touched | none stated (per notes) | n/a | do not use | never |
| OpenCode | https://github.com/anomalyco/opencode (was sst/opencode) | MIT (LICENSE read at the release commit) | release v1.19.5, commit 810c0eba1a7348036f8bdf61e6632d0c36c6876c; per-OS asset SHA-256 in `skills/router-environments/references/environments.json` (as GitHub reports them, not recomputed) | own-harness environment, installed only by a T3a-format recipe with owner approval; nothing installed yet | catalog entry `opencode` |
| Codex (CLI) | https://github.com/openai/codex | Apache-2.0 (GitHub SPDX, 2026-11-02) | release rust-v0.166.0, commit 67f0994b532b8740f14b5b738af7b7b2a915b1d6; per-OS asset SHA-256 in `skills/router-environments/references/environments.json` (as GitHub reports them; the macOS arm64 asset was downloaded and hashed on 2026-11-02) | a provider environment the harness can install into its own folder after the owner approves the plan; never bundled | catalog entry `codex` |
| Goose | https://github.com/aaif-goose/goose (block/goose redirects here) | Apache-2.0 (GitHub SPDX, 2026-11-02) | release v1.57.0, commit 30bba95105e59bc3e7df624cf6b4d433690cfe12; per-OS asset SHA-256 in `skills/router-environments/references/environments.json` (macOS arm64 asset downloaded and hashed on 2026-11-02) | own-harness environment, added as data only; installed into its own folder after the owner approves the plan; never bundled | catalog entry `goose` |
| Contract review skill (pack) | https://github.com/evolsb/claude-legal-skill | MIT (GitHub SPDX and its LICENSE file, which the pack carries) | commit bca0d2e9d5ec81298ddfbfde78fa4f38fb4589b0; three files pinned by raw address at that commit and by SHA-256 (fetched 2026-11-02) | a file-based skill pack: instructions an agent reads, placed in the pack's own folder after the owner approves the plan; never bundled, never run | catalog entry `legal-contract-review-skill` |
| Pi | https://github.com/earendil-works/pi (was badlogic/pi-mono) | MIT (LICENSE read at the release commit; registry says MIT) | release v1.4.0, commit 4a080252f06caf63a525d7873c5d94d3c23887e0; npm @earendil-works/pi-coding-agent 1.4.0, integrity sha512-Z41LpZ3yYHSC/UUps6Mr39UMFPwH8elhJBG9DY1P7kuzsE5qSK/6eMj2EHzO5evJRQWLbKSqUMw+4nCNtIyiQg==; dependencies' licenses not yet checked | own-harness environment, runs only inside a sandbox backend; nothing installed yet | catalog entry `pi` |
| Codex CLI | https://github.com/openai/codex | Apache-2.0 (LICENSE read at the pinned commit) | 67f0994b532b8740f14b5b738af7b7b2a915b1d6 (seen release rust-v0.166.0) | provider environment: the owner's own installed, signed-in tool; never installed, bundled or copied by us | catalog entry `codex`; terms for automated use to review |
| Claude Code CLI | https://code.claude.com/docs/en/cli-reference | closed, commercial | n/a | provider environment: the owner's own installed, signed-in tool; never installed, bundled or copied by us | catalog entry `claude-code`; terms for automated use to review |
| Muse Code (Meta) | https://dev.meta.ai/docs/muse-code | not stated in the docs read | n/a | provider environment: the owner's own installed tool; never installed, bundled or copied by us | catalog entry `muse-code`; terms to review; API key variable per the official docs is META_API_KEY |

Public data files (usb.ids, OUI, Bluetooth SIG, mDNS) are listed in `DATA_SOURCES.md`.

## Data files (role: data, never committed; details in DATA_SOURCES.md)

| Name | Link | License | Fetched sha256 (prefix) | Verdict |
|---|---|---|---|---|
| usb.ids | http://www.linux-usb.org/usb.ids | GPL-2.0-or-later OR BSD-3-Clause (per http://www.linux-usb.org/usb-ids.html) | f5a48b0c | used, git-ignored |
| IEEE OUI list | https://standards-oui.ieee.org/oui/oui.csv | license unconfirmed | 71cf77fb | used locally, git-ignored |
| bluetooth-numbers-database (Nordic) | https://github.com/NordicSemiconductor/bluetooth-numbers-database | BSD-3-Clause (its LICENSE file; the research log said MIT, which was wrong). Derived from Bluetooth SIG data, whose own license is unconfirmed | 6b2aff96, fec38d7a | used, git-ignored |
| IANA service names | https://www.iana.org/assignments/service-names-port-numbers/ | license unconfirmed | 3837683c | used locally, git-ignored |

## Skill-format references (read as patterns, cloned into git-ignored `scout/`)

| Name | Link | Commit | License | What was used |
|---|---|---|---|---|
| anthropics/skills | https://github.com/anthropics/skills | dbd4588f9e1033efb41dad4bef2f7947c8993d44 | per skill: `skill-creator` is Apache-2.0 (its `LICENSE.txt`); repo-level notices in `THIRD_PARTY_NOTICES.md`; every other skill has its own `LICENSE.txt` and was NOT copied | read `template/SKILL.md`, `skills/skill-creator/SKILL.md`, `scripts/quick_validate.py`, `references/schemas.md`; patterns only, no file copied |
| obra/superpowers | https://github.com/obra/superpowers | 2ce8aa15d05bd57213e6f746f953aafb7ded7a8b | MIT (Copyright (c) 2025 Jesse Vincent) | read `skills/writing-skills/SKILL.md` and its `references/`; the baseline-first method and "description says only when to use" rule are adopted as ideas, with attribution here |
| Agent Skills specification | https://agentskills.io/specification | fetched on read | specification text, not copied | frontmatter rules implemented exactly in `src/envrec/skills/loader.py` |
| MCP Apps SDK (`@modelcontextprotocol/ext-apps`) | https://github.com/modelcontextprotocol/ext-apps | MIT (npm registry, 2.0.4) | n/a | design-only | the `postMessage` protocol the cards speak in Claude Desktop was written by hand from the published specification (stable 2026-01-26); no code and no package is used |
| Open WebUI | https://github.com/open-webui/open-webui | "Open WebUI License": BSD-3-Clause with a branding clause that binds deployments of 50 or more users (read in the repository LICENSE) | PyPI 0.12.5 | host, not a dependency | the owner installs it separately; nothing of it is bundled, copied or imported: the Pipe is a file the owner pastes into its Functions |

## Credential store dependencies (phase4d, read from PyPI metadata on 2026-10-31)

All new packages are pure Python (`py3-none-any`), so one wheel covers Windows (amd64, arm64), macOS (arm64, x86_64) and Linux (x86_64, arm64). Licenses are MIT or BSD-3-Clause, plus Apache-2.0 for `importlib_metadata` (Python 3.11 only). macOS needs no extra package: keyring reaches the Keychain with the system library. The compiled dependency of SecretStorage, `cryptography`, was already pinned per platform in `pyproject.toml`. Nothing was run on Windows or Linux (UNTESTED there).

Known limits (documented, not hidden):
- **No plaintext fallback, ever.** If keyring resolves to its fail or null backend (for example a headless Linux machine with no Secret Service running), secret entry refuses with the typed reason `credential_store_missing`.
- **macOS Keychain prompt.** An item is tied to the program that created it. When the Python binary changes (a new virtual environment, a `uv` interpreter update, `mas update`), macOS can ask the owner to allow the new binary to read the item, or the read can fail until the owner allows it. Answer with the account password in the system dialog; the harness never sees it.
- `typing-extensions` (PSF-2.0, via `cryptography`) is only pulled below Python 3.11, which this project does not support.

## MAS portable device (branch `device/v0`)

Device code (`src/mas_device/`, `device/`) adds **no new dependency** so far: it uses `cryptography` (Ed25519, already above) and PyYAML.
The repos in MISSION.md section 8.2 are **not cloned or evaluated yet**. Their licenses are left unconfirmed here until each one is read
from its own LICENSE file when it is cloned (into git-ignored `scout/`, read before anything runs, run only in a container).

| Name | Link | License | Role | Verdict |
|---|---|---|---|---|
| Monocypher 4.0.2 (+ optional monocypher-ed25519) | https://monocypher.org (tarball sha256 38d07179…0879c) | BSD-2-Clause OR CC0-1.0 (read: LICENCE.md) | vendored unmodified in device/firmware/vendor/monocypher (pure Ed25519 for the key firmware, D-007/A-2) | read before use: pure computation, no I/O; Ed25519 checked against `cryptography` |
| ziglang 0.13.0 (PyPI, Zig's `zig cc`) | https://pypi.org/project/ziglang/ | MIT | C compiler for the firmware host build, installed only in runs/toolchain (git-ignored) | build tool, not shipped |
| tillitis-key1 / tk1-pcba | https://github.com/tillitis/tillitis-key1 | to read at clone | design-only: firmware-hash-derived key identity idea (D-007); no PCB derived | not cloned |
| SoloKeys, USB Armory, tamago, armory-boot | see MISSION 8.2 | to read at clone | design reference | not cloned |
| pocketllm (vraj00222, Looming41), llama.cpp, project-nomad | see MISSION 8.2 | to read at clone | Phase 2 candidates | not cloned |
| python-build-standalone CPython 3.13.18 (release 20261110), linux x86_64 install_only | https://github.com/astral-sh/python-build-standalone | CPython PSF-2.0 (`lib/python3.13/LICENSE.txt` kept on the medium); the build statically links third-party libraries whose licence texts this variant does not carry | the medium's own interpreter (P2-PORTABLE, D-034), pinned by sha256 in device/medium/medium.lock.json; pruned at build; run only in containers | open: collect the bundled libraries' licences before any medium is distributed |
| cryptography 50.0.2, cffi 2.1.1, pycparser 3.12 (manylinux x86_64 wheels) | https://pypi.org | Apache-2.0 OR BSD-3-Clause; MIT; BSD-3-Clause (licence files kept in the unpacked dist-info) | vendored into the medium's runtime/site, pinned by sha256 | wheels unpacked, not modified |
| debian:bookworm-slim (Docker Hub, digest 734a72b3...b179bc) | https://hub.docker.com/_/debian | Debian (DFSG) | the clean host for P2-PORTABLE: no Python, no network | test fixture only |
| portable-memory (MacPaw) `.mem` format v1.0 | https://github.com/MacPaw/portable-memory | MIT (per its README, 2026-11-19) | P2-MEMORY's open format: `manifest.json` + `items/<kind>.jsonl` + `CHECKSUMS`, unknown kinds/fields verbatim, secretRef metadata only (D-033) | format followed from the README; no code copied, not cloned; its validator not yet run against our export |
| portable-ai-memory | https://github.com/portable-ai-memory/portable-ai-memory | spec CC BY 4.0, code Apache-2.0 (per its README) | considered for P2-MEMORY: single JSON file, no encryption | not used (D-033) |
| pi (earendil-works), pi-continual, prime-agent | see MISSION 8.2 | to read at clone | harness candidates (MAS already lists `pi` as an environment) | not cloned |
| Syft, Grype, osquery | see MISSION 8.2 | to read at clone | Phase 3 candidates (fwupd, EMBA, binwalk, Ghidra cut for v0: D-011) | not cloned |
| KiCad 10.0.8 (`kicad-cli`, bundled Python with pcbnew and numpy) | https://www.kicad.org (winget KiCad.KiCad) | GPL-3.0-or-later (application); stock symbol and footprint libraries CC-BY-SA-4.0 with KiCad's exception for designs that use them | P5-PCB: ERC, DRC, netlist, Gerbers, and the board built by `device/hardware/gen_pcb.py` through pcbnew; run as an external tool, nothing copied into the repo | in use |
| KiBot, Zener (diodeinc/pcb), atopile, Freerouting | see MISSION 8.2 | not read (never installed) | considered for Phase 5 (D-009) | not used: the board is generated by this repo's own `gen_pcb.py` and grid router |
| QEMU 11.1.0 (`qemu-system-arm`) | https://www.qemu.org (winget SoftwareFreedomConservancy.QEMU) | GPL-2.0 | P4-FW-EMU emulator, run as an external tool (no QEMU code in the repo) | installed natively on Josué's host |
| Renode 1.17.0 (portable zip) | https://github.com/renode/renode/releases/tag/v1.17.0 | MIT (bundled components list their own in `licenses/`) | P4-FW-RENODE emulator, external tool; its stock `ARMv8M-TrustZone_mps2.repl` is used in place | unpacked into runs/toolchain/renode (git-ignored) |
| anthropics/claude-quickstarts (autonomous-coding) | https://github.com/anthropics/claude-quickstarts | to read at clone | harness reference only | not cloned |
