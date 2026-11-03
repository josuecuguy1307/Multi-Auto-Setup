"""The one formatter for text the owner reads (docs/STYLE.md, docs/HARNESS_APP_STANDARD.md).

Every intent that reaches a chat, a terminal or the panel passes through `render`. In Claude Desktop the person sees only the text of a tool result, and the host model rewrites it, so the text is built
to stand alone and to survive rewriting: a title line with a symbol and its word, short sections, numbered options, a `Next:` line, plain markdown, never ANSI. Raw ids, hashes, event dumps and JSON
belong to the log and to the `detailed` level; the other two levels show the same facts in fewer words.

Presentation only. Nothing here chooses an action, advances anything, approves anything or states a fact the intent did not carry: it reads the intent (its kind, its payload, its text) and lays it out.
Text a model writes is not touched (the `mas-voice` skill covers it). The words are the strings module's; the symbols are the style tokens' (a shape and a word, never a color)."""

from __future__ import annotations

import json
import re

from envrec_harness.strings import STR, t
from envrec_style import tokens

LEVELS = ("brief", "normal", "detailed")
DEFAULT_LEVEL = "normal"
BODY_LINES = {"brief": 8, "normal": 30}  # lines of body for a view that is data to read; a plan, an approval and a question are never cut (the owner reads the whole action, every option)
NEVER_CUT = ("confirm", "ask_choice")
HASH = re.compile(r"(?:sha256:)?[0-9a-f]{16,}")  # what `violations` looks for: a whole hash
ID_MARK = re.compile(r"\s*\[id: [^\]]*\]")
RELAY_KEYS = ("ap_ask_tool", "ap_ask_cli", "ap_ask_cli_plan", "ap_plan_ask", "ap_chat_ask", "ap_answer_id", "ap_intent_id", "ap_answer_in_chat")
RUN_SYMBOL = {"done": "done", "failed": "failed", "running": "running", "cancelled": "denied", "undone": "undone"}
DEVICE_SYMBOL = {"laptop": "laptop", "desktop": "desktop", "server": "server", "oldpc": "oldpc"}


def level_of(value) -> str:
    """A verbosity as stored or typed; anything else is the default. (The owner's setting is validated where it is written.)"""
    return value if value in LEVELS else DEFAULT_LEVEL


def sym(sym_id: str) -> str:
    """A symbol with its word (`▣ confidential`): where a mark carries meaning, the shape and the word go together."""
    return tokens.symbol_text(sym_id)


def glyph(sym_id: str) -> str:
    """The symbol's shape alone, for a title line whose own words already say what it is."""
    return tokens.symbol(sym_id)["char"]


def _relay_patterns(lang: str) -> list[re.Pattern]:
    out = []
    for k in RELAY_KEYS:
        raw = STR.get(lang, STR["en"]).get(k)
        if raw:
            out.append(re.compile("^" + re.escape(raw).replace(re.escape("{id}"), r"\S+") + "$"))
    return out


def clean(text: str, *, exact: bool = False, plan_hashes: tuple[str, ...] = ()) -> list[str]:
    """The lines of `text` as a person should read them: a hash by its first 8 characters, no `[id: ...]` marker (an answer may be a number), no line that is only JSON, no run of blank lines.
    `exact` is the text of an action the owner approves (a plan, an approval): its commands are never touched, a checksum inside a command is part of what runs; only the plan's own hash (`plan_hashes`) is shortened."""
    out: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if s[:1] in ("{", "["):
            try:
                json.loads(s)
                continue
            except ValueError:
                pass
        line = ID_MARK.sub("", line)
        line = HASH.sub(lambda m: m.group(0).removeprefix("sha256:")[:8] + "…", line) if not exact else _short_plan_hash(line, plan_hashes)  # a hash is shown by its first 8 characters, enough to match it against the plan or a dialog; the whole one is in the detailed view and the log
        if not line.strip() and (not out or not out[-1].strip()):
            continue
        out.append(line.rstrip())
    while out and not out[-1].strip():
        out.pop()
    return out


def _short_plan_hash(line: str, plan_hashes: tuple[str, ...]) -> str:
    for h in plan_hashes:
        if h:
            line = line.replace(h, h.removeprefix("sha256:")[:8] + "…")
    return line


def _split_relay(lines: list[str], lang: str) -> tuple[list[str], list[str]]:
    pats = _relay_patterns(lang)
    body, relay = [], []
    for ln in lines:
        (relay if any(p.match(ln.strip()) for p in pats) else body).append(ln)
    while body and not body[-1].strip():
        body.pop()
    return body, list(dict.fromkeys(relay))


