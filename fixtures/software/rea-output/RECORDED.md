# Recorded REA outputs

Real output of `rea-agents` **6.7.0** (npm, MIT) installed into `scout/rea-run/` with `--ignore-scripts`, run on this
macOS 15.7.1 / Apple M3 machine with Node v22.20.0. Files are the unmodified stdout of each command. They contain
this machine's absolute paths (`subject.local_path`); that is real output, left as it was.

Each command ran through `scripts/rea_monitored.sh`: isolated `HOME` (`scout/rea-home`), an empty working directory,
and a sampler that looked for network sockets held by the REA process tree every 100 ms. **No socket was ever seen
(4 to 20 samples per command).** This observes, it does not enforce; see `docs/mapping_cards/rea.md`.

| File | Command | Exit | Target | Notes |
|---|---|---|---|---|
| `js_app.json` | `rea analyze-javascript-application <dir> --json` | 0 | `fixtures/software/tiny-notes-app` | 594 KB |
| `macho.json` | `rea inspect-macho <file> --json` | 0 | `fixtures/software/hello` | |
| `artifact_bin.json` | `rea inspect-artifact <file> --json` | 0 | `fixtures/software/hello` | |
| `artifact_app.json` | `rea inspect-artifact <dir> --json` | 1 | `fixtures/software/tiny-notes-app` | REA's own error: directories go to analyze_javascript_application |
| `capabilities.json` | `rea capabilities --json` | 0 | none | which providers and operations exist on this machine |
| `doctor.json` | `rea doctor --json` | 1 | none | unhealthy here: no Hopper, Ghidra, IDA, JADX or Kuna configured |

A first attempt ran REA under macOS `sandbox-exec`. It failed for both `analyze-javascript-application` and
`inspect-macho` with `cleanup_incomplete / spawn EPERM`: REA's cleanup runs `ps`, and `ps`, `sysctl` and `lipo` cannot be
launched from a `sandbox-exec` process here (even with a bare `(allow default)` profile). Those failed outputs are not kept.
