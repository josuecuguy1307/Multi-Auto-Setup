---
name: remote-os-probe
description: Use when an enrolled computer answers and its operating system family is not yet known.
---

# Which operating system family answers

One read-only command, `uname -s`, tells the common Unix-like families apart. A computer that is not Unix-like does not understand it: the command fails, `os.family` stays unknown and says why, and nothing is guessed. The way forward for an unknown family is the unknown ladder (read about it with `remote_read`, research its documentation, propose a skill), not a table in this folder.