def _cut(lines: list[str], level: str, lang: str) -> list[str]:
    cap = BODY_LINES.get(level)
    if cap is None or len(lines) <= cap:
        return lines
    return [*lines[:cap], t(lang, "fm_more").format(n=len(lines) - cap)]


def title_of(i: dict, lang: str) -> str:
    kind, p = i["kind"], i.get("payload") or {}
    data = p.get("data") or {}
    lock = f" · {sym('confidential')}" if i.get("sensitivity") == "confidential" and not p.get("withheld") else ""
    if p.get("withheld"):
        return f"{sym('confidential')} · {t(lang, 'fm_withheld')}"
    if kind == "show_data":
        dk = p.get("data_kind")
        if dk == "network":
            return f"{glyph('network')} {t(lang, 'fm_network')}{lock}"
        if dk == "machine":
            return f"{glyph('node')} {t(lang, 'fm_machine').format(name=data.get('name') or '?')}{lock}"
        if dk == "options":
            return f"{glyph('owner')} {t(lang, 'fm_options')}{lock}"
        if dk == "plan":
            return f"{glyph('undone' if data.get('undo_plan') else 'waiting')} {t(lang, 'fm_undo_plan' if data.get('undo_plan') else 'fm_plan')}{lock}"
        r = data.get("run") or {}
        status = str(r.get("status") or "")
        return f"{glyph(RUN_SYMBOL.get(status, 'warning' if status else 'running'))} {t(lang, 'fm_run').format(kind=r.get('kind') or '?', status=status or '?')}{lock}"
    if kind == "ask_choice":
        return f"{glyph('owner')} {t(lang, 'fm_ask')}{lock}"
    if kind == "confirm":
        return f"{glyph('waiting')} {t(lang, 'fm_confirm')}{lock}"
    if kind == "progress":
        return f"{glyph('running')} {t(lang, 'fm_progress')}{lock}"
    actor = {"OWNER": "owner", "MODEL": "model", "SCRIPT": "script"}.get(str(i.get("actor")), "guard")
    return f"{glyph(actor)} {t(lang, 'fm_text')}{lock}"


# ---- the data views, laid out from their payload ---------------------------------------------------------------------------------------------

def _fact(r: dict, lang: str) -> str:
    from envrec_harness.cards import fmt_value
    status = r.get("status")
    return f"{r.get('label')}: {fmt_value(r.get('value'), 80)}" + (f" ({t(lang, status)})" if status else "")


def _ago(secs: int) -> str:
    """Seconds as a person says them: 40 s, 12 min, 5 h, 3 days."""
    if secs < 90:
        return f"{secs} s"
    if secs < 5400:
        return f"{round(secs / 60)} min"
    if secs < 172800:
        return f"{round(secs / 3600)} h"
    return f"{round(secs / 86400)} days"


def _rows(m: dict, level: str, lang: str, compact: bool = False) -> list[str]:
    """A machine's facts, grouped, each with how it is known. File paths stay out (this text can reach a cloud chat)."""
    out = []
    for g, key in (("system", "fm_system"), ("hardware", "fm_hardware"), ("ai", "fm_ai")):
        if (level == "brief" or compact) and g == "system":
            continue  # (the system is already in the machine's own line)
        rows = [r for r in (m.get("groups") or {}).get(g) or [] if "path" not in str(r.get("key", ""))]
        if rows:
            out.append(f"**{t(lang, key)}**: " + "; ".join(_fact(r, lang) for r in rows[: (3 if level == "brief" or compact else 8)]))
    if level != "brief":
        from envrec_harness.cards import fmt_value
        for f in (m.get("findings") or [])[:3]:
            out.append(f"- {fmt_value(f.get('text'), 200)} ({t(lang, f.get('status', 'hypothesis'))})")
    return out


