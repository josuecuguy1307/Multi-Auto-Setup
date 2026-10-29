---
name: app-installed
description: Use when the applications and packages installed on this machine need to be counted or listed by source.
---

# Installed applications and packages

On a Mac it reads the application inventory (with version, where it came from and the architecture) and falls back to listing `/Applications` when the profiler is slow or blocked; Homebrew formulae are counted separately. On Linux it asks each package manager that exists (dpkg, rpm, pacman, flatpak, snap) and counts what each answers.

A package manager that is not installed is not an error: it is skipped. If none answers, the result is a typed unknown, never a count of zero, because zero would claim the machine has no software.
