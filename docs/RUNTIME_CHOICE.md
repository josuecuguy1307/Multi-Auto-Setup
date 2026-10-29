# Runtime and model sources: proposal

Status: **proposed, not approved, nothing downloaded.** `config/runtime_manifest.yaml` has `approved: false`.

Approving the manifest approves **sources**: a few official runtime projects (their release channels) and model publishers, each with a license allowlist. The catalog generator (R3b) then computes every combination from their official metadata, so the space can reach thousands of entries while you only review the sources. At run time only the generated lockfile is used.

## Runtimes evaluated

All facts below were read from each project's own LICENSE file and its release API.

| Runtime | License | Ships binaries | Backends in releases | Formats | Verdict |
|---|---|---|---|---|---|
| **llama.cpp** | MIT | yes, per build tag `b<N>` (prerelease-flagged) | CPU, Vulkan, CUDA, ROCm, OpenVINO, SYCL, Metal (macOS), Android | GGUF | **approve** |
| **mistral.rs** | MIT | yes, stable tags `v0.9.5`... | CPU (linux gnu), Metal (macOS), CUDA per GPU class | GGUF, safetensors | **approve** |
| llamafile | Apache-2.0 (the file; GitHub's API says NOASSERTION) | yes, one executable per release | CPU, cross-OS | GGUF | propose-off |
| ollama | MIT | yes | CPU/GPU, Jetson, ROCm | GGUF via its own registry | propose-off |
| LocalAI | MIT | yes | needs backends fetched at run time | various | **exclude**: fetches outside the manifest |
| cortex.cpp | Apache-2.0 | yes | n/a | n/a | **exclude**: archived |
| mlc-llm, mlx-lm, BitNet, ik_llama.cpp, fastllm | MIT or Apache-2.0 | no release binaries | n/a | n/a | **exclude** |

Why two runtimes and not one: the harness needs to *choose* a (runtime, model) pair per device, and a single runtime would make that choice trivial. llama.cpp and mistral.rs both give small standalone CPU binaries for both fleet architectures and differ enough (formats, backends, server behaviour) to make the choice real.

Why the other two are off: llamafile is a different execution model (a self-extracting cross-OS executable; the standard build is 368 MB) and ollama's archives are 1.5 GB, need zstd, and run a daemon with its own model registry, which is a source outside this manifest. Neither is bad; both can be turned on by editing one line.

## Compatibility matrix (what the generator may combine)

"Fleet" = a Docker container on this controller (Apple silicon, CPU only; the other architecture is emulated x86_64).

| Runtime | Backend | OS | Arch | Asset (build b11856 / v0.9.5) | Size | Fleet can run it |
|---|---|---|---|---|---|---|
| llama.cpp | cpu | linux (glibc) | arm64 | `llama-b11856-bin-ubuntu-arm64.tar.gz` | 18.1 MB | yes (glibc to be checked) |
| llama.cpp | cpu | linux (glibc) | amd64 | `llama-b11856-bin-ubuntu-x64.tar.gz` | 22.4 MB | yes, emulated |
| llama.cpp | vulkan | linux | arm64 / amd64 | `...ubuntu-vulkan-arm64/-x64.tar.gz` | 28.4 / 35.1 MB | no GPU in containers |
| llama.cpp | cpu (Metal inside) | macOS | arm64 / amd64 | `...macos-arm64/-x64.tar.gz` | 16.2 / 12.0 MB | controller itself, not a container |
| llama.cpp | cuda | linux | amd64 12.8 / arm64 13.4 | `...ubuntu-cuda-...tar.gz` | 178 / 154 MB | no NVIDIA GPU here |
| llama.cpp | any | linux **musl** (Alpine) | any | **not found** | | no |
| mistral.rs | cpu | linux gnu | arm64 / amd64 | `mistralrs-cpu-aarch64/x86_64-unknown-linux-gnu.tar.gz` | 36.2 / 36.3 MB | yes (glibc to be checked) |
| mistral.rs | metal | macOS | arm64 | `mistralrs-metal-aarch64-apple-darwin.tar.gz` | 50.5 MB | controller itself |
| mistral.rs | cuda | linux | per GPU class | 0.8 to 1.2 GB each | | excluded by the 3 GB / size policy and no GPU |

Model formats: GGUF (both runtimes), safetensors (mistral.rs only).

## Model sources

First-party publishers, approved; a repo is kept only if the license on **its own model page** is in the allowlist (`apache-2.0, mit, bsd-2/3-clause, isc, unlicense`), otherwise it is excluded automatically and listed with the reason.

| Publisher | GGUF repos seen (first 100) | Licenses seen |
|---|---|---|
| Qwen | 54 | 41 apache-2.0, 13 other (incl. `qwen-research`: excluded) |
| HuggingFaceTB | 6 | 6 apache-2.0 |
| ibm-granite | 53 | 51 apache-2.0, 2 none (excluded) |
| microsoft | 6 | 6 mit |
| mistralai | 11 | 11 apache-2.0 |
| allenai | 12 | 6 apache-2.0, 6 none (excluded) |
| TinyLlama | 3 | 3 apache-2.0 |

Conversion hubs (ggml-org, bartowski, unsloth) host other people's models under mixed licenses, including cc-by-nc and no license at all: **off by default**. Excluded outright: Google (the Gemma license restricts use), meta-llama and Llama-family licenses, LiquidAI (LFM license), lmstudio-community (mixed, and redundant). If the Meta angle matters, overriding the Llama exclusion is your call.

## Size of the space

Upper bound before any filter: about 124 allowed repos x 3 quantizations is roughly 370 model files, times 11 runtime assets is on the order of 4,000 raw combinations. Parameter cap (8B), the 3 GB file cap, format support per runtime and the target's limits cut that down a lot. The generator, not this estimate, reports the real number.

## Initial smoke set

So work can start before the generator exists, the manifest lists the artifacts I already cross-checked (hash, size, URL, pinned revision all match the source APIs): llama.cpp `b11856` for arm64 and amd64, and three Qwen models (0.5B 491 MB, 1.5B 1.12 GB, Qwen3-4B 2.50 GB). They sit inside the approved sources. The default live run uses the runtime plus the 0.5B model, about 0.5 GB.

## Risks, stated up front

1. **glibc.** The Linux builds are labelled `ubuntu` / `gnu`. The Debian bookworm fixtures may have an older glibc than they need. Unknown until the first run. If so, only the fixture image changes (to an Ubuntu base).
2. **Alpine.** No musl build exists for llama.cpp; the Alpine fixture stays a stub-only provisioning test.
3. **Archive contents and flags.** I expect `llama-server` / `mistralrs` inside the archives but have not opened them. The capped flags (threads, context, memory) are fixed after the first approved fetch shows what each build supports.
4. **Prerelease tags.** llama.cpp flags every build a prerelease. The per-asset sha256 and commit pin the exact bytes, so this is stability, not integrity.
5. **Lockfile reality.** The generator trusts each source's metadata for size and sha256; for Hugging Face that is the LFS object id, which is a sha256. A source that offers no checksum is excluded.
6. **Small models may be weak** at the structured-action task. That result will be reported as it is.

## What approval means

`approved: true` lets the generator read release metadata from the sources marked `approve`, and lets the fetcher download artifacts that appear in the lockfile, each after its own approval prompt showing URL, size and checksum, each after the disk-reserve check. Sources marked `propose-off` or `exclude` stay unused until you change their status.
