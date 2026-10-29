---
name: app-model-servers
description: Use when a local model server (Ollama, llama.cpp, LM Studio, vLLM and similar) may be running on this machine and has to be found.
---

# Local model servers

`assets/model_servers.json` lists the model servers worth recognizing, with the ports each normally uses, a read-only path that identifies it and a pattern its answer must match. The skill first finds which of those ports this machine is listening on, then asks each one a single GET on the loopback address.

A port that listens proves nothing: many things listen. The answer must match the table's pattern before a server is recorded, and an OpenAI-compatible endpoint on a port that a more specific server already claimed is not counted twice. The GET is read-only and loopback only; the tool refuses any other host.
