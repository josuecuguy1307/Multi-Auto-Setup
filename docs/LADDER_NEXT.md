# LADDER_NEXT: what plugs in for rungs 4 and 5, the verifier, and skills

> Status: the replay-runner, verifier and skill parts below are built (see `ladder/generate.py`). The Docker runner is still not built.
Built so far (this phase): rungs 1, 2, 3 and 6 on replay devices. The contracts, store, policy, menu and scorer already carry what the next phase needs, so it adds adapters and one loop branch; it does not redesign anything.

## What already exists and is reused unchanged

| Piece | Where | Why the next phase needs it |
|---|---|---|
| `Action.research`, `Action.propose_probe` | `contracts/models.py` | Validated and parsed today; the loop currently answers them with `rejected: ... not available in this phase` (`loop/run.py`, `_apply`) |
| `ToolSpec.origin = generated`, `ToolSpec.verified` | `contracts` | Generated tools register with `verified=False` |
| Policy rule: generated + unverified is denied on real targets, `allow_logged` on `replay:` handles | `policy/policy.py` | The sandbox path is the only way an unverified tool ever runs |
| `Skill(signature, code_ref, tests_ref, evidence, verified_runs)`, `Store.put_skill / find_skill` | `contracts`, `store` | Skill storage is done; nothing executes a skill yet |
| `Target.signature` (`usb:<vid>:<pid>`), `try_known_skill` (rung 1) | `ladder/adapter.py` | Already computes the signature and looks it up |
| `Fact.status`: `verified` needs `created_by=verifier` or two independent providers | `contracts` | The verifier is the only new source of `verified` |
| `Hypothesis.rung`, `ToolSpec.rung`, `scoring.highest_rung` | `contracts`, `scoring/rungs.py` | Rungs 4 and 5 just carry `rung=4` / `rung=5`; the metric already counts them |
| `ReplayDevice` (descriptor, stream, allowlisted replies) | `ladder/replay.py` | The replay runner for generated probes |
| `ModelAdapter` + `ModelProfile` | `model/` | The verifier is a second profile in `config/models.yaml` |

## Rung 4: research

```python
class Researcher(Protocol):
    async def research(self, target: Target, question: str, ctx: ToolContext) -> ProbeResult: ...
```

- Loop: in `_apply`, route `ActionType.research` to a `Researcher` (question comes from `Action.question`, target from `Action.target_id` or the current one).
- Reads only domains listed in a human-written config file (same trust rule as `Target.authorized`). Downloaded text is untrusted data; it is stored by hash and never enters a prompt.
- Output: `hypothesis` facts only, `Evidence.provider = "research:<domain>"`, `raw_ref` to the saved page, `limitations = ["from documentation, not measured"]`, source URL in `Evidence.note`. A fact reaches `tested` only after a rung-5 probe built from it passes its test.
- Menu: `research` joins OTHER ACTIONS for profiles that enable it (`ModelProfile` gets an `actions` list; strong models see it earlier).

## Rung 5: generate a probe

```python
@dataclass
class SandboxResult:
    exit_code: int; stdout: RawRef; stderr: RawRef; seconds: float; violations: list[str]

class SandboxRunner(Protocol):
    def run(self, code: str, *, stdin: bytes, timeout_s: float) -> SandboxResult: ...
```

- `propose_probe(code, test_plan)` writes `GeneratedProbe(code, test_plan)` and registers `ToolSpec(origin=generated, verified=False, rung=5, tier=read, tags=[])`. The policy file needs no change.
- Two runners behind one interface:
  - **Docker runner**: no network, read-only root, non-root user, CPU / memory / time limits, no mounts except an empty scratch dir. Pattern: `Repo2Run/build_agent/docker/` (Apache-2.0, commit 65042aa2). **This machine's Docker daemon is down**, so this runner is untested until it is started.
  - **Replay runner**: feeds a `ReplayDevice` transcript to the script's stdin and reads one JSON object from stdout. Works with no Docker, so it is the first one to build and test.
- The generated script must print one JSON object `{"facts":[{"key":..., "value":...}]}`. The harness wraps each fact in `Evidence(provider="generated:<hash>", probe=...)`; the script never writes facts itself.
- A generated probe may run on a real device only after the verifier passes it. Until then `decide()` returns `deny` for real targets.

## The verifier (clean context)

```python
@dataclass
class VerifierInput:
    test_plan: str; expected: str; probe_output: str   # read from raw_ref; no model reasoning, no code history
    safety_log: list[str]

@dataclass
class VerifierVerdict:
    passed: bool; unsafe: bool; reasons: list[str]

class Verifier(Protocol):
    async def verify(self, inp: VerifierInput) -> VerifierVerdict: ...
```

- One model call on a separate profile (`config/models.yaml`, name `verifier`), prompt built only from `VerifierInput`. Pattern: `BootstrapAgent/code/src/rethink/verifier/verifier.py` (MIT, a5ea466b).
- On `passed and not unsafe`: write the facts again with `created_by=verifier` and `status=verified` (the contract already allows exactly this), set the tool `verified=True`, create the `Skill`.
- Do not let the generating model grade itself. The tests (`test_plan` run against the replay transcript) are the judge; the verifier checks the test plan was actually exercised.

## Skills: store and reuse

- `Skill.signature` today is `usb:<vid>:<pid>`. Extend to `serial:<protocol>:<ident-prefix>` for protocol-level skills.
- `try_known_skill` (rung 1) already finds a matching `Skill`. Next: a `SkillAdapter` exposes each stored skill as `ToolSpec(origin=skill, rung=1, verified=True)` and runs its `code_ref` through the sandbox runner on the target; `verified_runs += 1` per success.
- The demo moment (second encounter uses the skill with no research): the menu already hides tools that ran and shows rung-1 tools first, so a skill appears at the top with no loop change.

## Tests to add first

1. Replay runner: a hand-written good probe passes; a probe that prints invalid JSON is rejected; a probe that tries a non-allowlisted command is blocked (`ReplayDevice.send` already raises).
2. Policy: a generated unverified tool is denied on a real target and allowed (logged) on `replay:`; after `verified=True` it follows its tier.
3. Verifier: a scripted verifier that passes yields `verified` facts and a `Skill`; one that fails leaves everything at `tested`.
4. The loop: scripted model runs `research` then `propose_probe` on `mystery-box` and ends `verified`; a second run on the same signature uses the skill at rung 1 with no research.
5. False-verified rate stays measured: add a fixture whose probe passes its own weak test but is wrong against ground truth, and check the scorer reports it.

## Update: P4 recipes and generated recipes
`remote/recipes.py` keeps a closed menu of eight built-in recipes. A recipe that comes from a generated probe is registered with `register_generated_recipe(...)` as `origin=generated, verified=False`; `decide_for_fleet` / `decide` deny it on real targets until a verifier sets `verified=True` (tested). The interfaces in this document did not change: the generated-recipe path only adds a ToolSpec, it needs no new contract.
