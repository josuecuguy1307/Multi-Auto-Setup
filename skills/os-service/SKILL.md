---
name: os-service
description: Use when the node (or the coordinator) has to start by itself when its owner signs in, on one operating system, without administrator rights.
---

# Running the program as a per-user service, per operating system

The code never names a service manager, a folder or a command of its own. `references/service.json` lists, for each operating system family (`darwin`, `linux`, `windows`), the user-level managers in the order they are tried. Each entry says how to tell that it is available (`probe`, `needs_env_any`), the file it writes (with a template next to this file), the commands that start, stop and read it, the words shown to the owner (English), and the undo.

Rules that hold for every row:

* **Per user, no administrator, no sudo.** Nothing is written outside the owner's profile. A manager that would need elevation is not a row.
* **Logon, not boot.** These managers start the program when the owner signs in. Starting before sign-in needs a system-wide change (a Linux "linger" setting, a Windows service) and is left to the owner; the text says so.
* **The owner is asked first** and sees exactly what will be written and the command that undoes it (`mas service install` prints both before it does anything).
* **No firewall, network profile or clock change.** A coordinator service listens on one port: the text says that and changes nothing.
* A family with no available manager is refused with the typed reason `no_user_service_manager`; the owner runs `mas node run` by hand.

Add a family or a manager by adding a row; do not branch in Python.