def _network(d: dict, level: str, lang: str) -> list[str]:
    from envrec_harness.cards import fmt_value
    nodes = d.get("nodes") or []
    lines = [t(lang, "fm_coordinator").format(name=fmt_value((d.get("coordinator") or {}).get("name") or t(lang, "coordinator")))]
    if not nodes:
        return [*lines, t(lang, "fm_none_yet")]
    for n in nodes:
        system = " ".join(x for x in (fmt_value(n.get("os")) if n.get("os") else "", f"({n['arch']})" if n.get("arch") else "") if x)
        seen = t(lang, "fm_seen").format(ago=_ago(n["seen_s"])) if isinstance(n.get("seen_s"), int) else t(lang, "fm_never_seen")
        lock = f" · {sym('confidential')}" if n.get("confidential") else ""
        act = n.get("activity")
        now = t(lang, "fm_now").format(what=f"{fmt_value(act.get('text'), 120)} ({act.get('actor', '')})") if act else t(lang, "fm_idle")
        head = f"{glyph(DEVICE_SYMBOL.get(str(n.get('kind')), 'node'))} **{fmt_value(n.get('name'))}** — {system or t(lang, 'unknown')} · {t(lang, n.get('status', 'offline'))}"
        if level == "brief":
            lines.append(head + (f" · {now}" if act else "") + lock)
            continue
        lines += ["", head + f" · {seen}{lock}", "  " + now + ("" if n.get("machine") else f" · {t(lang, 'fm_no_analysis')}")]
        if n.get("machine"):
            lines += ["  " + x for x in _rows(n["machine"], level, lang, compact=True)]
    return lines


def _machine(d: dict, level: str, lang: str) -> list[str]:
    from envrec_harness.cards import fmt_value
    return _rows(d, level, lang)


def _options(d: dict, level: str, lang: str) -> list[str]:
    from envrec_harness.cards import fit_line, fit_rows, fmt_value
    lines = [t(lang, "no_reco")]  # nobody recommends one: said with the options, at every level
    for o in d.get("options") or []:
        where = t(lang, "fm_where_cloud" if o.get("cloud") or o.get("kind") in ("api", "cli") else "fm_where_local")
        lines.append(f"- **{fmt_value(o.get('title'), 100)}** — {t(lang, o.get('kind', 'local'))}; {where}; {t(lang, 'fm_unmeasured')}")
        if level != "brief":
            f = o.get("fit") or {}
            lines += [f"   - {fit_line(r, f, lang)}" for r in fit_rows(o, lang)]
            if o.get("explanation"):
                lines.append(f"   - {fmt_value(o['explanation'], 200)} ({t(lang, 'expl')})")
    if level != "brief":
        lines.append(t(lang, "install_note"))
    return lines


def _run(d: dict, level: str, lang: str) -> list[str]:
    from envrec_harness.cards import ACTOR_ORDER, fmt_value
    r = d.get("run") or {}
    lines = [t(lang, "fm_machine").format(name=fmt_value(r.get("node")))]
    if r.get("reason"):
        lines.append(fmt_value(r["reason"], 200))
    lines.append(t(lang, "fm_who").format(actors=", ".join(f"{a} {int((d.get('actors') or {}).get(a, 0))}" for a in ACTOR_ORDER)))
    for e in (d.get("events") or [])[-(3 if level == "brief" else 6):]:
        tag = e.get("tag") if e.get("tag") in ACTOR_ORDER else "GUARD"
        lines.append(f"- {sym({'OWNER': 'owner', 'MODEL': 'model', 'SCRIPT': 'script'}.get(tag, 'guard'))}: {fmt_value(e.get('text'), 160)}")
    return lines


_VIEWS = {"network": _network, "machine": _machine, "options": _options, "run": _run}


def next_line(i: dict, lang: str) -> str | None:
    """What the person can do now, only when the intent itself says so: a pending question or approval, or a run that has ended. Never a suggestion the harness could not stand behind."""
    state = i.get("state") or ""
    if i["kind"] == "ask_choice" and state in ("", "pending"):
        return t(lang, "fm_next").format(what=t(lang, "fm_next_pick"))
    if i["kind"] == "confirm" and state == "pending":
        return t(lang, "fm_next").format(what=t(lang, "fm_next_approve"))
    p = i.get("payload") or {}
    if i["kind"] == "show_data" and p.get("data_kind") == "run" and str(((p.get("data") or {}).get("run") or {}).get("status")) in ("done", "failed", "cancelled", "undone"):
        return t(lang, "fm_next").format(what=t(lang, "fm_next_none"))
    return None


