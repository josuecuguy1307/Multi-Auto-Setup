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
| pytest | https://pypi.org/project/pytest/ | MIT (PyPI) | n/a | dependency | in use |
| PyYAML | https://pypi.org/project/PyYAML/ | MIT (PyPI) | n/a | dependency | in use |
| httpx | https://pypi.org/project/httpx/ | BSD-3-Clause (PyPI) | n/a | dependency | in use |
| packaging | https://github.com/pypa/packaging | Apache-2.0 OR BSD-2-Clause (LICENSE.APACHE and LICENSE.BSD read in the 26.3 distribution) | release 26.3 | dependency of pytest; read by `tests/test_packaging.py` | in use |
| bleak | https://github.com/hbldh/bleak | MIT (LICENSE read in the 3.0.2 distribution) | release 3.0.2 | optional extra `discovery` (Bluetooth), imported lazily | in use when installed |
| pyserial | https://github.com/pyserial/pyserial | BSD-3-Clause (the package's own classifier; its distribution carries no license file) | release 3.5 | optional extra `discovery` (serial), imported lazily | in use when installed |
| zeroconf | https://github.com/python-zeroconf/python-zeroconf | LGPL-2.1-or-later (LICENSE read in the 0.151.5 distribution) | release 0.151.5 | optional extra `discovery` (mDNS), imported lazily; not copied into this repository or bundled | in use when installed; the owner installs it, and its LGPL terms apply to it |
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
