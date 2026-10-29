"""SQLite store for targets, facts, events, hypotheses and skills; raw outputs as files by sha256.

Events are append-only at the database level (triggers abort UPDATE and DELETE).
Facts are never edited: a new fact supersedes the old one and the old one stays in the chain.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from envrec.contracts import (
    Event,
    Fact,
    Hypothesis,
    HypothesisOutcome,
    RawRef,
    Skill,
    Target,
    reconcile,
)

M = TypeVar("M", bound=BaseModel)

SCHEMA = """
CREATE TABLE IF NOT EXISTS targets (id TEXT PRIMARY KEY, json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS facts (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT UNIQUE NOT NULL,
    target_id TEXT NOT NULL,
    key TEXT NOT NULL,
    status TEXT NOT NULL,
    is_current INTEGER NOT NULL,
    supersedes TEXT,
    json TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_current_fact ON facts(target_id, key) WHERE is_current = 1;
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT UNIQUE NOT NULL,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    type TEXT NOT NULL,
    run_id TEXT,
    json TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TABLE IF NOT EXISTS hypotheses (id TEXT PRIMARY KEY, target_id TEXT NOT NULL, json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS skills (id TEXT PRIMARY KEY, signature TEXT NOT NULL, json TEXT NOT NULL);
"""


class RawHashMismatch(Exception):
    """A raw file no longer matches the sha256 recorded in its RawRef."""


class Store:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "envrec.db")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- targets ---------------------------------------------------------------------------

    def put_target(self, target: Target, *, from_config: bool = False) -> Target:
        """`authorized=True` is accepted only when the caller says it came from the config file."""
        if target.authorized and not from_config:
            raise PermissionError("authorized targets can only be registered from the human config file")
        existing = self.get_target(target.id)
        if existing and existing.authorized and not target.authorized and not from_config:
            target = target.model_copy(update={"authorized": True})  # discovery never revokes config
        with self.db:
            self.db.execute(
                "INSERT INTO targets(id, json) VALUES(?, ?) "
                "ON CONFLICT(id) DO UPDATE SET json = excluded.json",
                (target.id, target.model_dump_json()),
            )
        return target

    def get_target(self, target_id: str) -> Target | None:
        row = self.db.execute("SELECT json FROM targets WHERE id = ?", (target_id,)).fetchone()
        return Target.model_validate_json(row["json"]) if row else None

    def list_targets(self) -> list[Target]:
        rows = self.db.execute("SELECT json FROM targets ORDER BY rowid").fetchall()
        return [Target.model_validate_json(r["json"]) for r in rows]

    # ---- facts -----------------------------------------------------------------------------

    def write_fact(self, fact: Fact) -> Fact:
        """Reconcile with the current fact for (target, key) and return the fact that is now current."""
        current = self.current_fact(fact.target_id, fact.key)
        result = reconcile(current, fact)
        if not result.changed:
            assert current is not None
            return current
        with self.db:
            if result.superseded is not None:
                self.db.execute("UPDATE facts SET is_current = 0 WHERE id = ?", (result.superseded.id,))
            f = result.fact
            self.db.execute(
                "INSERT INTO facts(id, target_id, key, status, is_current, supersedes, json) "
                "VALUES(?, ?, ?, ?, 1, ?, ?)",
                (f.id, f.target_id, f.key, f.status.value, f.supersedes, f.model_dump_json()),
            )
        return result.fact

    def current_fact(self, target_id: str, key: str) -> Fact | None:
        row = self.db.execute(
            "SELECT json FROM facts WHERE target_id = ? AND key = ? AND is_current = 1", (target_id, key)
        ).fetchone()
        return Fact.model_validate_json(row["json"]) if row else None

    def current_facts(self, target_id: str | None = None) -> list[Fact]:
        sql = "SELECT json FROM facts WHERE is_current = 1"
        args: tuple = ()
        if target_id is not None:
            sql += " AND target_id = ?"
            args = (target_id,)
        rows = self.db.execute(sql + " ORDER BY seq", args).fetchall()
        return [Fact.model_validate_json(r["json"]) for r in rows]

    def get_fact(self, fact_id: str) -> Fact | None:
        row = self.db.execute("SELECT json FROM facts WHERE id = ?", (fact_id,)).fetchone()
        return Fact.model_validate_json(row["json"]) if row else None

    def fact_chain(self, fact_id: str) -> list[Fact]:
        """The fact and everything it superseded, newest first."""
        chain: list[Fact] = []
        seen: set[str] = set()
        fid: str | None = fact_id
        while fid and fid not in seen:
            seen.add(fid)
            fact = self.get_fact(fid)
            if fact is None:
                break
            chain.append(fact)
            fid = fact.supersedes
        return chain

    # ---- events (append-only) --------------------------------------------------------------

    def append_event(self, event: Event) -> Event:
        with self.db:
            self.db.execute(
                "INSERT INTO events(id, ts, actor, type, run_id, json) VALUES(?, ?, ?, ?, ?, ?)",
                (event.id, event.ts.isoformat(), event.actor.value, event.type, event.run_id,
                 event.model_dump_json()),
            )
        return event

    def events(self, *, run_id: str | None = None, type: str | None = None) -> list[Event]:
        sql, args = "SELECT json FROM events WHERE 1=1", []
        if run_id is not None:
            sql += " AND run_id = ?"
            args.append(run_id)
        if type is not None:
            sql += " AND type = ?"
            args.append(type)
        rows = self.db.execute(sql + " ORDER BY seq", args).fetchall()
        return [Event.model_validate_json(r["json"]) for r in rows]

    def tools_run(self, target_id: str) -> set[str]:
        """Names of tools that have already run on this target (from the append-only event log)."""
        return {e.payload.get("tool") for e in self.events(type="tool_run") if e.payload.get("target") == target_id}

    # ---- hypotheses and skills -------------------------------------------------------------

    def put_hypothesis(self, hyp: Hypothesis) -> Hypothesis:
        with self.db:
            self.db.execute(
                "INSERT INTO hypotheses(id, target_id, json) VALUES(?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET json = excluded.json",
                (hyp.id, hyp.target_id, hyp.model_dump_json()),
            )
        return hyp

    def resolve_hypothesis(self, hyp_id: str, outcome: HypothesisOutcome) -> Hypothesis:
        row = self.db.execute("SELECT json FROM hypotheses WHERE id = ?", (hyp_id,)).fetchone()
        if row is None:
            raise KeyError(hyp_id)
        hyp = Hypothesis.model_validate_json(row["json"]).model_copy(update={"outcome": outcome})
        return self.put_hypothesis(hyp)

    def hypotheses(self, target_id: str | None = None) -> list[Hypothesis]:
        sql, args = "SELECT json FROM hypotheses", ()
        if target_id is not None:
            sql, args = sql + " WHERE target_id = ?", (target_id,)
        rows = self.db.execute(sql + " ORDER BY rowid", args).fetchall()
        return [Hypothesis.model_validate_json(r["json"]) for r in rows]

    def put_skill(self, skill: Skill) -> Skill:
        with self.db:
            self.db.execute(
                "INSERT INTO skills(id, signature, json) VALUES(?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET json = excluded.json",
                (skill.id, skill.signature, skill.model_dump_json()),
            )
        return skill

    def find_skill(self, signature: str) -> Skill | None:
        row = self.db.execute("SELECT json FROM skills WHERE signature = ?", (signature,)).fetchone()
        return Skill.model_validate_json(row["json"]) if row else None

    # ---- raw outputs -----------------------------------------------------------------------

    def put_raw(self, data: bytes | str) -> RawRef:
        raw = data.encode("utf-8") if isinstance(data, str) else data
        sha = hashlib.sha256(raw).hexdigest()
        rel = f"raw/{sha[:2]}/{sha}"
        path = self.root / rel
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(raw)
            os.replace(tmp, path)
        return RawRef(path=rel, sha256=sha)

    def read_raw(self, ref: RawRef) -> bytes:
        path = self.root / ref.path
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != ref.sha256:
            raise RawHashMismatch(f"{ref.path}: content no longer matches sha256 {ref.sha256[:12]}…")
        return data
