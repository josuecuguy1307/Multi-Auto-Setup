---
name: app-agents
description: Use when other AI agents or assistants on this machine, and the MCP servers they are configured with, need to be found.
---

# Other agents and their MCP servers

`assets/agents.json` is the table of agents worth knowing (name, category, the process names, binaries, app bundles and config directories that betray each one). A running process, an installed binary, an app bundle or an existing config directory each count as evidence, and the entity records which of those it was seen as.

MCP configuration files are the sensitive part: they hold tokens. They are read by a tool that reduces each file to `{server name: command basename}` BEFORE anything else sees it, so neither this script nor the store ever holds an argument, an environment variable, a header or a token. The model sees names and command basenames only.

A running program that is not in the table but listens on a local port and has an agent-like name becomes an unresolved `unknown_agent`: a lead for the unknown ladder, not a claim.
