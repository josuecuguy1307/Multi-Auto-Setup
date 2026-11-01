"""The harness core API of the Harness App (docs/HARNESS_APP_STANDARD.md): runs, a typed stream of intents, and the owner's decisions, for every chat the owner works from.

It adds NO socket: it is served by the owner's loopback screen under `/harness/v1/` (web.py). Each chat adapter holds its own token file and is known to the core by it, so a channel is never a claim
a client types. Adapters translate; every rule below is here.

Who may decide what (the rules the tests prove):
* a choice that writes nothing may come from any channel, as an OWNER event tagged with its channel (`relayed_by_model` when a chat model sits in the middle);
* a WRITE is never approved by a tool call. It needs a *confirmation*: a ticket bound to the exact plan hash, answered by something a model cannot press: the host's own elicitation UI, the
  chat's own confirmation dialog (Open WebUI with our Pipe, no other model in the middle), a native dialog of the operating system raised by this coordinator, or a click on the owner panel;
* a confirmation for a plan hash that is not the intent's current hash is rejected, whatever the mechanism;
* a setup step (run or undo) is a pending decision of the run that only a human gesture resolves: the panel, the system dialog this coordinator raises, or the chat's own dialog. The coordinator refuses
  an approval that only says "owner" (`owner_authority_refused`), so no tool path of a chat can produce one;
* a cloud channel receives summaries only: nothing `confidential`, identifiers filtered, size capped, every egress logged."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field

import envrec_os.confirm as osconfirm
import envrec_os.secret_prompt as ossecret
from envrec_coordinator import agent as A
from envrec_coordinator import approval
from envrec_coordinator.runs import SHOWN, describe_event
from envrec_harness import cards
from envrec_harness.cards import fmt_value, human_bytes
from envrec_harness.intents import ACTORS, RANK, Intent, IntentInvalid, for_display, numbered, validate
from envrec_harness.strings import STR, humanize, label_for, t

CLOUD_DROP_KEYS = {"fingerprint", "ip", "address", "addresses", "path", "paths", "token", "secret", "cert", "key_path", "mac", "serial"}
MAX_CLOUD_BYTES = 48000  # a map with each machine's card can be large; what leaves is still filtered and logged
TICKET_TTL_S = 150.0
HOST_ANSWERED = ("host_elicitation", "host_dialog", "panel_click", "cli_typed")  # answered in a UI a model does not drive, delivered by trusted code (the adapter, or the panel)
OS_DIALOG = "os_dialog"
FEED_GAP_S = 2.0
FEED_MAX_LINES = 6  # a flush shows at most this many lines; the ordinary ones beyond that are merged into one
SETUP_TOOLS = ("run_step", "undo_step")  # their steps tell their own story (each state, with its size and time), so their tool calls add no line of their own
SETUP_KINDS = ("install", "uninstall", "connect")  # the runs that put a step in front of the owner: a setup and the undo of one
KINDS = ("laptop", "desktop", "server", "oldpc", "computer")


@dataclass(frozen=True)
class Channel:
    name: str
    label: str
    cloud: bool  # a cloud chat only ever receives summaries
    model_in_middle: bool  # a model that is not ours decides what to call: a tool call from it is never the owner
    mechanisms: tuple[str, ...]  # how a write may be confirmed from this channel, in order of preference


CHANNELS = {
    "panel": Channel("panel", "the owner panel", False, False, ("panel_click",)),
    "claude-desktop": Channel("claude-desktop", "Claude Desktop", True, True, ("host_elicitation", OS_DIALOG)),
    "openwebui": Channel("openwebui", "Open WebUI", False, False, ("host_dialog", OS_DIALOG)),
    "cli": Channel("cli", "the owner's terminal", False, False, ("cli_typed", OS_DIALOG)),  # `mas connect`: the owner types their own answer at their own terminal; no model sits between
    # A terminal agent (Claude Code, Codex CLI, Gemini CLI, opencode...) connects through the same stdio adapter. Its model decides what to call, so a tool call is never the owner; it has no dialog of its own that our
    # Pipe could trust, so a write is confirmed by an elicitation form when its handshake declares one, and otherwise by the operating system's dialog. `terminal` assumes a provider model (a cloud chat: summaries only);
    # `terminal-local` is for an agent whose model runs on this computer.
    "terminal": Channel("terminal", "a terminal agent", True, True, ("host_elicitation", OS_DIALOG)),
    "terminal-local": Channel("terminal-local", "a terminal agent with a local model", False, True, ("host_elicitation", OS_DIALOG)),
}


@dataclass
class Ticket:
    id: str
    intent_id: str
    plan_hash: str
    mechanism: str
    channel: str
    created: float
    state: str = "waiting"  # waiting | approved | declined | rejected | expired | timeout | unavailable
    reason: str = ""
    assurance: str = ""  # strong (Touch ID, account password, Windows Hello) | host_ui | weak (a button only)


@dataclass
class Pending:
    intent: Intent
    future: asyncio.Future = field(default_factory=lambda: asyncio.get_running_loop().create_future())


class Refused(Exception):
    def __init__(self, status: int, reason: str, message: str = "") -> None:
        super().__init__(reason)
        self.status, self.reason, self.message = status, reason, message


def _drop(v):
    if isinstance(v, dict):
        return {k: _drop(x) for k, x in v.items() if k not in CLOUD_DROP_KEYS}
    if isinstance(v, list):
        return [_drop(x) for x in v]
    return v


class HarnessCore:
    def __init__(self, coord, *, native_confirm=None, native_notice=None, native_secret=None, lang: str = "en", clock=None) -> None:
        self.coord, self.lang, self.clock = coord, lang, clock or time.time
        self.native_confirm = native_confirm or osconfirm.native_confirm
        self.native_notice = native_notice or osconfirm.native_notice  # information for the owner's own screen: the pairing code goes here, never into a tool result
        self.native_secret = native_secret or ossecret.native_secret
        self._code_notices: dict[str, object] = {}  # offer id -> the dialog that shows its code
        self.raise_dialogs = True  # the harness opens the owner's dialog by itself for what a chat started; a test world that plays the owner through the tools turns it off
        self._pair_chat: dict[str, Channel] = {}  # offer id -> the chat channel that asked for the code: only such an offer gets its confirmation raised by the harness
        self._secrets: dict[str, str] = {}  # a value typed into the hidden window, by ticket: held in memory until the one write task takes it, never logged, never put in an intent
        self.dir = coord.state / "harness"
        self.dir.mkdir(parents=True, exist_ok=True)
        coord.layer.protect_private(self.dir)
        self.tokens = {name: self._token(name) for name in CHANNELS if name != "panel"}
        self.caps: dict[str, dict] = {}
        self._logs: dict[str, list[Intent]] = {}
        self._cursor: dict[str, int] = {}
        self.feed_gap_s = FEED_GAP_S  # at most one flush of ordinary lines per this many seconds, per run
        self._feed: list[Intent] = []  # the activity lines of every run and task, oldest first: what a chat or the panel reads to follow the machines
        self._buf: dict[str, list[dict]] = {}  # lines waiting for the rate limit, by run
        self._last_flush: dict[str, float] = {}
        self._held: dict[str, dict] = {}  # a tool call in flight: who chose it and who ran it, until its result says how it went
        self._seen_steps: dict[str, set] = {}
        self._ended: set[str] = set()
        self._act_n = 0
        self._task_seen: dict[str, str] = {}
        self._done: set[str] = set()
        self._seq = 0
        self._pending: dict[str, Pending] = {}  # confirmations waiting: intent id -> intent and the future its protocol awaits
        self._tickets: dict[str, Ticket] = {}
        self._egress_logged: set[tuple[str, str]] = set()
        self._run_sens: dict[str, str] = {}
        self._os_busy = False
        self.kinds_file = coord.state / "node-kinds.json"

    # ---- who is asking ------------------------------------------------------------------------------------------------------------------------

    def _token(self, name: str) -> str:
        path = self.dir / f"{name}.token"
        if not path.is_file():
            path.write_text(secrets.token_urlsafe(32))
        self.coord.layer.protect_private(path)
        return path.read_text().strip()

    def authenticate(self, bearer: str) -> Channel | None:
        """The channel that holds this token, or None. Constant-time, and the token is never logged."""
        found = None
        for name, tok in self.tokens.items():
            if hmac.compare_digest(bearer.encode(), tok.encode()):
                found = CHANNELS[name]
        return found

    def write_endpoint(self, port: int) -> None:
        (self.dir / "endpoint.json").write_text(json.dumps({"host": "127.0.0.1", "port": port}))
        self.coord.layer.protect_private(self.dir / "endpoint.json")

    def declare(self, ch: Channel, caps: dict) -> dict:
        """What the chat's live handshake said it can do (elicitation, MCP Apps, iframe forms...). Stored as declared by the adapter; used only to choose among the confirmations the channel allows."""
        clean = {k: v for k, v in caps.items() if isinstance(k, str) and len(k) < 40 and isinstance(v, (bool, str, int))}
        self.caps[ch.name] = clean
        self.coord.log.add("harness_channel_capabilities", {"channel": ch.name, "capabilities": clean})
        return self.capabilities(ch)

    def mechanisms(self, ch: Channel) -> list[str]:
        out = []
        for m in ch.mechanisms:
            if m == "host_elicitation" and not self.caps.get(ch.name, {}).get("elicitation"):
                continue
            if m in ("host_dialog", "cli_typed") and not self.caps.get(ch.name, {}).get("event_call"):
                continue
            if m == OS_DIALOG and not osconfirm.available()["available"]:
                continue
            out.append(m)
        return out

    def capabilities(self, ch: Channel) -> dict:
        return {"channel": ch.name, "label": ch.label, "cloud": ch.cloud, "model_in_middle": ch.model_in_middle, "declared": self.caps.get(ch.name, {}),
                "confirmations": self.mechanisms(ch), "fallback": "panel_click", "lang": self.lang}

    # ---- intents -----------------------------------------------------------------------------------------------------------------------------

    def _add(self, run_id: str | None, kind: str, actor: str, sens: str, payload: dict, text: str, key: str) -> Intent:
        self._seq += 1
        i = validate(Intent(id=key, kind=kind, run_id=run_id, actor=actor if actor in ACTORS else "GUARD", sensitivity=sens, payload=payload, text_fallback=text, seq=self._seq, ts=self.clock()))
        self._logs.setdefault(run_id or "-", []).append(i)
        return i

    def _sens(self, run_id: str | None) -> str:
        return self._run_sens.get(run_id or "", "internal")

    def _label_rows(self, group: dict, states: dict, seen: set | None = None) -> list[dict]:
        rows = [{"key": k, "label": label_for(self.lang, k), "value": humanize(self.lang, k, v), "status": states.get(k, "unknown")} for k, v in group.items() if seen is None or k not in seen]
        if seen is not None:
            seen.update(r["key"] for r in rows)  # a fact is shown once, in the first group that holds it
        return rows

    def machine_data(self, run) -> dict:
        res = run.result or {}
        g, st = res.get("groups") or {}, res.get("evidence_state") or {}
        return {"name": run.node_name, "os": (g.get("system") or {}).get("machine.os.name", ""), "label": res.get("label", ""),
                "groups": {name: self._label_rows(g.get(name) or {}, st, seen) for seen in [set()] for name in ("system", "hardware", "ai")},
                "findings": res.get("findings") or []}

    def sync(self, run) -> list[Intent]:
        """Bring the run's intent log up to date with what happened (idempotent: an intent is added once). Returns the log."""
        log = self._logs.setdefault(run.id, [])
        have = {i.id for i in log}
        sens = self._sens(run.id)
        if run.kind in SETUP_KINDS:
            self._install_plan(run, have, sens)
            have = {i.id for i in log}
        if run.store is not None:
            rows = [e for e in run.store.events() if e.type in SHOWN]
            for n in range(self._cursor.get(run.id, 0), len(rows)):
                self._buf.setdefault(run.id, []).extend(self._lines_of(run, rows[n]))
            self._cursor[run.id] = len(rows)
            if run.finished and run.id not in self._ended:  # the final state is always shown, once, in the words of the run's own status
                self._ended.add(run.id)
                end = t(self.lang, "act_end").format(machine=run.node_name, kind=run.kind, status=run.status) + (f" — {fmt_value(run.reason, 160)}" if run.reason else "")
                self._buf.setdefault(run.id, []).append({"actor": "GUARD", "text": end, "important": True, "short": None})
            self._flush(run, sens)
        p = run.adapter.pending if run.adapter is not None else None
        for i in log:
            if i.extra.get("adapter_q") and i.extra.get("state") == "pending" and not (p and p.qid == i.payload["question"]):
                i.extra["state"] = "answered"  # the run's own question was answered or timed out; the owner's pick is not the run's question and is resolved below
        if p is not None and p.data.get("kind") not in ("approve_step", "approve_plan") and f"{run.id}:q:{p.qid}" not in have:
            opts = [{"id": o["id"], "label": str(o.get("label", o["id"])), "detail": str(o.get("detail", ""))} for o in p.options]
            i = self._add(run.id, "ask_choice", "GUARD", sens, {"question": p.qid, "text": p.text, "options": opts, "writes": False}, f"{p.text}\n{numbered(opts)}\n{t(self.lang, 'reply_hint')}", f"{run.id}:q:{p.qid}")
            i.extra.update(state="pending", adapter_q=True)
        if run.finished and run.result and run.id not in self._done:  # the end of a run is told once (it used to be told again on every poll of an options run: the list twice)
            self._done.add(run.id)
            if run.kind == "analysis" and run.result.get("agent", True):
                d = self.machine_data(run)
                self._add(run.id, "show_data", "GUARD", sens, {"data_kind": "machine", "data": d}, cards.fallback_text("machine", d, self.lang), f"{run.id}:data:machine")
            opts = sorted(run.result.get("options") or [], key=lambda o: o["id"])  # an unordered set: the order is by id, never by preference
            if opts:
                d = {"options": [{**o, "provisional": True} for o in opts], "picked": run.picked_option, "pickable": True, "intent_id": f"{run.id}:pick", "no_recommendation": True}
                self._add(run.id, "show_data", "GUARD", sens, {"data_kind": "options", "data": d}, cards.fallback_text("options", d, self.lang), f"{run.id}:data:options")
                pick = [{"id": o["id"], "label": o.get("title", o["id"]), "detail": ""} for o in opts]
                i = self._add(run.id, "ask_choice", "GUARD", sens, {"question": "pick_option", "text": t(self.lang, "pick_text"), "options": pick, "writes": False},
                              f"{t(self.lang, 'pick_text')}\n{numbered(pick)}\n{t(self.lang, 'reply_hint')} {t(self.lang, 'no_reco')}", f"{run.id}:pick")
                i.extra["state"] = "pending"
            if f"{run.id}:end" not in {x.id for x in log}:
                view = self.coord.runs.view(run.id) or {}
                d = {"run": {"id": run.id, "kind": run.kind, "node": run.node_name, "status": run.status, "reason": run.reason}, "events": [{"tag": e["tag"], "text": e["text"]} for e in view.get("events", [])], "actors": view.get("actors", {})}
                self._add(run.id, "show_data", "GUARD", sens, {"data_kind": "run", "data": d}, cards.fallback_text("run", d, self.lang), f"{run.id}:end")
        if run.picked_option:
            for i in log:
                if i.id == f"{run.id}:pick":
                    i.extra["state"] = "answered"
        if run.kind in SETUP_KINDS:
            self._sync_install(run, log, {i.id for i in log}, sens)
        return log

    # ---- preparing a machine: the setup run ----------------------------------------------------------------------------------------------------------

    def _facts(self, run) -> dict:
        if run.store is None or run.target is None:
            return {}
        return {f.key: f.value for f in run.store.current_facts(run.target.id)}

    def plan_data(self, run, view: dict) -> dict:
        """The setup plan as the card and the text show it (the run view's plan, with the machine and option by name and the memory numbers the run re-checked)."""
        pl, facts = view["plan"], self._facts(run)
        d = self._plan_data(run, view, pl, facts)
        return {**d, **({"notes": [for_display(n) for n in d["notes"]]} if d.get("notes") else {}), "steps": [{**x, **({"exact_command": for_display(x["exact_command"])} if "exact_command" in x else {}), "undo": for_display(x.get("undo", "")), "writes": [for_display(str(w)) for w in x.get("writes", [])]} for x in d["steps"]]}

    def _plan_data(self, run, view: dict, pl: dict, facts: dict) -> dict:
        if run.kind == "connect":
            steps = [{"n": s["n"], "title": s["title"], "kind": s["kind"], "exact_command": s["command"], "writes": s["writes"], "bytes": s["bytes"], "size_kind": s.get("size_kind"), "undo": s["undo"], "needs_owner_approval": s["needs_owner_approval"],
                      "status": s["status"]} for s in pl["steps"]]
            return {"connect": True, "plan_title": pl.get("plan_title"), "plan_id": pl["plan_id"], "plan_hash": pl["hash"], "machine": run.node_name, "machine_id": pl["machine"], "option_id": pl["option"], "option_title": pl.get("option_title") or pl["option"],
                    "sensitivity": pl["sensitivity"], "fit": {}, "total_download_bytes": pl["total_download_bytes"], "total_moved_bytes": pl.get("total_moved_bytes", 0), "steps": steps, "notes": pl.get("notes", []), "verification": pl["verification"], "can_approve_here": True}
        if run.kind == "uninstall":  # only the steps that are done, newest first, each with the undo that plan wrote
            todo = [x for x in reversed(pl["steps"]) if x["n"] in getattr(run.adapter, "offered", [])]
            return {"undo_plan": True, "plan_hash": pl["hash"], "machine": run.node_name, "machine_id": pl["machine"], "option_id": pl["option"], "option_title": ((self.coord.setup_of(run.node_id) or {}).get("option_title") or pl["option"]),
                    "of": len(pl["steps"]), "steps": [{"n": x["n"], "title": x["title"], "undo": x["undo"], "writes": x["writes"], "bytes": x["bytes"], "needs_owner_approval": True, "status": x["status"]} for x in todo], "can_approve_here": True}
        rc, pick = facts.get("fit.rechecked") or {}, self.coord.pick_of(run.node_id) or {}
        fit = {"fits": rc.get("fits", (pl.get("fit") or {}).get("fits")), "needed_gib": rc.get("needs_gib"), "budget_gib": rc.get("budget_gib"), "basis": rc.get("basis") or (pl.get("fit") or {}).get("limited_by")}
        steps = [{"n": s["n"], "title": s["title"], "kind": s["kind"], "exact_command": s["command"], "writes": s["writes"], "bytes": s["bytes"], "undo": s["undo"],
                  "needs_owner_approval": s["needs_owner_approval"], "status": s["status"]} for s in pl["steps"]]
        return {"plan_id": pl["plan_id"], "plan_hash": pl["hash"], "machine": run.node_name, "machine_id": pl["machine"], "option_id": pl["option"], "option_title": pick.get("title") or pl["option"],
                "sensitivity": pl["sensitivity"], "fit": fit, "total_download_bytes": pl["total_download_bytes"], "steps": steps, "verification": pl["verification"], "can_approve_here": True}

    # ---- the live activity: one line per thing that happened, merged and rate-limited ---------------------------------------------------------------------

    def _lines_of(self, run, e) -> list[dict]:
        """The lines one recorded event adds to the activity of a run. A tool call is one line (who chose it, who ran it, how it went); a setup step says each state that matters. `important` lines are never held back."""
        m, p, held = run.node_name, e.payload, self._held.setdefault(run.id, {})
        L = lambda actor, text, important=False, short=None: [{"actor": actor if actor in ACTORS else "GUARD", "text": text, "important": important, "short": short}]  # noqa: E731
        s = lambda key, **kw: t(self.lang, key).format(machine=m, **kw)  # noqa: E731
        if e.type == "run_requested":
            return L(e.effective_tag, s("act_started", kind=run.kind), True)
        if e.type == "action_chosen":
            if p.get("tool"):
                held.update(tool=p["tool"], chooser=e.effective_tag, runner="GUARD")
                return []
            return L(e.effective_tag, s("act_choice", actor=e.effective_tag, action=p.get("action")))
        if e.type == "tool_run":
            held["runner"] = e.effective_tag
            return []
        if e.type == "tool_result":
            tool, chooser, runner = p.get("tool"), held.get("chooser", "MODEL"), held.get("runner", "GUARD")
            held.clear()
            ok = p.get("ok", True)
            if tool in SETUP_TOOLS and ok:
                return []
            facts = p.get("facts")
            n = len(facts) if isinstance(facts, (list, tuple, dict)) else facts
            outcome = t(self.lang, "act_ok") if ok else t(self.lang, "act_failed").format(error=fmt_value(p.get("error"), 80))
            return L(chooser, s("act_tool", tool=tool, outcome=outcome, facts=t(self.lang, "act_facts").format(n=n) if n is not None and ok else "", chooser=chooser, runner=runner), not ok, f"{tool} {outcome}")
        if e.type == "protocol_state":
            return L("GUARD", s("act_protocol", from_=p.get("from"), to=p.get("to")))
        if e.type == "owner_answer":
            return L("OWNER", s("act_answer", question=p.get("question"), option=p.get("option")), True)
        if e.type == "owner_pick_option":
            return L("OWNER", s("act_pick", option=p.get("option")), True)
        if e.type == "owner_confirmation":
            return L("OWNER", s("act_confirm", verdict=t(self.lang, "act_approved" if p.get("approved") else "act_declined"), plan_hash=str(p.get("plan_hash", ""))[:19], mechanism=p.get("mechanism")), True)
        if e.type == "run_end":
            return []  # told once the run's final state is known (the line after the last event)
        if e.type in ("action_rejected", "protocol_refused", "policy_decision"):
            return L("GUARD", s("act_refused", what=e.type.replace("_", " "), reason=fmt_value(p.get("reason") or p.get("decision") or p.get("action"), 120)), True)
        if e.type == "code_only_probe":
            return L("SCRIPT", s("act_probe", probe=p.get("probe"), status=p.get("status")))
        if e.type == "setup_plan":
            text, actor = self._event_text(run, e)
            return L(actor, text, True)
        if e.type == "setup_step":
            st, key = str(p.get("state")), (p.get("step"), p.get("action"))
            seen = self._seen_steps.setdefault(run.id, set())
            if st == "accepted" or (st in ("running", "sent") and (key, "go") in seen):
                return []  # the same stretch of one step is said once: "sent" (or "running"), then how it ended
            if st in ("running", "sent"):
                seen.add((key, "go"))
            text, actor = self._event_text(run, e)
            if st == "done" and p.get("duration_s") is not None:
                text += t(self.lang, "act_in").format(s=round(float(p["duration_s"]), 1))
            return L(actor, text, st not in ("sent", "running"))
        return []

    def _flush(self, run, sens: str) -> None:
        """Turn the waiting lines of a run into intents, under the rate limit: ordinary lines wait for the gap (or for an important line, or for the end of the run); a long wait comes out as at most FEED_MAX_LINES lines,
        the ordinary ones beyond that merged into one. The end of a run and every question or decision are never held back."""
        lines = self._buf.get(run.id) or []
        if not lines:
            return
        now = self.clock()
        if not (any(x["important"] for x in lines) or run.finished or now - self._last_flush.get(run.id, 0.0) >= self.feed_gap_s):
            return
        self._buf[run.id] = []
        self._last_flush[run.id] = now
        if len(lines) > FEED_MAX_LINES:
            ordinary = [x for x in lines if not x["important"]]
            keep = max(FEED_MAX_LINES - sum(1 for x in lines if x["important"]), 1)
            if len(ordinary) > keep:
                merged = ordinary[: len(ordinary) - keep + 1]
                items = "; ".join(f"{x['actor']} {x.get('short') or x['text'].split(' · ', 1)[-1]}" for x in merged)  # who did each, in the summary too
                actors = {x["actor"] for x in merged}
                summary = {"actor": actors.pop() if len(actors) == 1 else "GUARD", "text": t(self.lang, "act_more").format(machine=run.node_name, n=len(merged), items=fmt_value(items, 300)), "important": False}
                first = lines.index(merged[0])
                lines = [x for x in lines if x not in merged]
                lines.insert(first, summary)
        for x in lines:
            self._act_n += 1
            i = self._add(run.id, "progress", x["actor"], sens, {"type": "activity", "important": x["important"]}, x["text"], f"{run.id}:a{self._act_n}")
            self._feed.append(i)

    def _task_lines(self) -> None:
        """The tasks nobody's run owns (the owner asked for one from the panel or a command): one line when it was sent and one when it ended. A run's own tasks are told by the run."""
        for rec in self.coord.tasks.list():
            if rec.by == "GUARD":
                continue
            v = rec.view()
            st = str(v.get("status"))
            if self._task_seen.get(rec.id) == st or (st in ("accepted", "running") and rec.id in self._task_seen):
                continue
            self._task_seen[rec.id] = st
            node = self.coord.registry.get(v.get("node"))
            self._act_n += 1
            i = self._add(None, "progress", rec.by if rec.by in ACTORS else "OWNER", "internal", {"type": "task", "task": rec.id}, t(self.lang, "act_task").format(machine=node.name if node else v.get("node"), type=v.get("type"), state=st.replace("_", " ")), f"task:{rec.id}:{st}")
            self._feed.append(i)

    def activity(self, ch: Channel, since: int = 0) -> dict:
        """What the machines are doing and have done since `since`: the lines of every run that is going or just ended, and of the tasks nobody's run owns, as THIS channel may see them."""
        now = self.clock()
        for run in list(self.coord.runs.runs.values()):
            if run.store is not None and (run.finished is None or run.id not in self._ended or now - run.finished < 600 or self._buf.get(run.id)):  # a run that ended still owes its last lines
                self.sync(run)
        self._task_lines()
        new = [i for i in self._feed if i.seq > since]
        busy = bool(self.activity_of(self.coord.snapshot()))
        return {"intents": [self.for_channel(i, ch) for i in new], "seq": max([since] + [i.seq for i in new]), "busy": busy}

    def activity_tail(self, n: int = 30) -> list[dict]:
        """The last lines for the owner's panel (it is the owner's own screen: nothing is withheld from it)."""
        return [{"actor": i.actor, "text": i.text_fallback, "ts": i.ts, "run": i.run_id} for i in self._feed[-n:]]

    def _event_text(self, run, e) -> tuple[str, str]:
        """The words of one event of the run and who acted. A setup run says the machine, the step, what is happening and who did it; the rest keeps the run's own description."""
        if run.kind in SETUP_KINDS and e.type == "setup_plan":
            pl = e.payload
            if pl.get("undo"):
                return t(self.lang, "sp_undo_plan").format(machine=run.node_name, steps=pl.get("steps")), "GUARD"
            if pl.get("moved_bytes"):
                return t(self.lang, "sp_plan_moved").format(machine=run.node_name, steps=pl.get("steps"), size=human_bytes(pl.get("moved_bytes"))), "GUARD"
            return t(self.lang, "sp_plan").format(machine=run.node_name, steps=pl.get("steps"), size=human_bytes(pl.get("download_bytes"))), "GUARD"
        if run.kind in SETUP_KINDS and e.type == "setup_step":
            pl = (run.adapter.plan_view() if run.adapter is not None else None) or {"steps": []}
            n, st = e.payload.get("step"), str(e.payload.get("state"))
            step = next((x for x in pl["steps"] if x["n"] == n), {})
            extra = (f" · {human_bytes(step.get('bytes'))}" if step.get("bytes") and st in ("sent", "running") else "") + (f" ({e.payload['reason']})" if e.payload.get("reason") else "")
            actor = e.effective_tag  # as the run tallies it: the coordinator's code asked, recorded and ran it (GUARD); the owner's own events ("owner_answer", "owner_confirmation") carry OWNER
            text = t(self.lang, "sp_line").format(machine=run.node_name, verb=t(self.lang, "sp_undo" if e.payload.get("action") == "undo" else "sp_run"), n=n, of=len(pl["steps"]) or "?",
                                                  title=fmt_value(step.get("title") or "", 100), state=t(self.lang, "st_" + st) if ("st_" + st) in STR[self.lang] else st, extra=extra)
            return text, actor
        return describe_event(e), e.effective_tag

    def _install_plan(self, run, have: set, sens: str) -> None:
        """The whole plan, as words and as data, before anything that asks for an approval (it is told ahead of the first "waiting for you")."""
        view = self.coord.runs.view(run.id, events=False) or {}
        pl = view.get("plan")
        if pl and f"{run.id}:plan:{pl['hash'][-12:]}" not in have:
            d = self.plan_data(run, view)
            self._add(run.id, "show_data", "GUARD", sens, {"data_kind": "plan", "data": d}, cards.fallback_text("plan", d, self.lang), f"{run.id}:plan:{pl['hash'][-12:]}")

    def _plan_question(self, run, p, d: dict, key: str, sens: str) -> None:
        """ONE question for the whole plan (approval-policy.json: `chat_choice`): every step with its exact command, what it writes, its size and its undo, bound to the plan's hash. The owner answers it in the
        chat (the model relays his words); it is recorded as `chat_choice`, never as a system dialog."""
        undoing = d["action"] == "undo"
        total = sum(x["bytes"] or 0 for x in d["steps"] if x.get("size_kind") != "move")
        head = t(self.lang, "ap_plan_head").format(verb=t(self.lang, "ap_undo" if undoing else "ap_run"), n=len(d["steps"]), machine=run.node_name, title=(" — " + fmt_value(d["title"], 120)) if d.get("title") else "")
        rows = []
        for s in d["steps"]:
            targets = ", ".join(for_display(str(w)) for w in s.get("writes") or []) or t(self.lang, "pl_nothing")
            rows.append(f"{s['n']}. {fmt_value(s['title'], 120)}\n   {t(self.lang, 'ud_does' if undoing else 'pl_cmd')}: {for_display(s['command'])}\n   {t(self.lang, 'ud_removes' if undoing else 'pl_writes')}: {targets} · {t(self.lang, 'ap_size')}: "
                        f"{cards.size_text(s.get('bytes'), s.get('size_kind'), self.lang)}" + ("" if undoing else f"\n   {t(self.lang, 'pl_undo')}: {for_display(s['undo'])}"))
        text = head + "\n" + "\n".join(rows) + f"\n{t(self.lang, 'ap_total')}: {human_bytes(total)}\n{t(self.lang, 'pl_hash')}: {d['plan_hash']}\n" + t(self.lang, "ap_plan_ask")
        payload = {"writes": True, "chat_choice": True, "approval_kind": run.kind, "summary": head, "plan_hash": d["plan_hash"], "command": f"{len(d['steps'])} step(s), listed above", "undo": t(self.lang, "ap_plan_undo"),
                   "action": d["action"], "targets": [for_display(str(w)) for s in d["steps"] for w in s.get("writes") or []], "bytes": total, "options": approval.options(), "question": p.qid}
        i = self._add(run.id, "confirm", "GUARD", sens, payload, text, key)
        i.extra.update(state="pending", answer=(run.id, p.qid))

    def _sync_install(self, run, log: list, have: set, sens: str) -> None:
        p = run.adapter.pending if run.adapter is not None else None
        d = p.data if p is not None else {}
        if p is not None and d.get("kind") == "approve_step":
            key = f"{run.id}:approve:{p.qid}:{int(p.asked_at * 1000)}"
            if key not in have:
                verb = t(self.lang, "ap_run" if d["action"] == "run" else "ap_undo")
                summary = t(self.lang, "ap_step").format(verb=verb, n=d["step"], of=d["of"], machine=run.node_name, title=fmt_value(d["title"], 120))
                command = for_display(d["command"] if d["action"] == "run" else d["undo"])
                shown_undo = for_display(d["undo"])
                targets = ", ".join(for_display(str(w)) for w in d.get("writes") or []) or t(self.lang, "pl_nothing")
                undoing = d["action"] == "undo"  # an undo says what is undone once: its "command" is the undo
                text = (f"{summary}\n{t(self.lang, 'ud_does') if undoing else t(self.lang, 'pl_cmd')}: {command}\n{t(self.lang, 'ud_removes' if undoing else 'pl_writes')}: {targets} · {t(self.lang, 'ap_size')}: {cards.size_text(d.get('bytes'), d.get('size_kind'), self.lang)}\n"
                        + ("" if undoing else f"{t(self.lang, 'pl_undo')}: {shown_undo}\n") + f"{t(self.lang, 'pl_hash')}: {d['plan_hash']}\n")
                text += "".join(x + "\n" for x in cards.legend_lines(text, self.lang)) + t(self.lang, "ap_ask_tool")
                payload = {"writes": True, "summary": summary, "plan_hash": d["plan_hash"], "command": command, "undo": shown_undo, "step": d["step"], "of": d["of"], "action": d["action"],
                           "targets": [for_display(str(w)) for w in d.get("writes") or []], "bytes": d.get("bytes"), "size_kind": d.get("size_kind")}
                i = self._add(run.id, "confirm", "GUARD", sens, payload, text, key)
                i.extra.update(state="pending", answer=(run.id, p.qid))
        if p is not None and d.get("kind") == "approve_plan":
            key = f"{run.id}:approve:{p.qid}:{int(p.asked_at * 1000)}"
            if key not in have:
                self._plan_question(run, p, d, key, sens)
        for i in log:
            if i.kind == "confirm" and i.extra.get("answer") and i.extra.get("state") == "pending" and not (p is not None and p.qid == i.extra["answer"][1] and f":{int(p.asked_at * 1000)}" in i.id):
                i.extra["state"] = "answered"  # answered on the panel, timed out, or replaced by the next step's question
        mc = self._facts(run).get("setup.minimal_call")
        if isinstance(mc, dict) and f"{run.id}:minimal" not in have:
            self._add(run.id, "progress", "GUARD", sens, {"type": "minimal_call"}, t(self.lang, "mc_line").format(machine=run.node_name, ok=t(self.lang, "mc_ok" if mc.get("ok") else "mc_bad"), latency=mc.get("latency_ms"),
                                                                                                                 rate=t(self.lang, "mc_rate").format(tps=mc["tokens_per_s"]) if mc.get("tokens_per_s") else "", answer=fmt_value(mc.get("answer"), 80)), f"{run.id}:minimal")
        if run.finished and run.id not in self._done:
            self._done.add(run.id)
            if run.store is None:  # it never started (not paired, no analysis, no pick, nothing to undo...): say why, in words
                self._add(run.id, "text", "GUARD", sens, {}, t(self.lang, "prep_refused").format(reason=fmt_value(run.reason, 200).rstrip(".")) + "." + (" " + t(self.lang, "no_pick_hint") if run.status == "no_pick" else " " + t(self.lang, "ud_none") if run.status == "nothing_to_undo" else ""), f"{run.id}:refused")
            else:
                view = self.coord.runs.view(run.id) or {}
                d = {"run": {"id": run.id, "kind": run.kind, "node": run.node_name, "status": run.status, "reason": run.reason}, "events": [{"tag": x["tag"], "text": x["text"]} for x in view.get("events", [])], "actors": view.get("actors", {})}
                self._add(run.id, "show_data", "GUARD", sens, {"data_kind": "run", "data": d}, cards.fallback_text("run", d, self.lang), f"{run.id}:end")
                svc = getattr(self.coord, "services", None)
                if run.kind == "install" and run.status == "done" and svc is not None:  # a setup finished: the harness itself proposes what it enables, from the catalog (one question)
                    self._add(run.id, "text", "GUARD", sens, {}, svc.proposal_text(), f"{run.id}:proposal")

    def for_channel(self, i: Intent, ch: Channel) -> dict:
        """The intent as THIS channel may see it. A cloud channel never receives `confidential`; internal data passes a filter, a size cap, and is logged once."""
        d = i.to_dict()
        d["state"] = i.extra.get("state", "")
        if ch.name == "cli" and i.kind == "confirm" and d["text_fallback"].endswith(t(self.lang, "ap_ask_tool")):  # the owner's terminal asks for the typed yes itself: the line that tells a chat to call a tool is not for it
            d["text_fallback"] = d["text_fallback"][:-len(t(self.lang, "ap_ask_tool"))] + t(self.lang, "ap_ask_cli")
        if ch.name != "cli" and i.kind == "confirm" and d["state"] == "pending" and f"intent_id for mas_request_approval: {i.id}" not in d["text_fallback"]:
            d["text_fallback"] = d["text_fallback"] + "\n" + t(self.lang, "ap_intent_id").format(id=i.id)  # a chat is never asked to pass an id it was not shown
        if not ch.cloud:
            return d
        if i.sensitivity == "confidential":
            msg = t(self.lang, "withheld")
            return {**d, "kind": "text", "payload": {"withheld": True}, "text_fallback": msg, "sensitivity": "confidential"}
        d["payload"] = _drop(d["payload"])
        if len(json.dumps(d["payload"], default=str)) > MAX_CLOUD_BYTES:
            d["payload"] = {"truncated": True, **({"data_kind": i.payload.get("data_kind")} if i.payload.get("data_kind") else {})}
            d["text_fallback"] = d["text_fallback"][:1500]
        if (i.id, ch.name) not in self._egress_logged:
            self._egress_logged.add((i.id, ch.name))
            self.coord.log.add("cloud_egress", {"intent": i.id, "channel": ch.name, "kind": i.kind, "sensitivity": i.sensitivity, "bytes": len(json.dumps(d["payload"], default=str))})
        return d

    def intents(self, ch: Channel, run_id: str, since: int = 0) -> dict:
        run = self.coord.runs.runs.get(run_id)
        if run is None:
            raise Refused(404, "not_found")
        log = self.sync(run)
        waiting = [i.id for i in log if i.kind in ("ask_choice", "confirm") and i.extra.get("state") == "pending"]
        questions = [{"intent_id": i.id, "question": i.payload.get("question"), "options": [{"n": n, "id": o["id"], "label": o["label"]} for n, o in enumerate(i.payload["options"], 1)]} for i in log
                     if i.kind == "ask_choice" and i.id in waiting]
        return {"run": {"id": run.id, "status": run.status, "kind": run.kind, "finished": run.finished is not None and not waiting, "ended": run.finished is not None}, "intents": [self.for_channel(i, ch) for i in log if i.seq > since],
                "waiting": waiting, "questions": questions}  # `finished` means ended AND nothing waits for the owner: a run stays open at the pick until he answers

    # ---- the network map --------------------------------------------------------------------------------------------------------------------

    def kinds(self) -> dict:
        try:
            doc = json.loads(self.kinds_file.read_text())
            return doc if isinstance(doc, dict) else {}
        except (OSError, ValueError):
            return {}

    def set_kind(self, node_id: str, kind: str) -> None:
        """The OWNER says what kind of computer a machine is. Nothing in the probes tells a laptop from a desktop, so the map never guesses."""
        if kind not in KINDS or self.coord.registry.get(node_id) is None:
            raise Refused(400, "bad_kind")
        doc = {**self.kinds(), node_id: kind}
        self.kinds_file.write_text(json.dumps(doc))
        self.coord.log.add("node_kind_set", {"node": node_id, "kind": kind}, owner=True)

    GROUPS = (("system", ("machine.os.", "machine.os_family")), ("hardware", ("machine.ram", "machine.vram", "machine.disk", "machine.gpu", "machine.model_path", "machine.largest", "machine.usable", "machine.memory")),
              ("ai", ("machine.runtime", "machine.ai_cli", "machine.api_key", "machine.programs")))

    def machine_from_stored(self, name: str, doc: dict) -> dict:
        """The machine card of a machine whose analysis is stored: the same card the analysis run ends with, available from the map without starting anything."""
        facts, seen = doc.get("facts") or {}, set()
        groups = {}
        for g, prefixes in self.GROUPS:
            groups[g] = [{"key": k, "label": label_for(self.lang, k), "value": humanize(self.lang, k, r["value"]), "status": r["status"]} for k, r in facts.items() if k.startswith(prefixes) and k not in seen and not seen.add(k)]
        return {"name": name, "os": (facts.get("machine.os.name") or {}).get("value", ""), "label": t(self.lang, "agent_analysis"), "groups": groups,
                "findings": [{"text": r["value"].get("text", ""), "status": r["status"]} for k, r in facts.items() if k.startswith("finding.") and isinstance(r["value"], dict)]}

    def activity_of(self, snap: dict) -> dict:
        """What each machine is doing right now and who acted: the newest step of a running run on it, or a task of its own in flight."""
        out: dict[str, dict] = {}
        for r in snap["runs"]:
            run = self.coord.runs.runs.get(r["id"])
            if r["status"] in ("starting", "running") and r["node_id"] and run is not None and run.store is not None:
                rows = [e for e in run.store.events() if e.type in SHOWN]
                if rows:
                    out[r["node_id"]] = {"text": describe_event(rows[-1]), "actor": rows[-1].effective_tag if rows[-1].effective_tag in ACTORS else "GUARD", "run": r["id"], "kind": r["kind"]}
        for x in snap["tasks"]:
            if x.get("status") in ("sent", "accepted", "running") and x["node"] not in out:
                out[x["node"]] = {"text": f"{t(self.lang, 'task')} {x['type']}", "actor": x.get("by") if x.get("by") in ACTORS else "GUARD", "run": None, "kind": "task"}
        return out

    def network_data(self, ch: Channel | None = None) -> dict:
        snap = self.coord.snapshot()
        activity = self.activity_of(snap)
        kinds = self.kinds()
        nodes = []
        for m in snap["machines"]:
            st = "offline" if m["status"] != "online" else "busy" if m["id"] in activity else "online"
            doc = self.coord.runs.stored_analysis(m["id"])
            facts = []
            if doc:
                for k, row in list(doc["facts"].items()):
                    if k in ("machine.os.name", "machine.ram_total_gib", "machine.ram_available_gib", "machine.usable_memory_gib", "machine.model_path"):
                        facts.append({"label": label_for(self.lang, k), "value": row["value"], "status": row["status"]})
            seen = round(snap["now"] - m["last_heartbeat"]) if isinstance(m.get("last_heartbeat"), (int, float)) and m["last_heartbeat"] > 0 else None
            nodes.append({"id": m["id"], "name": m["name"], "os": m["os"], "arch": m.get("arch") or "", "kind": kinds.get(m["id"], "computer"), "status": st, "route": "local", "confidential": False, "active": m["id"] in activity,
                          "activity": activity.get(m["id"]), "facts": facts[:6], "machine": self.machine_from_stored(m["name"], doc) if doc else None, "seen_s": seen,
                          "analysis_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(doc["stored_at"])) if doc and isinstance(doc.get("stored_at"), (int, float)) else None})
        return {"coordinator": {"name": self.coord.display_name}, "nodes": nodes, "clouds": [], "live": bool(activity)}

    def network_intent(self, ch: Channel) -> dict:
        d = self.network_data(ch)
        i = Intent(id=f"network:{int(self.clock())}", kind="show_data", run_id=None, actor="GUARD", sensitivity="internal", payload={"data_kind": "network", "data": d}, text_fallback=cards.fallback_text("network", d, self.lang), seq=0, ts=self.clock())
        self._logs["net"] = [*self._logs.get("net", [])[-7:], i]  # the latest few maps: a chat renders its card by id right after asking for it
        return self.for_channel(validate(i), ch)

    # ---- starting runs -------------------------------------------------------------------------------------------------------------------------

    def _resolve_node(self, ref: str | None) -> str | None:
        """A chat says "the Lenovo", the registry says an id: accept either, and only when the name is unambiguous."""
        if not ref or self.coord.registry.get(ref) is not None:
            return ref
        hits = [n.node_id for n in self.coord.registry.all() if n.name.lower() == ref.lower()]
        if len(hits) != 1:
            raise Refused(404 if not hits else 409, "unknown_node" if not hits else "ambiguous_node")
        return hits[0]

    def start(self, ch: Channel, goal: str, node: str | None, sensitivity: str = "internal", by: str | None = None) -> dict:
        if sensitivity not in RANK:
            raise Refused(400, "bad_sensitivity")
        if ch.cloud and sensitivity == "confidential":
            self.coord.log.add("harness_refused", {"channel": ch.name, "reason": "cloud_cannot_create_confidential"})
            raise Refused(403, "cloud_cannot_create_confidential", "a cloud chat cannot create a confidential task: use the owner panel or a local chat")
        via = "claude-code" if by == "claude-code" else "web" if ch.name == "panel" else f"chat:{ch.name}"  # the owner's request with its channel; the operator's start in a verification says so and is never the owner's
        if goal in ("connect_host", "disconnect_host"):  # a chat host on THIS computer: no paired node is involved, `node` carries the host's name (or nothing: the owner is asked)
            host = (node or "").strip()[:40] or None
            if goal == "disconnect_host" and not host:
                raise Refused(400, "bad_goal", "name the chat to disconnect")
            view = self.coord.start_connect_run(host, via) if goal == "connect_host" else self.coord.start_disconnect_run(host, via)
            self._run_sens[view["id"]] = sensitivity
            return view
        node = self._resolve_node(node)
        if goal == "analyze" and node:
            view = self.coord.start_analysis(node, via)
        elif goal == "options" and node:
            view = self.coord.start_options_run(node, via)
        elif goal == "prepare" and node:
            view = self.coord.start_install_run(node, via)
        elif goal == "undo" and node:
            view = self.coord.start_uninstall_run(node, via)
        else:
            raise Refused(400, "bad_goal")
        self._run_sens[view["id"]] = sensitivity
        return view

    # ---- pairing and unpairing from a chat ------------------------------------------------------------------------------------------------------
    # Two thin operations over what the coordinator already does for the terminal and the owner screen. The chat can ask for a code and read what is happening; it can never complete a pairing or an
    # unpair: both end in a confirmation the owner answers with a gesture of their own (the system dialog, the host's own dialog, the panel), and only that answer runs the action.

    def _text(self, key: str, text: str, sens: str = "internal") -> Intent:
        return self._add(None, "text", "GUARD", sens, {"topic": "pairing"}, text, f"{key}:{self._seq + 1}")

    def _waiting_named(self, ref: str) -> str:
        """A chat says "the Lenovo", the waiting list says an id: accept either, and only when it is one computer."""
        hits = [w for w in self.coord.waiting.list() if w.id == ref or w.name.lower() == ref.lower()]
        if len(hits) != 1:
            raise Refused(404 if not hits else 409, "not_waiting" if not hits else "ambiguous_machine", t(self.lang, "pr_not_waiting" if not hits else "pr_ambiguous").format(ref=ref[:60]))
        return hits[0].id

    def pairing(self, ch: Channel, machine: str | None = None) -> dict:
        """With a machine: make a pairing code for that waiting computer and say it. Without one: say what is happening (who waits, the code in use, the computer asking for approval, the last outcome),
        and when a computer has typed the code, raise the confirmation the owner answers. The code is shown to the owner; whoever types it must be at that computer."""
        from envrec_coordinator.pairing import PairingBusy
        from envrec_coordinator.server import WaitingGone
        coord = self.coord
        coord.desk.expire_due()
        view = coord.owner.view()
        live = view is not None and view["status"] in ("waiting", "exchange", "confirm", "deciding")
        if machine and live:
            raise Refused(409, "pairing_busy", t(self.lang, "pr_busy"))
        if machine:
            wid = self._waiting_named(machine)
            try:
                coord.offer_for_waiting(wid, via=f"chat:{ch.name}")
            except WaitingGone:
                raise Refused(404, "not_waiting", t(self.lang, "pr_not_waiting").format(ref=machine[:60])) from None
            except PairingBusy:
                raise Refused(409, "pairing_busy", t(self.lang, "pr_busy")) from None
            view = coord.owner.view()
            if view is not None:
                self._pair_chat[view["offer"]] = ch
        intents: list[Intent] = []
        st = view["status"] if view else "idle"
        name = (view or {}).get("target") or ""
        if st != "waiting":
            self._close_code_notices()  # the code is used up or gone: its dialog goes too
        if st == "waiting":
            shown, mins = self._show_code(view, name)
            intents.append(self._text("pair_code", t(self.lang, "pr_code" if shown else "pr_code_unavailable").format(name=name or "the computer", mins=mins)))
        elif st == "exchange":
            intents.append(self._text("pair_exchange", t(self.lang, "pr_exchange").format(name=name or "the computer")))
        elif st in ("confirm", "deciding"):
            conf = self._pair_confirmation(view)
            if self._dialog_open_for(conf.id):  # the harness already raised it: nothing for the chat to call (calling would only find the dialog busy)
                cand = view.get("candidate") or {}
                intents.append(self._text("pair_dialog", t(self.lang, "pr_dialog_open").format(name=cand.get("name") or "the computer", fp=(cand.get("fingerprint") or "")[:16])))
            else:
                intents.append(conf)
        else:
            done = t(self.lang, "pr_" + st).format(name=name or "the computer", reason=view.get("reason") or "") + "\n" if view else ""  # the last outcome, then who waits now
            ws = coord.waiting.list()
            lines = "\n".join(f"{n}. {w.name} ({w.os}, {w.arch})" for n, w in enumerate(ws, 1))
            intents.append(self._text("pair_list", done + (t(self.lang, "pr_list").format(lines=lines) if ws else t(self.lang, "pr_none"))))
        return {"state": st, "intents": [self.for_channel(i, ch) for i in intents]}

    def _show_code(self, view: dict, name: str) -> tuple[bool, int]:
        """Show the code to the owner in a dialog of this computer, once per offer (again only if they closed it). The code is NOT returned to the caller: what a chat's model reads says only that it is on
        the screen. Returns (shown, minutes it stays valid)."""
        left = max(10.0, (view.get("expires_at") or self.clock() + 120) - self.clock())
        mins = max(1, round(left / 60))
        cur = self._code_notices.get(view["offer"])
        if cur is not None and cur.alive():
            return True, mins
        notice = self.native_notice(t(self.lang, "pr_dialog_title"), t(self.lang, "pr_dialog_body").format(name=name or "the computer", code=view["code"], mins=mins), timeout_s=left)
        if notice is None:
            return False, mins
        self._code_notices[view["offer"]] = notice
        return True, mins

    def _close_code_notices(self) -> None:
        for key in list(self._code_notices):
            n = self._code_notices.pop(key, None)
            if n is not None:
                n.close()

    def _pair_confirmation(self, view: dict) -> Intent:
        cand = view.get("candidate") or {}
        name, fp = cand.get("name") or "a computer", (cand.get("fingerprint") or "")[:16]
        ph = "sha256:" + hashlib.sha256(json.dumps({"pair": view["offer"], **cand}, sort_keys=True).encode()).hexdigest()
        kw = {"name": name, "os": cand.get("os", ""), "arch": cand.get("arch", ""), "fp": fp}
        p = self.request_confirmation(None, summary=t(self.lang, "pr_confirm_summary").format(**kw), plan_hash=ph, command=t(self.lang, "pr_confirm_command").format(**kw),
                                      undo=t(self.lang, "pr_confirm_undo").format(**kw))
        offer = view["offer"]
        p.intent.extra["on_decision"] = lambda approved, via: self.coord.decide_pairing(offer, approved, via)  # runs only from `_resolve`, after a confirmation with a gesture
        if not p.intent.text_fallback.endswith(t(self.lang, "ap_ask_tool")):
            p.intent.text_fallback = p.intent.text_fallback + "\n" + t(self.lang, "ap_ask_tool")
        return p.intent

    def _dialog_open_for(self, intent_id: str) -> bool:
        return any(tk.intent_id == intent_id and tk.state == "waiting" and tk.mechanism == OS_DIALOG for tk in self._tickets.values())

    def on_pairing_candidate(self, summary: dict) -> None:
        """A node typed the right code and the owner is now asked about it (called by the owner desk, on the loop). When the offer was made through a chat, the HARNESS raises the owner's confirmation in the
        operating system's own dialog, showing the computer's name and key fingerprint, exactly as for a setup step: the chat's model does nothing in this step and needs no id. Only the owner's answer in
        that dialog pairs the computer (`on_decision`). If no such dialog can be raised, the confirmation stays pending for the other ways to answer it (`mas_pair` returns it with its id)."""
        offer = str(summary.get("offer", ""))
        ch = self._pair_chat.pop(offer, None)
        view = self.coord.owner.view()
        if ch is None or view is None or view.get("offer") != offer:
            return
        intent = self._pair_confirmation(view)
        if OS_DIALOG not in self.mechanisms(ch):
            self.coord.log.add("pairing_dialog_not_raised", {"offer": offer, "channel": ch.name, "reason": "os_dialog_unavailable", "tag": "GUARD"})
            return
        try:
            self.begin_confirmation(ch, intent.id, OS_DIALOG)
        except Refused as exc:
            self.coord.log.add("pairing_dialog_not_raised", {"offer": offer, "channel": ch.name, "reason": exc.reason, "tag": "GUARD"})
            return
        self.coord.log.add("pairing_dialog_raised", {"offer": offer, "channel": ch.name, "intent": intent.id, "tag": "GUARD"})

    def unpair(self, ch: Channel, machine: str | None) -> dict:
        """Ask the owner to unpair a computer. Nothing is removed here: the confirmation is the owner's, and only its approval removes the key."""
        node = self._resolve_node(machine) if machine else None
        rec = self.coord.registry.get(node) if node else None
        if rec is None:
            raise Refused(404, "unknown_node", t(self.lang, "pr_unknown").format(ref=(machine or "")[:60]))
        kw = {"name": rec.name}
        ph = "sha256:" + hashlib.sha256(json.dumps({"unpair": rec.node_id, "name": rec.name}, sort_keys=True).encode()).hexdigest()
        p = self.request_confirmation(None, summary=t(self.lang, "un_summary").format(**kw), plan_hash=ph, command=t(self.lang, "un_command").format(**kw), undo=t(self.lang, "un_undo").format(**kw))
        via_ = lambda approved, via: approved and asyncio.ensure_future(self.coord.unpair_node(rec.node_id, via=via))  # noqa: E731
        p.intent.extra["on_decision"] = via_
        p.intent.text_fallback = p.intent.text_fallback + "\n" + t(self.lang, "ap_ask_tool")
        return {"intents": [self.for_channel(p.intent, ch)]}

    # ---- owner decisions ------------------------------------------------------------------------------------------------------------------------

    def ask_chat(self, run_id: str | None, *, key: str, text: str, options: list[dict], on_choice, sensitivity: str = "internal", multi: bool = False) -> Intent:
        """A closed question the owner answers in the chat (the model relays his words; `mas_answer`). It writes nothing: `on_choice(option_id)` runs only from `decide`, once. The options are the caller's data,
        never a model's. Asking the same key again while it is pending is the same question."""
        for x in self._logs.get(run_id or "-", []):
            if x.extra.get("base") == key and x.extra.get("state") == "pending":
                return x
        base, key = key, f"{key}:{self._seq + 1}"  # every question has its own id: an answered one is over
        opts = [{"id": o["id"], "label": str(o.get("label", o["id"])), "detail": str(o.get("detail", ""))} for o in options]
        i = self._add(run_id, "ask_choice", "GUARD", sensitivity, {"question": key, "text": text, "options": opts, "writes": False, "chat": True}, f"{text}\n{numbered(opts)}\n{t(self.lang, 'reply_hint')}", key)
        i.extra.update(state="pending", on_choice=on_choice, base=base)
        return i

    def _find(self, intent_id: str) -> Intent | None:
        for log in self._logs.values():
            for i in log:
                if i.id == intent_id:
                    return i
        return None

    def decide(self, ch: Channel, intent_id: str, option: str, by: str | None = None) -> dict:
        """The owner's choice on a closed question that WRITES NOTHING. Anything that writes is refused here, whoever asks."""
        i = self._find(intent_id)
        if i is None or i.extra.get("state") != "pending":
            raise Refused(409, "nothing_to_decide")
        if i.kind == "confirm" and i.payload.get("chat_choice") and approval.mechanism_for(str(i.payload.get("approval_kind"))) == approval.CHAT_CHOICE and not i.payload.get("secret"):
            return self._decide_chat_choice(ch, i, option, by)
        if i.kind == "confirm" or i.payload.get("writes") or str(i.payload.get("question", "")).startswith("approve_"):
            self.coord.log.add("decision_refused", {"channel": ch.name, "intent": i.id, "reason": "confirmation_required"})
            raise Refused(409, "confirmation_required", "a write is approved by a confirmation bound to its plan hash, never by a tool call")
        if i.kind != "ask_choice":
            raise Refused(400, "bad_option")
        option = self._option_id(i, option)
        cb = i.extra.get("on_choice")
        if cb is not None:  # a question the harness itself put (the owner's profile, a proposal): its own callback takes the answer, once
            i.extra["state"] = "answered"
            self.coord.log.add("harness_decision", {"channel": ch.name, "intent": i.id, "question": i.payload.get("question"), "option": option, "relayed_by_model": ch.model_in_middle}, owner=(by != "claude-code"))
            nxt = cb(option)
            return {"ok": True, "channel": ch.name, "relayed_by_model": ch.model_in_middle, **({"next": nxt} if isinstance(nxt, str) and nxt else {})}
        run = self.coord.runs.runs.get(i.run_id or "")
        if run is None:
            raise Refused(404, "not_found")
        by_model = ch.model_in_middle
        if i.payload["question"] == "pick_option":
            ok = self.coord.pick_option(run.id, option, ch.name, by)
        else:
            ok = self.coord.answer_run(run.id, i.payload["question"], option, "claude-code" if by == "claude-code" else "owner", ch.name)
        if not ok:
            raise Refused(409, "nothing_to_decide")
        i.extra["state"] = "answered"
        self.coord.log.add("harness_decision", {"channel": ch.name, "intent": i.id, "option": option, "relayed_by_model": by_model, **({"by": "claude-code"} if by == "claude-code" else {})}, owner=(by != "claude-code"))
        return {"ok": True, "channel": ch.name, "relayed_by_model": by_model}

    def _decide_chat_choice(self, ch: Channel, i: Intent, option: str, by: str | None) -> dict:
        """The owner's answer to ONE plan question, relayed by the chat. Bound to the plan hash the question carries (a changed plan is another question), recorded with the gesture `chat_choice` and the fact
        that a model relayed it: a weaker assurance than a system dialog, said so wherever it is shown."""
        if ch.cloud and i.sensitivity == "confidential":
            raise Refused(403, "withheld_from_cloud", t(self.lang, "withheld"))
        option = self._option_id(i, option)
        tk = Ticket(secrets.token_urlsafe(12), i.id, i.payload["plan_hash"], approval.CHAT_CHOICE, ch.name, self.clock())
        self._tickets[tk.id] = tk
        self._resolve(tk, "accept" if option == "approve" else "decline", i.payload["plan_hash"], {"assurance": "chat_relay", "relayed_by_model": ch.model_in_middle})
        return {"ok": True, "channel": ch.name, "relayed_by_model": ch.model_in_middle, "state": tk.state, "mechanism": approval.CHAT_CHOICE}

    def _option_id(self, i: Intent, given: str) -> str:
        """The option an answer names: its id, its number in the question's numbered list, or its exact label. Anything else is refused WITH the valid ones, so the next call can be right."""
        opts, g = i.payload["options"], str(given).strip()
        by_id = [o["id"] for o in opts if o["id"] == g]
        g_num = g.rstrip(".)").lstrip("#")
        by_n = [opts[int(g_num) - 1]["id"]] if g_num.isdigit() and 1 <= int(g_num) <= len(opts) else []
        by_label = [o["id"] for o in opts if o["label"].strip().lower() == g.lower()]
        for found in (by_id, by_n, by_label):
            if len(found) == 1:
                return found[0]
        valid = "; ".join(f"{n}) {o['id']} — {o['label']}" for n, o in enumerate(opts, 1))
        raise Refused(400, "bad_option", t(self.lang, "bad_option").format(given=g[:40], valid=valid))

    # ---- confirmations of a write ---------------------------------------------------------------------------------------------------------------

    def request_confirmation(self, run_id: str | None, *, summary: str, plan_hash: str, command: str, undo: str, sensitivity: str = "internal", actor: str = "GUARD", raise_for: Channel | None = None) -> Pending:
        """A protocol (T3a's setup) asks the owner to approve a write. The returned future resolves True/False once a valid confirmation arrives. Nothing is shown unless the intent validates."""
        payload = {"writes": True, "summary": summary, "plan_hash": plan_hash, "command": command, "undo": undo}
        key = f"{run_id or '-'}:confirm:{plan_hash[-12:]}"
        same = self._pending.get(key)
        if same is not None and same.intent.extra.get("state") == "pending":
            return same  # asking again for the same plan is the same question
        if self._find(key) is not None:  # the same plan was asked before and answered (declined, or approved and done): a new question gets its own id
            key = f"{key}:{self._seq + 1}"
        text = f"{summary}\n{t(self.lang, 'command')}: {command}\n{t(self.lang, 'undo')}: {undo}\nplan {plan_hash}"
        i = self._add(run_id, "confirm", actor, sensitivity, payload, text, key)
        i.extra["state"] = "pending"
        p = Pending(i)
        self._pending[i.id] = p
        if raise_for is not None:
            i.extra["raise_for"] = raise_for
            self.raise_dialog(raise_for, i.id)
        return p

    def request_secret_entry(self, run_id: str | None, *, summary: str, plan_hash: str, command: str, undo: str, credential: str, sensitivity: str = "internal", actor: str = "GUARD", raise_for: Channel | None = None) -> Pending:
        """Ask the owner to type a credential into the operating system's own hidden-field window. The confirmation is a `secret` one: the only mechanism that can answer it is the OS window, the typed value
        is taken from the ticket by `take_secret` (once) and is in no intent, no event and no result."""
        payload = {"writes": True, "secret": True, "credential": credential, "summary": summary, "plan_hash": plan_hash, "command": command, "undo": undo}
        key = f"{run_id or '-'}:secret:{plan_hash[-12:]}:{self._seq + 1}"  # a new question each time: an earlier one that was declined or timed out is over
        same = None
        if same is not None and same.intent.extra.get("state") == "pending":
            return same
        i = self._add(run_id, "confirm", actor, sensitivity, payload, f"{summary}\n{t(self.lang, 'command')}: {command}\n{t(self.lang, 'undo')}: {undo}\nplan {plan_hash}", key)
        i.extra["state"] = "pending"
        p = Pending(i)
        self._pending[i.id] = p
        if raise_for is not None:
            i.extra["raise_for"] = raise_for
            self.raise_dialog(raise_for, i.id)
        return p

    def raise_dialog(self, ch: Channel, intent_id: str) -> bool:
        """The harness itself opens the owner's confirmation in the operating system's own dialog (Touch ID where the system has it) for something a chat started: no model action and no id is needed. If another
        native window is open it waits its turn (`autoraise`). When no dialog can be opened the confirmation stays pending for the other ways to answer it, and its text carries the id for them."""
        i = self._find(intent_id)
        if i is None or i.kind != "confirm" or i.extra.get("state") != "pending" or i.extra.get("raised"):
            return False
        if not self.raise_dialogs:
            return False
        if self._os_busy:
            return False  # not yet: `autoraise` comes back for it when the open window closes
        i.extra["raised"] = True
        if OS_DIALOG not in self.mechanisms(ch):
            self.coord.log.add("confirmation_dialog_not_raised", {"intent": i.id, "channel": ch.name, "reason": "os_dialog_unavailable", "tag": "GUARD"})
            return False
        try:
            self.begin_confirmation(ch, i.id, OS_DIALOG)
        except Refused as exc:
            self.coord.log.add("confirmation_dialog_not_raised", {"intent": i.id, "channel": ch.name, "reason": exc.reason, "tag": "GUARD"})
            return False
        self.coord.log.add("confirmation_dialog_raised", {"intent": i.id, "channel": ch.name, "tag": "GUARD"})
        return True

    def autoraise(self) -> None:
        """Called every second by the coordinator: raise the dialogs that had to wait for a free native window (an access change a chat started)."""
        if not self.raise_dialogs:
            return
        for p in list(self._pending.values()):
            i, ch = p.intent, p.intent.extra.get("raise_for")
            if ch is not None and i.extra.get("state") == "pending" and not i.extra.get("raised"):
                if self._os_busy:
                    return  # one native window at a time: this one waits for the next second
                self.raise_dialog(ch, i.id)

    def take_secret(self, intent_id: str) -> str | None:
        """The value typed for this intent, once. A second call, or a call for an intent nobody answered, gets None."""
        for tid, tk in self._tickets.items():
            if tk.intent_id == intent_id and tid in self._secrets:
                return self._secrets.pop(tid)
        return None

    def change_plan(self, intent_id: str, new_hash: str) -> None:
        """The plan behind a pending confirmation changed: every ticket for the old hash is now stale."""
        i = self._find(intent_id)
        if i is not None:
            i.payload = {**i.payload, "plan_hash": new_hash}

    def _ticket_for(self, ch: Channel, intent_id: str, mechanism: str) -> tuple[Intent, Ticket]:
        i = self._find(intent_id)
        if i is None or i.kind != "confirm" or i.extra.get("state") != "pending":
            now = "; ".join(f"{p.intent.id}: {str(p.intent.payload.get('summary', ''))[:80]}" for p in self._pending.values() if p.intent.extra.get("state") == "pending") or t(self.lang, "ap_none_pending")
            raise Refused(409, "nothing_to_confirm", t(self.lang, "ap_nothing_pending").format(pending=now))
        if ch.cloud and i.sensitivity == "confidential":
            raise Refused(403, "withheld_from_cloud", t(self.lang, "withheld"))
        if i.payload.get("chat_choice") and mechanism in (OS_DIALOG, "host_elicitation", "host_dialog"):
            raise Refused(409, "answer_in_chat", t(self.lang, "ap_answer_in_chat").format(id=i.id))
        if i.payload.get("secret") and mechanism != OS_DIALOG:  # a credential is typed into the OS window and nowhere else: a host form, a chat dialog or the panel would put the value where a model or a host can read it
            raise Refused(409, "secret_needs_os_dialog", t(self.lang, "secret_os_only"))
        if mechanism not in self.mechanisms(ch):
            raise Refused(409, "mechanism_unavailable", f"{mechanism} is not available for {ch.name}; available: {self.mechanisms(ch)}")
        tk = Ticket(secrets.token_urlsafe(12), i.id, i.payload["plan_hash"], mechanism, ch.name, self.clock())
        self._tickets[tk.id] = tk
        return i, tk

    def _ticket_of(self, ch: Channel, intent_id: str, mechanism: str) -> "Ticket | None":
        """The operating system's ticket already made for this question on this channel: still open, or answered (the question is no longer pending)."""
        i = self._find(intent_id)
        if i is None or i.kind != "confirm" or mechanism != OS_DIALOG:
            return None
        mine = [tk for tk in self._tickets.values() if tk.intent_id == intent_id and tk.channel == ch.name and tk.mechanism == OS_DIALOG]
        if not mine:
            return None
        last = mine[-1]
        if last.state == "waiting" or i.extra.get("state") != "pending":
            return last
        return None

    def begin_confirmation(self, ch: Channel, intent_id: str, mechanism: str) -> dict:
        """Start a confirmation. The text the owner will read comes from the intent (the harness's), never from the caller. For a host-answered mechanism the adapter shows it and later calls
        `complete_confirmation`; for the OS dialog this coordinator raises the dialog itself and the answer arrives on the ticket."""
        same = self._ticket_of(ch, intent_id, mechanism)
        if same is not None:  # the harness already opened (or the owner already answered) this one: the chat's call joins it instead of opening a second dialog or failing
            return {"ticket": same.id, "plan_hash": same.plan_hash, "mechanism": same.mechanism, "prompt": {}, "state": same.state}
        i, tk = self._ticket_for(ch, intent_id, mechanism)
        undoing = i.payload.get("action") == "undo"  # the "command" of an undo is what the undo does: it is not said twice
        extra = (f"\n{t(self.lang, 'ud_removes' if undoing else 'pl_writes')}: {', '.join(map(str, i.payload.get('targets') or [])) or t(self.lang, 'pl_nothing')} · {t(self.lang, 'ap_size')}: {cards.size_text(i.payload.get('bytes'), i.payload.get('size_kind'), self.lang)}" if "targets" in i.payload else "")
        prompt = {"title": t(self.lang, "ap_title") if "targets" in i.payload else "Multi Auto Setup",
                  "message": f"{i.payload['summary']}\n\n{t(self.lang, 'ap_undoing') if undoing else t(self.lang, 'command')}:\n{i.payload['command']}{extra}" + ("" if undoing else f"\n\n{t(self.lang, 'undo')}: {i.payload['undo']}") + f"\n\nplan {tk.plan_hash}" + "".join("\n" + x for x in cards.legend_lines(f"{i.payload['command']}{extra}", self.lang))}
        self.coord.log.add("confirmation_started", {"channel": ch.name, "intent": i.id, "mechanism": mechanism, "plan_hash": tk.plan_hash})
        if i.payload.get("secret"):
            if self._os_busy:
                tk.state, tk.reason = "rejected", "a native confirmation is already open"
                raise Refused(409, "os_dialog_busy")
            self._os_busy = True
            asyncio.ensure_future(self._os_secret(tk, prompt))
        elif mechanism == OS_DIALOG:
            if self._os_busy:
                tk.state, tk.reason = "rejected", "a native confirmation is already open"
                raise Refused(409, "os_dialog_busy")
            self._os_busy = True
            asyncio.ensure_future(self._os_confirm(tk, prompt))
        return {"ticket": tk.id, "plan_hash": tk.plan_hash, "mechanism": mechanism, "prompt": prompt, "state": tk.state}

    async def _os_secret(self, tk: Ticket, prompt: dict) -> None:
        try:  # first the owner approves THIS plan in the system dialog (Touch ID where there is one); only then the hidden window for the key opens
            ok = await asyncio.to_thread(self.native_confirm, prompt["title"], prompt["message"], timeout_s=TICKET_TTL_S - 20, reason=f"{prompt['title']}: plan {tk.plan_hash[:19]}…")
        except Exception as exc:  # noqa: BLE001
            ok = {"result": "unavailable", "reason": type(exc).__name__}
        if ok.get("result") != "approved":
            self._os_busy = False
            self._resolve(tk, {"declined": "decline"}.get(ok.get("result", ""), ok.get("result", "unavailable")), tk.plan_hash, {"os_mechanism": ok.get("mechanism")})
            return
        try:
            res = await asyncio.to_thread(self.native_secret, prompt["title"], prompt["message"], timeout_s=TICKET_TTL_S - 20)
        except Exception as exc:  # a window that cannot be raised is not an approval
            res = {"result": "unavailable", "reason": type(exc).__name__}
        finally:
            self._os_busy = False
        if res.get("result") == "entered" and res.get("value"):
            self._secrets[tk.id] = res["value"]
        action = {"entered": "accept", "declined": "decline"}.get(res.get("result", ""), res.get("result", "unavailable"))
        self._resolve(tk, action, tk.plan_hash, {"os_mechanism": res.get("mechanism"), "assurance": "typed_secret"})

    async def _os_confirm(self, tk: Ticket, prompt: dict) -> None:
        try:
            res = await asyncio.to_thread(self.native_confirm, prompt["title"], prompt["message"], timeout_s=TICKET_TTL_S - 20, reason=f"{prompt['title']}: plan {tk.plan_hash[:19]}…")
        except Exception as exc:  # a dialog that cannot be raised is not an approval
            res = {"result": "unavailable", "reason": type(exc).__name__}
        finally:
            self._os_busy = False
        self._resolve(tk, {"approved": "accept", "declined": "decline"}.get(res["result"], res["result"]), tk.plan_hash, {"os_mechanism": res.get("mechanism"), "strong": res.get("strong"), "assurance": res.get("assurance")})

    def complete_confirmation(self, ch: Channel, ticket_id: str, plan_hash: str, action: str) -> dict:
        """The host answered (an elicitation the owner accepted or declined, the chat's own confirmation dialog, the owner panel's click). Valid only for the same channel, once, in time, and for
        the intent's CURRENT plan hash."""
        tk = self._tickets.get(ticket_id)
        if tk is None or tk.channel != ch.name:
            raise Refused(404, "unknown_ticket")
        if tk.mechanism not in HOST_ANSWERED:
            raise Refused(409, "not_host_answered", "this confirmation is answered by the operating system, not by the caller")
        self._resolve(tk, action, plan_hash, {})
        return {"state": tk.state, "state_text": t(self.lang, "tk_" + tk.state), "reason": tk.reason}

    def ticket(self, ch: Channel, ticket_id: str) -> dict:
        tk = self._tickets.get(ticket_id)
        if tk is None or tk.channel != ch.name:
            raise Refused(404, "unknown_ticket")
        return {"ticket": tk.id, "state": tk.state, "state_text": t(self.lang, "tk_" + tk.state), "reason": tk.reason, "assurance": tk.assurance}

    def _resolve(self, tk: Ticket, action: str, plan_hash: str, extra: dict) -> None:
        i = self._find(tk.intent_id)
        why = ""
        if tk.state != "waiting":
            why = "ticket_used"
        elif self.clock() - tk.created > TICKET_TTL_S:
            tk.state, why = "expired", "ticket_expired"
        elif i is None or i.extra.get("state") != "pending":
            why = "nothing_to_confirm"
        elif plan_hash != tk.plan_hash or plan_hash != i.payload.get("plan_hash"):
            why = "plan_hash_mismatch"
        elif i.extra.get("answer") and not self._still_asked(i, tk.plan_hash):
            why = "stale_decision"
        if why:
            if tk.state == "waiting":
                tk.state = "rejected"
            tk.reason = why
            self.coord.log.add("confirmation_rejected", {"channel": tk.channel, "intent": tk.intent_id, "mechanism": tk.mechanism, "reason": why, "ticket_hash": tk.plan_hash, "presented_hash": plan_hash})
            raise Refused(409, why)
        if action not in ("accept", "decline"):
            tk.state, tk.reason = ("timeout" if action == "timeout" else "unavailable" if action == "unavailable" else "declined"), action
            self.coord.log.add("confirmation_ended", {"channel": tk.channel, "intent": tk.intent_id, "mechanism": tk.mechanism, "result": tk.state})
            return
        approved = action == "accept"
        tk.state = "approved" if approved else "declined"
        i.extra["state"] = "approved" if approved else "declined"
        payload = {"channel": tk.channel, "mechanism": tk.mechanism, "plan_hash": tk.plan_hash, "ticket": tk.id, "approved": approved, **extra}
        payload["assurance"] = (extra.get("assurance") or ("host_ui" if tk.mechanism in ("host_elicitation", "host_dialog", "panel_click", "cli_typed") else "weak")) if approved else None  # only an approval has a strength
        tk.assurance = payload["assurance"] or ""
        run = self.coord.runs.runs.get(i.run_id or "")
        if run is not None and run.store is not None:
            A.write_owner_event(run.store, "owner_confirmation", payload, run.loop_run_id)  # OWNER: a person answered in a UI a model does not drive
        self.coord.log.add("owner_confirmation", payload, owner=True)
        p = self._pending.pop(i.id, None)
        if p is not None and not p.future.done():
            p.future.set_result(approved)
        on_decision = i.extra.get("on_decision")
        if on_decision is not None:  # an action that waited for exactly this confirmation (a pairing, an unpair): it runs here and nowhere else
            on_decision(approved, f"{tk.channel}:{tk.mechanism}")
        if i.extra.get("answer"):  # a setup step: the run's own pending decision, answered WITH the gesture that made it (the coordinator refuses an approval without one)
            run_id, qid = i.extra["answer"]
            self.coord.answer_run(run_id, qid, "approve" if approved else "reject", "owner", tk.channel, tk.mechanism)

    def _still_asked(self, i: Intent, plan_hash: str) -> bool:
        """The run still waits for exactly this step of exactly this plan: a ticket for an earlier question or another plan cannot answer it."""
        run_id, qid = i.extra["answer"]
        run = self.coord.runs.runs.get(run_id)
        pend = run.adapter.pending if run is not None and run.adapter is not None else None
        return pend is not None and pend.qid == qid and pend.data.get("plan_hash") == plan_hash

    def panel_decide(self, intent_id: str, plan_hash: str, accept: bool) -> dict:
        """The owner's click on the panel: the last resort, and the only mechanism of the panel channel."""
        _, tk = self._ticket_for(CHANNELS["panel"], intent_id, "panel_click")
        self._resolve(tk, "accept" if accept else "decline", plan_hash, {})
        return {"state": tk.state}

    def pending_confirmations(self) -> list[dict]:
        for run in list(self.coord.runs.runs.values()):
            if run.kind in SETUP_KINDS and run.finished is None:
                self.sync(run)  # a setup step waiting for the owner shows on the panel even when no chat is polling
        found = [p.intent for p in self._pending.values()] + [i for log in self._logs.values() for i in log if i.kind == "confirm" and i.extra.get("answer")]
        return [{"intent": i.id, "run": i.run_id, "summary": i.payload["summary"], "command": i.payload["command"], "undo": i.payload["undo"], "plan_hash": i.payload["plan_hash"],
                 "action": i.payload.get("action", "run"), "targets": i.payload.get("targets", []), "bytes": i.payload.get("bytes")}
                for i in found if i.extra.get("state") == "pending"]

    # ---- cards ------------------------------------------------------------------------------------------------------------------------------------

    def tool_specs(self, ch: Channel) -> dict:
        return {"tools": self.coord.services.specs()}

    def tool(self, ch: Channel, name: str, args: dict) -> dict:
        """A tool of the services, called by a chat. The channel is the token's: whether the chat is a cloud one is its own property, never an argument."""
        return self.coord.services.call(ch, name, args)

    def view_context(self, ch: Channel, context: dict) -> dict:
        """How the host displays our view (display mode, size, theme): booleans, short strings and numbers only, kept as evidence."""
        clean = {k: v for k, v in context.items() if isinstance(k, str) and len(k) < 40 and (isinstance(v, (bool, int, float)) or (isinstance(v, str) and len(v) < 60) or
                                                                                              (isinstance(v, list) and len(v) < 8 and all(isinstance(x, str) and len(x) < 30 for x in v)))}
        self.coord.log.add("harness_view_context", {"channel": ch.name, "context": clean})
        return {"ok": True}

    def network_fragment(self) -> str:
        """The map for the owner's panel (its own home): the same card the chats get, with the owner's buttons."""
        return cards.network(self.network_data(), self.lang, True)

    def card(self, ch: Channel, intent_id: str, host: str, fragment: bool) -> dict:
        from envrec_harness import host as hosts
        i = self._find(intent_id)
        if i is None:
            raise Refused(404, "not_found")
        d = self.for_channel(i, ch)
        if d["kind"] != "show_data":
            raise Refused(409, "not_a_card")
        actions = host in ("mcp", "panel")  # an Open WebUI embed cannot call back; its choices go through the chat's own dialog
        frag = cards.render(d["payload"]["data_kind"], d["payload"]["data"], self.lang, actions)
        return {"html": frag if fragment else hosts.document(frag, host, self.lang), "host": host}
