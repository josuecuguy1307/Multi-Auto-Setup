---
name: setup-recipes
description: Use when a setup plan has to be built for a machine from a closed recipe, with the operations in order, the paths each may write, the exact command to show the owner for each operating system and how to undo each.
---

# Closed setup recipes (data, not code)

A recipe lists the **operations** a setup may use (`references/recipes.json`), the **sequence** they run in, the **paths each may write** (always under `<mas>`, the node's own folder in the user's profile), the **command shown to the owner** for each operating system family (macOS, Linux, Windows) and the **undo** of each. A plan can only contain what its recipe lists; the node holds its own copy and refuses a plan made from a different one.

The plan is built by code (`envrec_coordinator.setup_plan.build_plan`) from three things that are never typed by a model: the recipe, the option the owner picked (its build comes from the **build matrix** of `model-catalog-rules`: operating system x architecture x where models run x graphics maker) and the catalog's pinned addresses and SHA-256 of the runtime build and the model file. A machine whose cell the matrix does not support, a build the catalog lacks, or a model that does not fit the measured memory gets a typed refusal (`unsupported_cell`, `no_build_in_catalog`, `no_cell_for_facts`, `does_not_fit`), never a guessed plan.

The inference server a plan starts binds to `127.0.0.1` only. How many layers go to the graphics memory comes from the backend of the matrix cell (`backend_args`), not from the machine's name. Writing a recipe is a change to this file and its fixtures, reviewed like code; a model never writes one.
