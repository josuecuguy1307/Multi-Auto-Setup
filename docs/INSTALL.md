# Install, update, service, unpair, uninstall

Multi Auto Setup installs for ONE user, with no administrator rights and no changes outside that user's folders. It installs this program only: no AI models (choosing and setting those up is the separate setup flow).

## Install

Download, read, run. Nothing here is signed, so reading it first is the point.

**macOS and Linux (x86_64 and arm64; Debian/Ubuntu and Fedora tested, see the matrix):**

```bash
curl -fsSLO https://raw.githubusercontent.com/josuecuguy1307/Multi-Auto-Setup/main/install.sh
less install.sh
sh install.sh
```

**Windows 10/11:**

```powershell
Invoke-WebRequest https://raw.githubusercontent.com/josuecuguy1307/Multi-Auto-Setup/main/install.ps1 -OutFile install.ps1
notepad install.ps1
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

`-ExecutionPolicy Bypass` applies to that one run only and needs no administrator. A script saved by a browser carries the "downloaded from the internet" mark that the default `RemoteSigned` policy refuses; `Invoke-WebRequest` does not set the mark ([Microsoft: about_Execution_Policies](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_execution_policies)). On macOS, `curl` does not set the quarantine flag either, so Gatekeeper does not assess a script fetched this way; a script saved by a browser does get it, which is one more reason to fetch with `curl`.

**One line, if you accept the risk.** `curl -fsSL <url>/install.sh | sh` and `irm <url>/install.ps1 | iex` run whatever the server sends at that moment, without you having read it. Prefer the three steps above. (The shell version is wrapped so a download cut short runs nothing; it reads your answers from the terminal even through a pipe. The PowerShell version takes its options from environment variables when piped: `MAS_HOME`, `MAS_RELEASE_BASE`.)

### What the installer does, and asks

1. Shows the plan: every URL, folder and command, and how to undo each.
2. **uv.** If you do not have it, shows the official command and asks. It is run with `UV_NO_MODIFY_PATH=1`, so uv does not edit your shell files or PATH. ([uv installation](https://docs.astral.sh/uv/getting-started/installation/))
3. **Download and check.** Downloads the release archive of one pinned tag (default: the tag written at the top of the script) and compares its SHA-256 with the value written in the script. Mismatch: it stops, deletes the download, installs nothing. Another tag needs its own `--sha256` / `-Sha256`; the embedded hash is never applied to it and the check is never skipped.
4. **Build.** Unpacks (refusing absolute paths, `..`, links) into `~/.mas/releases/<tag>` (Windows: `%USERPROFILE%\.mas\releases\<tag>`), creates its own Python 3.13 environment with uv and installs the program there. A failure deletes the partial folder.
5. **The `mas` command.** A small script that runs the release named in `~/.mas/current`: `~/.local/bin/mas` on macOS/Linux, `%USERPROFILE%\.mas\bin\mas.cmd` on Windows. It never overwrites a `mas` that is not its own. If that folder is not on your PATH it **shows the exact change and asks**: a marked block in your shell profile (macOS/Linux) or an entry in your *user* PATH (Windows). Nothing is added to the system PATH.
6. **Role.** Asks "Will this machine be a coordinator or a node?". A node is offered `mas join` right away. A coordinator is told it will listen on one port (TCP 7443, optionally UDP 7444) and that your firewall may ask: the installer changes no firewall, network profile, clock or router setting.

It is idempotent: running it again repairs the command and the pointer without downloading when the installed release is healthy, and upgrades in place when you pass another tag.

### Where things go

| Thing | Where |
|---|---|
| Program, environments, installer copy, `current`/`previous` pointers | `~/.mas` (`MAS_HOME` or `--home` to change) |
| `mas` command | `~/.local/bin/mas` (macOS/Linux), `%USERPROFILE%\.mas\bin\mas.cmd` (Windows) |
| Keys, pinned peers, logs, service log | the state folder of `mas`: `~/Library/Application Support/mas` (macOS), `${XDG_DATA_HOME:-~/.local/share}/mas` (Linux), `%LOCALAPPDATA%\mas` (Windows) |
| uv | where uv's own installer puts it (`~/.local/bin`); the uninstaller leaves it alone |

## Uninstall

```bash
sh ~/.mas/install.sh --uninstall            # Windows: powershell -ExecutionPolicy Bypass -File $env:USERPROFILE\.mas\install.ps1 -Uninstall
```

Shows what will be removed and asks. It removes the user services, the program folder, the `mas` command and the PATH block or entry it added. **Keys, pairings and logs stay** unless you add `--purge` / `-Purge` and type `DELETE` after being told that this makes the computer (and, for a coordinator, every node) pair again. uv, the firewall and the network are not touched.

## Run as a service (per user)

```bash
mas service install            # a paired node
mas service install --role coordinator --bind 192.0.2.10
mas service status
mas service uninstall
```

It shows the file it will write, the command it runs and the undo, then asks. The program starts **when you sign in**, and its output goes to `service-<role>.log` in the state folder. The managers are data in `skills/os-service/references/service.json`; no administrator and no sudo.

| OS | Manager | Notes |
|---|---|---|
| macOS | LaunchAgent in `~/Library/LaunchAgents`, loaded with `launchctl bootstrap gui/<uid>` | restarts on failure; macOS may show a "background item" notice ([launchctl bootstrap/bootout](https://inventivehq.com/knowledge-base/macos/how-to-manage-launchagents-launchdaemons-macos)) |
| Linux | `systemd --user` unit in `~/.config/systemd/user` | restarts on failure; starts at sign-in. Starting at boot without signing in needs an administrator to run `loginctl enable-linger` once; this program does not. |
| Linux without systemd user | desktop autostart entry in `~/.config/autostart` | only with a desktop session; no automatic restart. Otherwise the typed refusal `no_user_service_manager`: run `mas node run` by hand. |
| Windows | `HKCU\...\Run` value, windowless `pythonw.exe` | no administrator; no automatic restart. A Task Scheduler logon task was **not** chosen: whether a standard user may create one for their own logon could not be confirmed from the sources found ([schtasks create](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/schtasks-create) says only Administrators can schedule tasks for `/ru system`; the own-account case is not stated). |

A node that the coordinator unpairs exits normally and is not restarted. A node that is not paired does not get a service. A coordinator service binds to the address you give: if your router gives the computer another address it will not start, so reserve a fixed one.

## Update

```bash
mas update --check      # what would change, nothing done
mas update              # asks first
```

It reads the latest release's manifest (`mas-release.json`: tag, archive, SHA-256, notes), shows what changes, asks, downloads, checks the SHA-256, builds the new release **next to** the running one, smoke-tests it, switches `current` atomically, and keeps the previous release for one more update (older ones are deleted). Any failure before the switch deletes the new folder; a failure after it puts the old tag back. A registered service is re-registered with the new interpreter. Your keys and pairings are never touched.

What the check protects: a corrupt or swapped download. What it does not: a compromised publisher (the manifest comes from the same release page). The trust root is the public repository over HTTPS.

## Unpair

```bash
mas unpair <node>       # on the coordinator: name, id or id prefix
mas forget              # on a node: drop the coordinator locally (when the coordinator is gone)
mas coordinator rotate-identity
```

`mas unpair` asks first, then removes the node's pinned key from the coordinator *before* anything else, so the node is refused from that moment (conformance U1). If the node is connected it is told on its signed session (`revoked`) and forgets the coordinator; if it is offline it finds out when it dials and is refused. It can join again with `mas join`. `rotate-identity` makes a new coordinator identity: every node pins the old one, so **every node must pair again**; the old identity and node list are moved aside, not deleted. The coordinator must be stopped first.

## Making a release (maintainer)

```bash
python3 scripts/make_release.py ~/mas-export TAG out/      # mas-TAG.tar.gz + mas-release.json, deterministic
# publish both files on the GitHub release TAG, then:
python3 scripts/pin_installers.py TAG <sha256 of the published archive>
```

An archive cannot hold its own hash, so the installer copy inside a release pins the release before it; the installers on `main` pin the newest published one. The installers can only install a release that already carries `mas --version`-style support for the lifecycle commands (`node-preview-3` predates them: it installs and runs, but has no `mas update`, `service`, `unpair` or `forget`).

## Support matrix

See `docs/NETWORK_CONTRACT.md` 7.3 (lifecycle coverage), with the label of what was actually run.
