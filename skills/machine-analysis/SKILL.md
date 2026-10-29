---
name: machine-analysis
description: Use when a paired computer has to describe itself to its coordinator, covering hardware, system, programs and which AI is available there, read-only and without reading any secret.
---

# Machine analysis (a read-only task a node runs for its coordinator)

The node runs the repository's EXISTING discovery on itself and returns typed, evidence-tagged facts, grouped for the owner: system, hardware, programs, AI available. Nothing new is invented here: the Ring 0 probes (operating system, CPU and memory, disks), the observe-tier discovery skills where the operating system is supported (installed tools, other agents, local model servers, installed apps) and the workspace sizing table (which model size fits this memory and disk).

What it never reads: tokens, keys, passwords or MCP arguments. For API keys it reports the NAMES of environment variables that look like an AI provider's key (the patterns are data in `references/ai-env-names.json`), never their values. For AI command-line tools it reports that they are installed and what the discovery found, never a login or a token.

Where the discovery skills do not cover an operating system yet (they cover macOS and Linux today), the result says so in `limits`: the Ring 0 probes still run everywhere.
