# envrec

A harness for a private network of AI agents that runs across computers you own, on your local network.

Entry for the Meta Global AI Developer Hackathon, Indie track (build window Oct 26 to Nov 4, 2026).

Human development and AI-assisted development.

Tests with Meta's models are planned for Thursday, October 29.

## What it does

- **Recognition.** Read-only probes find out what the machine is. They return typed facts with their evidence. The model sees a short, budgeted view and a closed menu and answers with one validated JSON action per turn.
- **Unknown devices.** Anything the probes do not recognize goes up a ladder (tested, honestly unknown, or contradicted) on recorded replay devices.
- **Pairing with owner permission.** A device becomes a target only when you enroll it yourself, in a terminal. Nothing the model says can create a target.
- **Workspace self-setup.** For an enrolled device, the controller plans and sets up a model server that listens only on that device's localhost, reached through an SSH tunnel it opens. Every write asks for a fresh approval that shows the exact command, and one approved command removes everything again.

## Setup

Requires Python 3.11 or newer.

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
```

```bash
.venv/bin/python -m pytest
```

The test suite uses scripted models and recorded fixtures. Some tests use Docker and are skipped when it is not available. Optional reference tables (USB IDs and similar) are downloaded once by `scripts/fetch_data.py` into a git-ignored folder; see `DATA_SOURCES.md`.

## Run

```bash
.venv/bin/python -m envrec run --profile muse-glimmer
```

`inventory`, `enroll`, `fleet`, `deploy` and `undo` handle enrolled devices; see `docs/CONNECTING_DEVICES.md` and `docs/PROVISIONING_SAFETY.md`. `python -m envrec --help` lists everything.

## Configure the Meta models

Model profiles live in `config/models.yaml`: `muse-glimmer` (the small model) and `muse-spark` (the strong one). Both are OpenAI-compatible endpoint profiles, and the shipped endpoint and model id are placeholders.

Put your real values in `config/models.local.yaml`, which has the same shape and overrides the profiles above. Keep that file out of version control. If the endpoint needs a key, set `api_key_env` to the name of an environment variable that holds it; never write a key into a file.

```yaml
profiles:
  muse-glimmer:
    name: muse-glimmer
    kind: openai_compat
    model_id: <your model id>
    endpoint: <your OpenAI-compatible base URL>
    supports_tool_calling: false
    context_tokens: 8192
    view_budget_facts: 8
    menu_max: 6
```

`view_budget_facts` and `menu_max` are the only dials that differ between a small and a strong profile.

## Hard limits

- Own or authorized devices only.
- Read-only by default. Every write needs a fresh approval for that exact action; there is no "approve all".
- No credentials in the repo. SSH keys are referenced by environment variable name, and key paths are never logged.
- Pairing cannot be bypassed: enrollment needs a terminal and your typed confirmation.

## Status

Ground truth for the author's machine is a draft, so scores are labelled provisional. Replay devices are recorded transcripts, not hardware. No results with real models are claimed here.

Third-party code and data are listed in `THIRD_PARTY.md` and `DATA_SOURCES.md`.
