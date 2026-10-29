# Provisioning safety: the shipped policy

These limits are fixed. They do not depend on what you write in `goal` or `scope`, and no model action can change them.

The network rules in `docs/NETWORK_CONTRACT.md` supersede the controller-to-SSH-to-device design described below. Rows marked **legacy** describe code that still exists and is enforced today; Phase 1 replaces it, and no new code may depend on it. Nothing here was loosened.

| Limit | Meaning | Where it is enforced |
|---|---|---|
| `targets_source: config_only` -> **targets.yaml OR owner-confirmed pairing** | A device is authorized only by an entry in `targets.yaml` (by hand, or by interactive enrollment) OR by a pairing the owner confirmed on the coordinator (contract section 2). Facts found by Ring 1/2 discovery are inventory only and never become a deployable target, even if they match an entry's fingerprint. No action may use discovery results to propose a new target. | `policy/config_gate.py`; `enroll/` is the only writer of `targets.yaml` (tested by source scan). The pairing store does not exist yet (Phase 1) and will have the same property: written only after owner confirmation. |
| `max_workers: 3` -> **lease model** | The fixed limit of three workers is replaced by the board's lease model: one lease per task, a deadline, automatic reassignment on expiry (contract section 5). If a cap is still needed for safety (concurrent leases, attempts per task) it is a coordinator setting, not a constant. | **legacy:** `mission/models.py` still holds the fixed value until Phase 1 replaces it. |
| `deploy_requires_approval: true` | Every write to a target needs a fresh approval showing the exact command, target id, file checksum and version, and the tool's origin. No "approve all", no carryover. | `remote/approval.py`; tested: six writes, six separate prompts |
| `worker_can_deploy_further: false` | A worker or node on device B can never deploy to, enroll or push tasks to device C; nodes do not talk to each other. This is not a permission check: the node/worker build does not contain deploy-further, enroll, planner or remote-runner code at all. A node may inspect itself and run what the coordinator leased to it, within the task's capability token. | `build/worker/`, `src/envrec_worker/`; build-time test inspects the artifact |
| `model_server_bind: localhost_only` -> **local inference, signed channel only** | Each node runs its own inference locally. Our node agent opens zero listening sockets; a third-party inference server is used only if it is bound to loopback (127.0.0.1 or ::1), which the node verifies at runtime, and it is never exposed, forwarded or tunnelled. The node reaches the coordinator only through the node-initiated signed channel; the coordinator never opens a connection to a node, and there is no controller-opened tunnel (contract sections 1 and 3). Nodes open no other listening port. | **legacy:** worker refuses any other host than 127.0.0.1 and the controller still opens an SSH tunnel; tested by reading `/proc/net/tcp` inside the container. Conformance N1/N2 now guard the new shape. |
| `autostart: false` | Nothing starts on boot or on its own. | no installer step creates a service |
| `credentials_in_repo: false` | No key, password or token in any file. SSH keys are paths in environment variables; the variable's name is in config. Key paths are never logged or put in a Fact. | `mission/models.py` validators; tests check events and results |

## Also true

- **Only a pinned artifact is ever sent.** `build/worker/release.json` pins the worker's sha256. The controller checks the local file against it before the first approval, and the device's copy after each transfer. A mismatch aborts before anything is installed. Never code a model wrote, never the controller's own source.
- **Fixed commands only.** Remote work uses a library of command templates with strictly validated placeholders. There is no free-form shell string from the model. Scripts receive values as positional parameters, never inside script text.
- **Full undo.** An undo entry is recorded before each write. One approved command stops the server, removes the install folder, and confirms nothing remains. A denied approval leaves no entry and no change.
- **Honest failure.** A read-only filesystem, a missing interpreter, a killed or truncated transfer, a server that will not start: each ends as a recorded failure with the reason, never as an invented success.
- **A server is `verified` only after a fresh-nonce test.** The check sends random tokens generated at that moment and requires them back. A server that returns canned text fails.
- **Escalation is code.** After N consecutive invalid replies, or when a weak device's model would ask a human, the turn goes to the controller's model. Each escalated turn is logged with its reason.
- **Skills do not travel for free.** A skill verified on one device is `unverified_here` on every other device until its stored tests pass there.
- **Enrollment is human-only.** It refuses without a terminal, inside the loop, and in a worker build.

## What is not enforced by the operating system

REA runs without an OS-level network ban (see `docs/mapping_cards/rea.md`), and the replay probe runner is a restricted subprocess, not a security sandbox. The model-written code path runs on recorded data only.
