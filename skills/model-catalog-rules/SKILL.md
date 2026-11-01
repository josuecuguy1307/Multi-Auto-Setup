---
name: model-catalog-rules
description: Use when adding a catalog entry, estimating whether a local model (GGUF, Ollama) fits a machine, building setup options or a setup plan, or checking a model file's size, kind, license and origin.
---

# Model catalog rules

Knowledge for the agent and for the catalog tools. Each rule says what goes wrong, how to detect it from verifiable data, and what to do. Code applies the hard filters; the model explains. Every derived fact is tagged "verified-by-code" (read from file headers, the hub API or the runtime) or "declared-by-project" (model card text). When they disagree, verified-by-code wins and the conflict is recorded.

## 1. Is this file the model at all?

| Pitfall | Detect | Do |
|---|---|---|
| Vision projector (`mmproj`) taken as the model | file name contains `mmproj`; GGUF `general.architecture` = `clip`; small size next to a larger GGUF in the same repo | never a catalog entry on its own; attach as a companion only when the task needs vision |
| LoRA adapter | GGUF `general.type` = `adapter` (or LoRA keys) | exclude; adapters need a base model |
| Vocab-only GGUF | GGUF `general.type` = `vocab`, no weight tensors | exclude |
| Importance matrix, tokenizer, config files | `.dat`, `.json`, `.txt`, `imatrix` in name | exclude |
| Draft / speculative model | card or name says draft; very small sibling of a large model | exclude from general options |
| Embedding or reranker model | architecture or card type embedding/reranker; no chat template | only for embedding/retrieval tasks, never as a chat model |

## 2. How big is it really?

- **Use the real file size, not params × bits.** Quant names are effective bit-widths: Q4_K_M is about 4.9 bits per weight, so an 8B file is about 4.6 GiB, not 4.0.
- **Split GGUF shards:** names like `-00001-of-00003.gguf`. Size = sum of all shards; download all shards; the plan lists every shard with its SHA-256.
- **Mixture of experts:** total parameters decide memory; active parameters decide speed. Store both.
- **KV cache grows with context:** bytes ≈ 2 × layers × kv_heads × head_dim × context × bytes_per_element. Read `block_count`, `attention.head_count_kv`, key/value length from GGUF metadata. Sliding-window, hybrid (Mamba/linear) and MLA models need less; mark their KV estimate "estimate, architecture-specific". Always set the context explicitly in the plan; never assume the trained maximum.
- **Runtime overhead:** add a fixed allowance (≈1 GB) plus a percentage of weights; Ollama holds more than plain llama.cpp. Label it as estimate.

## 3. Will it fit on this machine?

- Compare against **available** memory at probe time, minus a margin, not total memory. Show both numbers and the margin.
- **CPU path vs GPU path:** a GPU build helps only with enough dedicated VRAM. Integrated GPUs share system RAM: they do not add memory, and the default path is CPU. The explanation must follow the `machine.model_path` fact.
- Disk: free space must cover all files plus temporary download space.
- If it does not fit: say so with the numbers and offer the variants that fit (smaller quantization or smaller model). The owner picks.
- After install, **measured here beats estimate:** record resident memory after load and tokens per second from the minimal call; the option label changes from "estimate" to "measured here".

## 4. Is it the right kind of model for the task?

- Chat/agent tasks need an instruct/chat model with a chat template (`tokenizer.chat_template` in GGUF). Base models are excluded from chat options.
- Vision or omni models only when the task needs images or audio; otherwise prefer the text model (less memory, fewer moving parts).
- Reasoning ("thinking") models: long outputs and higher latency; label them.
- Safety / guard classifiers (`guardian`, `guard`, `shield` models) classify text as safe or unsafe; they are not chat models, only for moderation tasks.
- Context claims: trained context vs extended (RoPE scaling) — store what metadata says and what the card claims, separately.

## 5. Can the runtime load it?

- The architecture must be supported by the pinned runtime version. New architectures need newer llama.cpp/Ollama builds. Verify with a load test after install; until then label "load not verified".
- Ollama: never use a moving tag like `latest`; pin the digest. Ollama names differ from Hugging Face names; record the mapping.

## 6. Is it legitimate and stable?

- Pin the repository revision (commit) and the SHA-256 of each file. Repos get re-uploaded.
- Prefer the original publisher or a well-known quantizer; record who published it. Re-uploads with unclear origin are excluded.
- Gated repos need a login token: we never handle tokens, so mark them "unavailable without the owner's own login".
- License: read `general.license` from GGUF metadata and the card; if they disagree or are missing, mark "license unverified" and do not offer it by default.
- Never use vendor benchmark numbers as evidence; follow the source tiers in RECOMMENDATION_SPEC.

## 7. Where to verify (in this order)

1. **GGUF header metadata** (read with an HTTP range request before download, or from the file): architecture, `general.type`, size label, context length, block count, KV heads, chat template, license.
2. **Hub API file listing** for the pinned revision: every file with its size; shard detection.
3. **The runtime's own report** after install: llama.cpp load log, `ollama show --verbose`, resident memory, tokens per second.
4. **Official docs** of the runtime and the GGUF spec (llama.cpp repository and its gguf documentation).
5. **Model card**: declared-by-project only.

When a new kind of file or architecture appears, check the llama.cpp releases and discussions and the GGUF spec first, then add a row to this skill and a fixture test. Each rule in this file has a fixture that must fail without the rule.
