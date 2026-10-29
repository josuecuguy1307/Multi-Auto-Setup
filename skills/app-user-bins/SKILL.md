---
name: app-user-bins
description: Use when programs in the user's own bin directories that the known-tools table does not cover should be listed as unknowns.
---

# Unknown executables in user bin directories

Lists file NAMES in the user's own bin directories (`~/.local/bin`, `~/bin`, `~/.cargo/bin`, `~/go/bin`) and keeps those that the tools table does not know. Each one becomes an unresolved entity: something present that nothing here can name yet, which is exactly what the unknown ladder is for.

Only names are read, never the files. At most 40 are kept. If none of the directories exists the result is a typed `not_applicable` unknown.
