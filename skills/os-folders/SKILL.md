---
name: os-folders
description: Use when a task needs the names, sizes and dates of the files in a folder of the owner's machine, given by a well-known name such as Desktop or by an absolute path.
---

# Listing a folder (names, sizes and dates only)

The node's `list_dir` task reads ONE folder and returns, for each entry: its name, whether it is a file, a folder or something else, its size in bytes and its modification date. It never opens a file, never follows a link out of the folder and never recurses. A folder with more entries than the limit in `references/folders.json` is cut, and the result says how many were cut.

A well-known name (`desktop`, `documents`, `downloads`, `pictures`) is looked up in `references/folders.json` for this operating system family; an absolute path is used as it is. A relative path, a path with `..`, a path that is not a folder and a name no row knows are refused with a typed reason, so the owner can be told what to say instead.

The owner approves each path with one chat question before anything is listed, and the listing stays on the computers: a chat in the cloud never receives it (see the harness's input kinds).
