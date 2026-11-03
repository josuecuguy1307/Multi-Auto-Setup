---
name: os-menu
description: Use when `mas` is run with no arguments on a computer - the first-run menu for THIS computer only (coordinator or node, leave it running as a service, connect Claude Desktop, announce a node). Says which steps exist, in what order, and how each one is undone.
---

# The first-run menu (`mas` with no arguments)

One guided entry for the few terminal steps a computer needs once. After Claude Desktop is connected, everything else (adding computers, pairing, analysis, setting up models) happens in the chat; the menu does none of that. It is not a chat and not a daily console.

The menu is for the OWNER, not the model: it decides nothing and writes nothing itself. Each step runs the command that already exists (`mas service install`, `mas connect claude-desktop`, `mas join`), which shows its own plan with the exact command and the undo and takes the owner's typed `yes` (the gesture `cli_typed`). The menu never answers for the owner and never passes `--yes`.

* `references/menu.json`: the steps, per role, in order. Each has the command shown before it runs, its undo, what it needs first, and the name of the check that says it is already done. A new step is a new row plus a check in `envrec_mas/menu.py`; a new role is a new key.
* The banner is drawn by `envrec_style.term` from the style tokens (docs/STYLE.md); the menu's words are in `envrec_mas/strings.py`.

Rules that hold every time:
* Re-running shows what is already done and offers only what is missing. The role is read from what is on disk (a coordinator's settings, a node's pinned coordinator); it is asked only when neither exists, and asking writes nothing (it only chooses which steps are shown).
* No terminal to answer on: plain help text, exit code 2, never a prompt. `NO_COLOR`, `TERM=dumb` and narrow widths are handled by the banner.
* Claude Desktop is never restarted for the owner: the menu says to quit and reopen it.
* The coordinator's address is chosen from this computer's own private-network addresses; nothing is guessed.
* The installers (install.sh, install.ps1) do not open this menu yet.