def render(i: dict, level: str = DEFAULT_LEVEL, lang: str = "en") -> str:
    """The intent `i` (its dict form) as the person reads it at `level`. `detailed` is the whole original text under the same title; the other levels lay out the same facts."""
    level = level_of(level)
    title = title_of(i, lang)
    raw = i.get("text_fallback") or ""
    if level == "detailed":
        return f"{title}\n\n{raw}".rstrip()
    p = i.get("payload") or {}
    kind = i["kind"]
    dk = p.get("data_kind") if kind == "show_data" else None
    if dk in _VIEWS and p.get("data") is not None and not p.get("truncated"):
        body = _VIEWS[dk](p["data"], level, lang)
        relay: list[str] = []
        tail_text = [ln for ln in raw.splitlines() if any(rx.match(ln.strip()) for rx in _relay_patterns(lang))]
        relay = list(dict.fromkeys(x.strip() for x in tail_text))
    elif kind == "ask_choice" and isinstance(p.get("options"), list):
        lines, relay = _split_relay(clean(raw), lang)
        first = next((n for n, ln in enumerate(lines) if re.match(r"^1\. ", ln)), len(lines))
        question = [ln for ln in lines[:first]]
        opts = [f"{n}. {o['label']}" + (f" ({o['detail']})" if o.get("detail") else "") for n, o in enumerate(p["options"], 1)]
        body = [*question, "", *opts] if question else opts
    else:
        exact = kind == "confirm" or dk == "plan"
        hashes = tuple(str(h) for h in (p.get("plan_hash"), (p.get("data") or {}).get("plan_hash")) if h)
        body, relay = _split_relay(clean(raw, exact=exact, plan_hashes=hashes), lang)
    if dk == "plan" and body:  # the plan names itself in its first line: that line is the title
        title = f"{title.split(' ', 1)[0]} {body[0]}"
        body = body[1:]
        while body and not body[0].strip():
            body.pop(0)
    if kind not in NEVER_CUT and dk != "plan":
        body = _cut(body, level, lang)
    nxt = next_line(i, lang)
    if kind in ("progress", "text") and len([ln for ln in body if ln.strip()]) == 1 and not nxt and not relay and i.get("sensitivity") != "confidential":  # (a confidential one keeps its mark) one line of activity: its mark and the line, not a title over a single sentence
        return next(ln for ln in body if ln.strip()).strip()
    parts = [title, "", *body]
    if nxt:
        parts += ["", nxt]
    if relay:
        parts += ["", *(t(lang, "fm_relay").format(line=ln) for ln in relay)]
    return "\n".join(parts).rstrip()


def violations(text: str, i: dict, level: str) -> list[str]:
    """What is wrong with a rendered text for its level: raw JSON or a hash outside `detailed`, or more lines than the level allows for a view that is cut. Used by the tests and by the conformance check."""
    out = []
    action = i["kind"] == "confirm" or (i.get("payload") or {}).get("data_kind") == "plan"  # the text of an action keeps its commands whole, checksums included
    if level != "detailed":
        if HASH.search(text) and not action:
            out.append("a hash outside the detailed view")
        if re.search(r"\{\s*\"", text) or re.search(r"^\s*[\[{].*[\]}]\s*$", text, re.M):
            out.append("raw JSON outside the detailed view")
        if "\x1b[" in text:
            out.append("an ANSI escape in owner-facing text")
    cap = BODY_LINES.get(level)
    p = i.get("payload") or {}
    if cap is not None and i["kind"] not in NEVER_CUT and p.get("data_kind") != "plan":
        n = len([ln for ln in text.splitlines() if ln.strip()])
        if n > cap + 8:  # the body, its cut notice, the title, a Next line and the relay rules
            out.append(f"{n} non-empty lines at level {level} (limit {cap} + 8)")
    return out


def render_tool(ok: bool, text: str, level: str = DEFAULT_LEVEL, lang: str = "en") -> str:
    """The text a service tool returns (a profile, a proposal, a task's result, a refusal), laid out like an intent: a mark for the outcome, the cleaned lines, the rules for relaying an answer under their own heading.
    `detailed` returns the text as it is. A text that already is a rendered intent (starts with a mark we draw) is not wrapped twice."""
    level = level_of(level)
    if level == "detailed" or not text.strip():
        return text
    body, relay = _split_relay(clean(text), lang)
    if level == "brief":
        body = _cut(body, level, lang)
    mark = glyph("guard") if ok else glyph("failed")
    first = body[0] if body else ""
    parts = [f"{mark} {first}" if first else mark, *body[1:]]
    has_options = any(re.match(r"^1\. ", ln) for ln in body)
    if has_options and relay:
        parts += ["", t(lang, "fm_next").format(what=t(lang, "fm_next_pick"))]
    if relay:
        parts += ["", *(t(lang, "fm_relay").format(line=ln) for ln in relay)]
    return "\n".join(parts).rstrip()
