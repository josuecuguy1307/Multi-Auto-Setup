"""The dry run of a proposed recipe: the real executor over a recording transport that answers every command with success. It runs in this process (nothing reaches a device), and
proves what can be proven without one: every command renders, every write's undo is on the journal before it runs, the order is consistent, the undo replays newest first, and the
`absent` check is run last. What it cannot prove (that the commands do what they claim on the real operating system) is left to the owner's approval and to the checks after the run."""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

from envrec.recipes.executor import Artifact, RecipeExecutor, UndoJournal
from envrec.recipes.model import recipe_hash, render


def dry_run(recipe: dict) -> list[str]:
    """Problems found (empty: the dry run is clean)."""
    with tempfile.TemporaryDirectory(prefix="envrec-dry-") as d:
        files = {}
        for role in ("worker", "runtime", "model"):
            f = Path(d) / role
            f.write_bytes(role.encode())
            files[role] = Artifact(f, hashlib.sha256(role.encode()).hexdigest())
        journal = UndoJournal(Path(d) / "undo.jsonl")
        seen: list[tuple[list[str], int]] = []

        values = {"root": str(recipe.get("root")), "port": str(recipe.get("port"))}
        checks: dict[tuple, str] = {}
        for s in recipe["steps"]:
            if s.get("expect_sha256") and isinstance(s.get("run"), list):
                try:
                    checks[tuple(render(s["run"], values))] = s["expect_sha256"]
                except ValueError:
                    pass  # the executor reports the unrenderable step below

        def run_argv(argv, shell, stdin=None, timeout=60.0):
            seen.append((argv, len(journal.path.read_text().splitlines()) if journal.path.exists() else 0))
            role = checks.get(tuple(argv))  # a hash check prints the hash of the file that was sent, as a real one would
            return (0, files[role].sha256.encode(), b"") if role in files else (0, b"", b"")

        problems: list[str] = []
        try:
            ex = RecipeExecutor(recipe, recipe_hash(recipe), "dry", run_argv, lambda text: True, journal, files)
            results = ex.deploy()
            bad = [r for r in results if r.status != "ok"]
            if bad:
                return [f"step {bad[0].id}: {bad[0].status}: {bad[0].detail}"]
            writes = sum(1 for s in recipe["steps"] if s.get("write"))
            entries = len(journal.pending("dry"))
            if entries != writes:
                problems.append(f"{writes} write steps but {entries} undo entries were journaled")
            undone = ex.undo()
            if [r.id for r in undone] != [s["id"] for s in reversed(recipe["steps"]) if s.get("write")]:
                problems.append("the undo does not replay the writes newest first")
            if journal.pending("dry"):
                problems.append("an undo entry is still pending after the undo ran")
        except Exception as exc:  # a recipe that cannot even be rendered fails here with the reason
            problems.append(f"{type(exc).__name__}: {str(exc)[:160]}")
        return problems
