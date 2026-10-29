---
name: app-containers
description: Use when running containers or virtual machines on this machine need to be listed (docker, podman, Parallels, VirtualBox, UTM).
---

# Containers and virtual machines

Asks each runtime that might be installed for its list: docker and podman (running containers as JSON lines), Parallels, VirtualBox and UTM. Each answer becomes entities with the runtime, the image or name and the state.

A runtime that is not installed is skipped silently. If none answers the result is a typed `not_applicable` unknown rather than zero: "no tool answered" and "nothing is running" are different things.
