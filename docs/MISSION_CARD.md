# Mission card and targets

Two files, two roles. Keep them apart.

| File | Written by | What it is | What it can do |
|---|---|---|---|
| `mission.yaml` | you | `goal` and `scope`, free text | Shown to the model as context at the top of every turn. **Authorizes nothing.** |
| `targets.yaml` | only you: by hand, or `python -m envrec enroll` | One entry per device you own | The **only** source of valid targets. Policy denies every action on a device that is not listed. |

Changing the words in `mission.yaml` cannot add, authorize or reach a device. That is tested: a whole session makes identical policy decisions with and without "also install on any device you find" in the goal, and the policy modules cannot even see the mission card.

## mission.yaml

```yaml
goal: "Recognize each device in my fleet, then set up a model server on the ones I approve, and prove it works with a test request."
scope: "Devices listed in targets.yaml only."
limits:                      # fixed. Leave exactly as shipped; a file that changes one is refused
  targets_source: config_only
  max_workers: 3
  deploy_requires_approval: true
  worker_can_deploy_further: false
  model_server_bind: localhost_only
  autostart: false
  credentials_in_repo: false
```

`goal` is capped at 500 characters and `scope` at 300. They are never parsed as instructions. Shipped copy: `config/mission.example.yaml`.

## targets.yaml

```yaml
targets:
  - id: laptop-b
    name: "Laptop B"
    host: 192.168.1.42
    os: linux
    allowed_transports: [ssh]
    ssh: { user: josue, port: 22, key_env: LAPTOP_B_KEY_PATH }
    limits: { max_disk_gb: 20, max_model_params: 9B }
    owner_confirmed: true
```

Rules the loader enforces (it refuses the whole file otherwise):

- Every field is strict: unknown fields are errors, including anything named `password`, `private_key` or `token`.
- A transport you list needs its connection block (`ssh`, `docker_exec`, `adb_usb`, `serial`), and a block for a transport you did not list is refused.
- `key_env` is the **name** of an environment variable (upper case). It holds the path of a key file kept outside the repo. The key itself is never in this file.
- `allowed_transports` is ordered. The connection planner picks only from it, in your order of preference after its rules, and never tries anything else.
- `max_model_params` looks like `9B` or `500M`. The model chosen for a device never exceeds the **stricter** of this, the RAM table (`src/envrec/remote/sizing.py`), and the device's free disk and `max_disk_gb`.
- `reachability` may appear (`unchecked`, `reachable`, `unreachable`). Only enrollment's single read-only check writes it. The planner's choice is logged as an Event and never written back.

Shipped copy: `config/targets.example.yaml`.

## Adding a device

1. Run recognition and read the inventory report: `python -m envrec inventory --store runs/<run>`.
2. Do the on-device steps yourself (see `docs/CONNECTING_DEVICES.md`).
3. Either run `python -m envrec enroll --store runs/<run>` (interactive; you type the device's name and `yes`), or write the entry by hand.
4. Discovery never adds a device. A device the scan saw but you did not enroll stays inventory only, in memory, for that session.

## Editing the size table

`src/envrec/remote/sizing.py` holds the RAM brackets. It is yours to edit; the model never writes it.
