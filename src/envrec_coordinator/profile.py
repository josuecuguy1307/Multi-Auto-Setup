"""The owner's profile: what is confidential for him, his stance on cloud spend, local-first, the role of each machine, and what he chose to close.

The questions and their options are data (`skills/owner-profile/references/profile.json`); a model never writes them. The answers are stored in `<state>/coordinator/profile.json`, change only through `set`
(which validates against the schema and records a `profile_changed` event with the old value, so every change is undoable) and are read by two things only: the router's policy (`apply`) and the sensitivity of a task
(`level_for`). Nothing here asks the owner anything by itself: the services put the questions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SCHEMA_FILE = Path(__file__).resolve().parents[2] / "skills" / "owner-profile" / "references" / "profile.json"
LEVELS = ("public", "internal", "confidential")


def load_schema(path: Path | None = None) -> dict:
    return json.loads((path or SCHEMA_FILE).read_text(encoding="utf-8"))


class ProfileError(ValueError):
    """A value the schema does not allow; the text says what is allowed."""


class Profile:
    def __init__(self, path: Path, *, schema: dict | None = None, log=None, dynamic: dict | None = None) -> None:
        self.path, self.schema, self._log = Path(path), schema or load_schema(), log or (lambda t, p: None)
        self.dynamic = dynamic or {}  # name -> a function giving [{id, label}]: the options a field takes from data (`options_from`), such as the publishers of the verified models
        self.fields = {f["id"]: f for f in self.schema["fields"]}
        self.values: dict[str, Any] = {}
        self.roles: dict[str, str] = {}
        self.closed: dict[str, list[str]] = {"environments": [], "subtypes": [], "accesses": []}
        self.answered: set[str] = set()
        self.declined: list[str] = []  # environments the owner said "not now" to when the harness offered to clear them: not offered again on its own
        self.cleared: list[str] = []  # environments whose vendor terms for AUTOMATED use the owner read and accepted: his decision, recorded, undoable (`unclear`)
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
            self.values = {k: v for k, v in doc.get("values", {}).items() if k in self.fields}
            self.roles = {k: v for k, v in doc.get("roles", {}).items() if isinstance(v, str)}
            self.closed = {k: list(doc.get("closed", {}).get(k, [])) for k in self.closed}
            self.answered = set(doc.get("answered", [])) & set(self.fields)
            self.cleared = [x for x in doc.get("cleared", []) if isinstance(x, str)]
            self.declined = [x for x in doc.get("declined", []) if isinstance(x, str)]
        except (OSError, ValueError):
            pass

    # ---- reading --------------------------------------------------------------------------------------------------------------------------------

    def get(self, field: str) -> Any:
        self._reload_if_changed()
        if self.fields[field].get("stores") == "automated_use_cleared":
            return [x for x in self.cleared if x in self.option_ids(field)]  # one source of truth: the list the router reads
        return self.values.get(field, self.fields[field].get("default"))

    def _reload_if_changed(self) -> None:
        """The file is also written by the owner's terminal (`mas config`): a change there is read here at the next question, so one setting has one place and no restart."""
        try:
            m = self.path.stat().st_mtime_ns
        except OSError:
            return
        if m == getattr(self, "_mtime", 0):
            return
        self._mtime = m
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.values = {k: v for k, v in doc.get("values", {}).items() if k in self.fields}
        self.answered = set(doc.get("answered", [])) & set(self.fields)

    def applies(self, field: str) -> bool:
        f = self.fields[field]
        if f.get("skip_when_no_options") and not [o for o in self.options_of(field) if o["id"] != "none"]:
            return False  # nothing to choose between on these computers: the question is not asked
        cond = f.get("asked_when")
        if not cond:
            return True
        v = self.get(cond["field"])
        return v != cond["not"] if "not" in cond else v == cond.get("is")

    def missing(self) -> list[str]:
        """The fields the owner has not answered yet and that matter now, in the schema's order."""
        return [f["id"] for f in self.schema["fields"] if f["id"] not in self.answered and self.applies(f["id"]) and f.get("ask_first_run", True)]  # a presentation setting is asked when the owner wants to change it, not on the first run

    def options_of(self, field: str) -> list[dict]:
        """The options of a field: the ones it lists, or, for `options_from`, those the data gives now plus the "no preference" choice."""
        f = self.fields[field]
        if f.get("options_from"):
            fn = self.dynamic.get(f["options_from"])
            return [*(fn() if fn else []), {"id": "none", "label": f.get("none_label", "No preference")}]
        return list(f.get("options", []))

    def option_ids(self, field: str) -> list[str]:
        return [o["id"] for o in self.options_of(field)]

    def role_options(self) -> list[dict]:
        return list(self.schema["machine_roles"]["options"])

    def categories(self) -> list[str]:
        return self.option_ids("confidential")

    # ---- writing --------------------------------------------------------------------------------------------------------------------------------

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"values": self.values, "roles": self.roles, "closed": self.closed, "answered": sorted(self.answered), "cleared": self.cleared, "declined": self.declined}, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)
        try:
            self._mtime = self.path.stat().st_mtime_ns  # our own write is not a change to reload
        except OSError:
            pass

    def check(self, field: str, value: Any) -> Any:
        """The value as stored, or ProfileError saying what is allowed."""
        f = self.fields.get(field)
        if f is None:
            raise ProfileError(f"there is no profile field {field!r}; the fields are {', '.join(self.fields)}")
        kind = f["kind"]
        if kind == "multi":
            items = [value] if isinstance(value, str) else value
            if not isinstance(items, list) or not all(isinstance(x, str) for x in items):
                raise ProfileError(f"{field} is a list of: {', '.join(self.option_ids(field))}")
            items = [x for x in dict.fromkeys(items) if x not in ("none", "")]
            bad = [x for x in items if x not in self.option_ids(field)]
            if bad:
                raise ProfileError(f"{field}: {', '.join(bad)} is not one of {', '.join(self.option_ids(field))}")
            return items
        if kind == "single":
            if value not in self.option_ids(field):
                raise ProfileError(f"{field} is one of: {', '.join(self.option_ids(field))}")
            return value
        if kind == "number":
            if isinstance(value, bool) or not isinstance(value, int) or not f["min"] <= value <= f["max"]:
                raise ProfileError(f"{field} is a whole number from {f['min']} to {f['max']}")
            return value
        raise ProfileError(f"{field} cannot be set")

    def set(self, field: str, value: Any, *, by: str = "owner") -> Any:
        value = self.check(field, value)
        if self.fields[field].get("stores") == "automated_use_cleared":  # the answer lands in the list the router reads, one undoable change per tool
            for env in self.option_ids(field):
                if env != "none":
                    self.clear_automated_use(env, env in value, by=by)
            self.answered.add(field)
            self._save()
            return value
        old = self.values.get(field, "<unset>")
        self.values[field] = value
        self.answered.add(field)
        self._save()
        self._log("profile_changed", {"field": field, "old": old, "new": value, "by": by})
        return value

    def set_role(self, machine_id: str, role: str, *, by: str = "owner") -> str:
        if role not in {o["id"] for o in self.role_options()}:
            raise ProfileError(f"a machine's role is one of: {', '.join(o['id'] for o in self.role_options())}")
        old = self.roles.get(machine_id, "<unset>")
        self.roles[machine_id] = role
        self._save()
        self._log("profile_changed", {"field": f"role:{machine_id}", "old": old, "new": role, "by": by})
        return role

    def close(self, kind: str, ident: str, closed: bool = True, *, by: str = "owner") -> bool:
        """Close (or open again) a subtype, an environment or an access: the owner's choice, never a default. True when it changed."""
        if kind not in self.closed:
            raise ProfileError(f"what can be closed is one of: {', '.join(self.closed)}")
        have = ident in self.closed[kind]
        if have == closed:
            return False
        self.closed[kind] = [*self.closed[kind], ident] if closed else [x for x in self.closed[kind] if x != ident]
        self._save()
        self._log("profile_changed", {"field": f"closed:{kind}", "old": "closed" if have else "open", "new": "closed" if closed else "open", "id": ident, "by": by})
        return True

    def clear_automated_use(self, env_id: str, cleared: bool = True, *, by: str = "owner") -> bool:
        """The owner's decision that a vendor's CLI (a subscription he has) may be used automatically by the router. Nothing else fills this list. True when it changed."""
        have = env_id in self.cleared
        if have == cleared:
            return False
        self.cleared = [*self.cleared, env_id] if cleared else [x for x in self.cleared if x != env_id]
        self._save()
        self._log("profile_changed", {"field": "automated_use_cleared", "old": "cleared" if have else "not cleared", "new": "cleared" if cleared else "not cleared", "id": env_id, "by": by})
        return True

    def decline_clearance(self, env_id: str, *, by: str = "owner") -> bool:
        """"Not now" to the offer to clear a tool: remembered, so the offer is not made again on its own (`mas_profile clear` still can). True when it changed."""
        if env_id in self.declined:
            return False
        self.declined = [*self.declined, env_id]
        self._save()
        self._log("profile_changed", {"field": "clearance_declined", "old": "open", "new": "declined", "id": env_id, "by": by})
        return True

    # ---- what reads it --------------------------------------------------------------------------------------------------------------------------

    def apply(self, policy) -> None:
        """The router's policy as the profile makes it. The base values come from policy.json and are restored first, so closing something and opening it again leaves exactly the base."""
        base = getattr(policy, "_base", None)
        if base is None:
            base = policy._base = {"open_subtypes": set(policy.open_subtypes), "cost_order": tuple(policy.cost_order), "max_escalation": policy.max_escalation, "automated_use_cleared": set(policy.automated_use_cleared)}
        policy.open_subtypes = base["open_subtypes"] - set(self.closed["subtypes"])
        policy.automated_use_cleared = base["automated_use_cleared"] | set(self.cleared)
        policy.closed_environments = set(self.closed["environments"])
        policy.closed_accesses = set(self.closed["accesses"])
        policy.cloud_stance = self.get("cloud_spend")
        policy.cost_order = base["cost_order"] if self.get("local_first") == "local" else ("api", "subscription", "local")
        policy.max_escalation = min(base["max_escalation"], int(self.get("cloud_steps")) if policy.cloud_stance != "never" else base["max_escalation"])
        closes = {o["id"]: o["closes"] for o in self.role_options()}
        policy.machine_closes = {m: closes[r] for m, r in self.roles.items() if closes.get(r, "none") != "none"}

    def level_for(self, touches: Any) -> tuple[str | None, str]:
        """(level, why) for a task that states which categories it touches. None means the harness cannot tell and must ask the owner: the model's statement is never taken as `public` on its own."""
        if isinstance(touches, str):
            touches = [touches]
        if not isinstance(touches, list) or not touches or not all(isinstance(x, str) for x in touches) or "unsure" in touches:
            return None, "unclear"
        named = [x for x in touches if x != "none"]
        bad = [x for x in named if x not in self.categories()]
        if bad:
            return None, "unknown_category"
        hit = [x for x in named if x in self.get("confidential")]
        if hit:
            return "confidential", "category:" + ",".join(hit)
        if named:  # it touches something the owner did not list as confidential: it is not public either
            return "internal", "category:" + ",".join(named)
        d = self.get("default_sensitivity")
        return (None, "ask_each_time") if d == "ask" else (d, "owner_default")

    def summary(self) -> dict:
        return {"values": {f: self.get(f) for f in self.fields if self.applies(f)}, "answered": sorted(self.answered), "missing": self.missing(), "roles": dict(self.roles), "closed": {k: list(v) for k, v in self.closed.items()}, "clearance_declined": list(self.declined), "automated_use_cleared": list(self.cleared)}
