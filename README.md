# Multi Auto Setup

A private network of agents across your own computers. Each machine installs the software once, with the owner's consent, pairs once with the owner's permission, and then takes part in shared work. The Python package is called `envrec`.

AI-assisted development.

## What it is

A **coordinator** runs on one computer and listens on **one LAN port**. Every other computer runs a **node agent** that only dials out to the coordinator and **opens no listening port at all**. The rules are written down in [docs/NETWORK_CONTRACT.md](docs/NETWORK_CONTRACT.md) and checked by the conformance suite in `conformance/`.

What exists today:

- **Secure pairing.** The coordinator shows a 6-digit one-time code. The code is the password of a password-authenticated key exchange (SPAKE2), so a network observer cannot test guesses offline. A code lives two minutes, allows one attempt, and is rate-limited. The owner then sees the node's name, system and key fingerprint on the coordinator and approves or refuses once.
- **Signed messages over mutual TLS with pinned keys.** Both sides keep each other's identity key from pairing; nothing else authorizes a node. Messages are signed envelopes with a timestamp window and replay protection.
- **A node agent** that sends signed heartbeats, reconnects with backoff, and holds zero listening sockets (checked at runtime, not only in the source).
- **A local web screen** for the owner (`mas coordinator start --web`): the paired machines with their online state, and pairing with the code, a countdown and an approve/reject card. It listens on loopback only, needs a one-time link printed on the owner's terminal, and rejects forged Host headers and cross-site requests. It is off by default.
- **Recognition and setup groundwork**: discovery of a machine and its local network, typed tools and skills, an append-only event log, a policy gate and a sandbox for skill scripts, and setup of a workspace on another machine with an approval for every write.
- A **harness**, not a script: a model picks one action per turn from a closed menu, and code provides the senses, the guards and the verification. See [docs/HARNESS_SPEC.md](docs/HARNESS_SPEC.md).

## What is verified, and what is not

Verification is labelled and never mixed.

- **Verified, SIMULATED-LOCAL on macOS (Apple Silicon):** the coordinator and a node as two real operating-system processes on one machine over loopback: pairing, heartbeats, and the owner's web screen driven in a browser. The coordinator holds the expected sockets and the node holds none.
- **Verified, LINUX-CONTAINER:** a node inside a Linux container (Alpine, Python 3.12) paired with a coordinator on the host and sent heartbeats.
- **Not verified yet:** Windows (the per-OS layer for it exists and has never run), two physical machines, discovery of the coordinator on a LAN, an installer, and anything that involves running a model end to end.

The model profiles in `config/models.yaml` are placeholders. Put your own endpoint and model id in `config/models.local.yaml` (not committed).

## Roadmap

A shared typed board with leases, a router that picks a combination of harness, model and machine by written rules and sensitivity levels, recommendations backed by evidence ([docs/RECOMMENDATION_SPEC.md](docs/RECOMMENDATION_SPEC.md)), and more sandbox backends.

## Install and run

Python 3.11 or newer.

```bash
uv venv --python 3.13 .venv && uv pip install --python .venv/bin/python -e .
```

The `discovery` extra adds optional Bluetooth, serial and mDNS support (`bleak`, `pyserial`, `zeroconf`); features that need them report "unavailable" without them:

```bash
uv pip install --python .venv/bin/python -e ".[discovery]"
```

Some fingerprinting uses public ID lists (USB, Bluetooth, vendor and service names). They are downloaded once, by you, into the git-ignored `data/downloads/` (sources in `DATA_SOURCES.md`); the few tests that need them are skipped until then:

```bash
.venv/bin/python scripts/fetch_data.py
```

On the coordinator, start it on the address of the network interface nodes will reach (a loopback, private or link-local address; never every interface), optionally with the local web screen:

```bash
.venv/bin/mas coordinator start --bind 192.0.2.10 --port 7443 --web
```

Pair a node. On the coordinator, show a code (or press the button in the web screen):

```bash
.venv/bin/mas pair
```

On the other computer, type the code once when asked:

```bash
.venv/bin/mas pair 192.0.2.10:7443
```

Then run the node agent there, and list the paired nodes on the coordinator:

```bash
.venv/bin/mas node run
```

```bash
.venv/bin/mas nodes
```

(`192.0.2.10` is a documentation address; use your own.)

## Hard limits

- Own or explicitly authorized machines only.
- Read-only by default; every write on a device needs a fresh owner approval that shows the exact action, with an undo recorded first.
- Pairing, trust prompts and passwords are never bypassed.
- No credentials are stored in the repository, logs, facts or prompts.
- No scanning of other people's systems; the microphone is off.
- The software never changes an operating-system firewall, network profile or router setting by itself.

## Tests

```bash
.venv/bin/python -m pytest conformance
```

```bash
.venv/bin/python -m pytest tests
```

The skill sandbox uses Docker, so the full suite needs Docker running. Two conformance checks read recorded runs that only exist on the machine that made them; in a fresh checkout they are skipped as "not measured".

## Layout

`src/envrec_coordinator/`, `src/envrec_node/`, `src/envrec_net/`, `src/envrec_mas/` (the network layer and the `mas` command), `src/envrec_os/` (the only place with operating-system specific calls), `src/envrec/` (recognition, tools, loop, setup), `skills/` (SKILL.md folders), `conformance/`, `docs/`, `tests/`, `config/`, `fixtures/` (recorded outputs, redacted), and [THIRD_PARTY.md](THIRD_PARTY.md) for licenses.

Tests with Meta's models are planned.
