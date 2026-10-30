---
name: setup-options
description: Use when options for which AI to use on a machine have to be built from what the machine has, with hard filters and a written ordering that no model can change.
---

# Setup options from a machine's analysis

The owner says which kind of AI they want on a machine (open-source local, a subscription through its official command-line tool, their own API key, or a mix). Code then builds candidates from what the analysis found and from the small catalog, applies the HARD FILTERS, orders what is left by the WRITTEN RULE, and shows at most three. The model explains in plain words why each option is there and what limits it; it never ranks, and the order does not depend on anything it says.

Sources, each labeled: the repository's `catalog.lock.yaml` (runtime builds and model files whose licenses and checksums were read by code: "verified-by-code"), `references/harnesses.json` (command-line tools and API providers: "declared-by-project", with the source link), and `references/evidence.json` (public evidence entries with source, tier and date; empty means "no public data", which is said, not hidden).

Hard filters: the model fits the machine's memory (VRAM on a GPU path, RAM otherwise), the disk has room, the license is on the allowed list, the runtime has a build for this operating system and architecture, and the sensitivity allows it (an option that sends data to a provider is CLOUD and is dropped for confidential work). A sandbox is not needed to RUN a model server; any generated code would need one, and that is not part of this step.

Written ordering: cost class first (free and local, then a subscription, then metered API), then measured pass rate and measured time when they exist (none are measured yet, so they are skipped and the option is labeled "provisional, not measured here"), then the written order (an already-installed runtime before a download; a larger model that fits before a smaller one). Nothing is installed in this step.
