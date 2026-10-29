---
name: authoring
description: Use when a device or a data format has no skill in the library and a new skill has to be written from a recorded capture.
---

# Writing a skill for this harness

A skill turns a capture of one kind of thing into facts that cite it. You write a DRAFT, one JSON object; code lays it out as a skill folder, runs your tests in a sandbox, and compares the result with what happens without your skill. If it does not beat that baseline, it is not kept. That is the point: a skill earns its place by making the result better, not by existing.

## 1. Start from the capture, not from the skill

Read the recorded capture the harness gives you before writing anything. Find the part that identifies the thing and the parts that carry values. Decide what you can state with certainty (a field printed in plain text) and what would be a guess (a name inferred from a vendor id). Only the first becomes a `tested` fact; a guess is a hypothesis, marked as one.

## 2. The draft

| field | what to put there |
|---|---|
| `name` | lowercase words joined by hyphens, the same as the device or format it handles |
| `description` | WHEN to use the skill, starting with "Use when". Triggers and symptoms only. Never how it works: a reader who sees the steps follows them instead of reading the body |
| `body` | instructions to the next model, in the imperative, saying WHY each thing matters. Under 500 lines; put long tables in a reference |
| `steps` | the registered tools that collect the capture (`run_command`, `read_file`, `serial_identify`, ...), with their arguments. Only registered tools exist; the tier of a tool may not exceed the skill's |
| `script` | Python defining `parse(inp)`; see `references/script-contract.md` |
| `produces` | the fact keys the script can write |
| `tests` | at least one recorded real capture with the facts it must produce (`match: true`), and one capture that must NOT match (`match: false`) |

## 3. The script

It runs alone in a sandbox: no network, no files but its own folder, a few seconds. It receives the captures and returns facts, entities and unknowns; when it needs a table lookup or another read it ASKS (`out.want(...)`) and is called again with the answer. It cannot write anything or call anything. Everything it returns goes through the privacy gate.

Every fact names the capture it came from (`src`) and, because you are a generated skill, QUOTES the text of that capture the value was read from (`quote`). The harness checks that the quote is really in the capture. A fact whose quote is not there is rejected whole, which is how invented values are kept out: you can only report what the capture says.

Keys look like `prefix.name` in the fixed namespace (`serial.protocol`, `sensor.humidity`). A status above what the evidence supports is not available to you: a value read from the capture is `tested`; anything inferred is `hypothesis=True`. Only the verifier can make a fact `verified`.

## 4. Tests from real captures

Copy a real capture into a test; do not compose one by hand to fit your script. The positive test lists the facts you expect, with their values. The negative test is a capture of something else (a different protocol, an error message, an empty reply) and must produce no fact at all: this is what stops a script that matches everything.

## 5. Run, judge, iterate

The harness reports each failure by name. Fix exactly what it names and run again; do not rewrite everything. When the tests pass, the baseline comparison runs: the same model on the same capture without the skill, then with it, judged by a verifier that sees only the evidence. Read its reasons; a skill that only restates what the model already got right is rejected.

## Mistakes the checks catch (so you can avoid them)

- A description that starts with anything but "Use when", or that lists steps (`then`, `first`, numbered lists, arrows).
- Fact keys outside the namespace, or with spaces or capitals.
- A fact without `src`, or whose `src` is a capture that failed.
- A value that was never in the capture (the quote is not there).
- A status stronger than the evidence: asserting a protocol from one ambiguous line.
- A tool above the skill's tier, a tool that is not registered, arguments the tool does not take.
- Tests copied from the script's own output instead of from a real capture, or no negative test.
- A script that crashes on input it did not expect: it is reported as an error, never as silence.
- Anything that tries to reach the network or write a file: the sandbox stops it and the skill is rejected.

## Where to look next

`references/script-contract.md` has the exact input and output of `parse`. `references/examples.md` names four finished skills, one for each shape of problem, so you can open the one that looks like yours.
