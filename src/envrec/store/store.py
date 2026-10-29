"""SQLite store for targets, facts, events, hypotheses and skills; raw outputs as files by sha256.

Events are append-only at the database level (triggers abort UPDATE and DELETE).
Facts are never edited: a new fact supersedes the old one and the old one stays in the chain.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from envrec.contracts import (
    CreatedBy,
    Event,
    Fact,
    FactStatus,
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
CREATE TABLE IF NOT EXISTS captures (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    path TEXT NOT NULL,
    name TEXT NOT NULL,
    size INTEGER NOT NULL,
    tool TEXT NOT NULL,
    event_id TEXT,
    source TEXT NOT NULL DEFAULT '',
    UNIQUE(target_id, sha256, name)
);
"""


LOADING = {"loading": True}  # facts already stored were accepted under the rules of their day: reading them back does not re-judge them


_INSERT_EVENT = "INSERT INTO events(id, ts, actor, type, run_id, json) VALUES(?, ?, ?, ?, ?, ?)"
_THREAD_WRITE = threading.Lock()


def _tagged(event: Event) -> Event:
    if event.tag is None:
        from envrec.contracts.tags import derive_tag
        event = event.model_copy(update={"tag": derive_tag(event.actor, event.type, event.payload)})
    return event


def _event_row(event: Event) -> tuple:
    return (event.id, event.ts.isoformat(), event.actor.value, event.type, event.run_id, event.model_dump_json())


def append_event_from_thread(root: Path | str, event: Event) -> Event:
    """Append one event to the store at `root` from ANY thread, through its own short-lived connection (a sqlite connection belongs to the thread that made it). For code that
    runs on a server thread and has to leave a record when something happens, whether or not a tool is running: the setup link's expiry, say. The events table is append-only
    either way (its triggers apply to this connection too)."""
    event = _tagged(event)
    with _THREAD_WRITE:
        db = sqlite3.connect(Path(root) / "envrec.db", timeout=10)
        try:
            with db:
                db.execute(_INSERT_EVENT, _event_row(event))
        finally:
            db.close()
    return event


class RawHashMismatch(Exception):
    """A raw file no longer matches the sha256 recorded in its RawRef."""


class Store:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "envrec.db")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        try:  # a store made before captures carried their source
            self.db.execute("ALTER TABLE captures ADD COLUMN source TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError:
            pass

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

    def has_claim_by_other(self, target_id: str, key: str, creator: CreatedBy) -> bool:
        """Is there, in the history of this (target, key), a fact whose creator is not `creator`?"""
        rows = self.db.execute("SELECT json FROM facts WHERE target_id = ? AND key = ?", (target_id, key)).fetchall()
        return any(Fact.model_validate_json(r["json"], context=LOADING).created_by != creator for r in rows)

    def write_fact(self, fact: Fact) -> Fact:
        """Reconcile with the current fact for (target, key) and return the fact that is now current.
        A verifier judges someone else's claim: a `verified` fact created by the verifier needs an earlier fact for the same key by another creator."""
        if fact.status == FactStatus.verified and fact.created_by == CreatedBy.verifier and not self.has_claim_by_other(fact.target_id, fact.key, CreatedBy.verifier):
            raise ValueError(f"the verifier cannot create the claim it verifies: {fact.key} has no earlier claim by another creator")
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
        return Fact.model_validate_json(row["json"], context=LOADING) if row else None

    def current_facts(self, target_id: str | None = None) -> list[Fact]:
        sql = "SELECT json FROM facts WHERE is_current = 1"
        args: tuple = ()
        if target_id is not None:
            sql += " AND target_id = ?"
            args = (target_id,)
        rows = self.db.execute(sql + " ORDER BY seq", args).fetchall()
        return [Fact.model_validate_json(r["json"], context=LOADING) for r in rows]

    def get_fact(self, fact_id: str) -> Fact | None:
        row = self.db.execute("SELECT json FROM facts WHERE id = ?", (fact_id,)).fetchone()
        return Fact.model_validate_json(row["json"], context=LOADING) if row else None

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
        event = _tagged(event)
        with self.db:
            self.db.execute(_INSERT_EVENT, _event_row(event))
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
        """Names of tools that have run here AND succeeded (from the append-only log). A failed attempt (a permission, an error) is not evidence and advances nothing."""
        return {e.payload.get("tool") for e in self.events(type="tool_result") if e.payload.get("target") == target_id and e.payload.get("ok") is True}

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

    def all_skills(self) -> list[Skill]:
        return [Skill.model_validate_json(r[0]) for r in self.db.execute("SELECT json FROM skills ORDER BY id").fetchall()]

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

    # ---- the capture index: which stored captures belong to which target (names and sizes only; the bytes stay in the raw store) ------------------------------

    def index_capture(self, target_id: str, ref: RawRef, name: str, size: int, tool: str, event_id: str | None = None, source: str = "") -> None:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO captures(target_id, sha256, path, name, size, tool, event_id, source) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                            (target_id, ref.sha256, ref.path, name[:64], size, tool[:64], event_id, source[:300]))

    def captures(self, target_id: str) -> list[dict]:
        """The captures kept for a target, oldest first: {sha256, name, size, tool}. No content."""
        rows = self.db.execute("SELECT sha256, path, name, size, tool, source FROM captures WHERE target_id = ? ORDER BY seq", (target_id,)).fetchall()
        return [dict(r) for r in rows]

    def resolve_capture(self, target_id: str, ref: str) -> RawRef:
        """The RawRef a capture id names, among THIS target's captures. The id is the sha256 or its first 8 or more hex digits. KeyError says why it did not resolve."""
        ref = ref.strip().lower()
        hits = {r["sha256"]: r["path"] for r in self.captures(target_id) if r["sha256"].startswith(ref)} if len(ref) >= 8 else {}
        if not hits:
            raise KeyError(f"no capture {ref[:16]!r} for this target; the CAPTURES list shows the ones that exist")
        if len(hits) > 1:
            raise KeyError(f"the capture id {ref[:16]!r} matches {len(hits)} captures; give more digits")
        sha, path = next(iter(hits.items()))
        return RawRef(path=path, sha256=sha)

    def delete_raw(self, ref: RawRef) -> bool:
        """Delete one stored raw output (for example a camera frame the human asked to have removed). Facts that cite it keep the hash and lose the bytes."""
        path = self.root / ref.path
        if path.is_file():
            path.unlink()
            return True
        return False

    def read_raw(self, ref: RawRef) -> bytes:
        path = self.root / ref.path
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != ref.sha256:
            raise RawHashMismatch(f"{ref.path}: content no longer matches sha256 {ref.sha256[:12]}…")
        return data
