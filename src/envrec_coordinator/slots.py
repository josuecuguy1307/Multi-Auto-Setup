"""The recommender's model slots (data: `skills/protocol-recommend/references/slots.json`). A slot is one judgment asked of a model as a pick from a CLOSED list. The first model is the harness model; a second
opinion from a cloud model is asked only when a low-confidence signal fired AND the owner's own answer on cloud use allows it AND the work is not confidential, and it goes through the node that holds the
access (the coordinator never holds a key). CODE validates the pick against the list, enforces what may leave this computer, and records every call with the model id the backend returned and the tokens.
Nothing here chooses for the model: a pick that is not on the list is refused, and a disagreement is reported, never settled by code."""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from envrec.model.base import ModelError
from envrec.model.parse import ActionParseError, extract_json_object
from envrec_net import envrun as E

SLOTS_FILE = E.SKILLS_DIR / "protocol-recommend" / "references" / "slots.json"

REFUSALS = {
    "invalid_pick": "the model did not answer with a name from the list",
    "model_error": "the model could not be reached",
    "confidential": "the work is confidential, so nothing leaves the owner's computers",
    "stance_unanswered": "the owner has not answered the question on cloud use, so no cloud model is asked",
    "stance_never": "the owner's answer on cloud use is never",
    "stance_changed": "the cloud-use setting no longer matches the owner's recorded answer",
    "budget": "the cloud calls allowed for one run are used",
    "no_route": "no paired computer that is online has the cloud access",
    "task_text_in_tags_only_slot": "a slot that may only see domain words was about to send words of the request",
    "slot_off": "this slot may not use a second opinion from the cloud",
}


class SlotRefused(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code, self.detail = code, detail or REFUSALS.get(code, code)


def load_slots(path=None) -> dict:
    return json.loads((path or SLOTS_FILE).read_text(encoding="utf-8"))


@dataclass
class Call:
    slot: str
    where: str  # local | node
    model_requested: str
    model_returned: str | None
    prompt_tokens: int
    completion_tokens: int
    latency_s: float
    text: str
    response_id: str | None = None
    node_host: str | None = None


@dataclass
class Pick:
    choice: str
    call: Call


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def leaks(task_text: str, prompt: str, n: int) -> bool:
    """True when `n` consecutive words of the request appear in the outbound prompt."""
    t, p = words(task_text), " " + " ".join(words(prompt)) + " "
    return any(" " + " ".join(t[i:i + n]) + " " in p for i in range(max(0, len(t) - n + 1)))


def render(closed: list[str], context: str) -> str:
    return f"{context}\n\nList:\n" + "\n".join(f"- {c}" for c in closed) + "\n"


def signals(picks: list[str], data: dict, *, none_value: str = "none_fits", check_failed: bool = False) -> list[str]:
    """Which of the data-listed low-confidence signals fired for these picks (independent answers to the same question) and checks."""
    on = set(data["low_confidence"])
    out = []
    if "picks_disagree" in on and len(set(picks)) > 1:
        out.append("picks_disagree")
    if "none_fits" in on and none_value in picks:
        out.append("none_fits")
    if "check_failed" in on and check_failed:
        out.append("check_failed")
    return out


class Slots:
    def __init__(self, data: dict, *, local: Callable[[str], Any], remote: Callable[[str, float], Awaitable[dict]] | None, level: str, permit: Callable[[], tuple[str | None, str]],
                 log: Callable[[str, dict], Any], task_text: str = "", clock: Callable[[], float] = time.time) -> None:
        """`local(profile_name_or_None)` gives the model adapter of the first opinion. `remote(prompt, timeout_s)` asks the cloud access through its node and returns {text, summary}, or raises SlotRefused.
        `permit()` -> (the owner's recorded answer on cloud use or None, why not)."""
        self.data, self._local, self._remote, self.level, self._permit, self._log = data, local, remote, level, permit, log
        self.task_text, self.calls, self.cloud_calls, self.clock = task_text, [], 0, clock

    def usage(self) -> dict:
        """What the slot calls of this run cost, by where they ran and the model id each backend returned: calls and the tokens it reported (zero when it reported none)."""
        out: dict[str, dict] = {}
        for c in self.calls:
            u = out.setdefault(f"{c.where}:{c.model_returned or c.model_requested or 'unreported'}", {"calls": 0, "in": 0, "out": 0})
            u["calls"] += 1
            u["in"] += c.prompt_tokens
            u["out"] += c.completion_tokens
        return out

    # ---- what may leave -------------------------------------------------------------------------------------------------------------------------

    def refusal(self, slot: str) -> str:
        """The typed reason a second opinion from the cloud may not be asked now, or '' when it may."""
        spec = self.data["slots"][slot]
        if not spec.get("escalate"):
            return "slot_off"
        if self.level == "confidential":
            return "confidential"
        answer, why = self._permit()
        if answer is None:
            return why or "stance_unanswered"
        if answer in self.data["authority"]["refused_values"]:
            return "stance_never"
        if self._remote is None:
            return "no_route"
        if self.cloud_calls >= int(self.data["escalation"]["max_calls_per_run"]):
            return "budget"
        return ""

    # ---- one pick -------------------------------------------------------------------------------------------------------------------------------

    async def ask(self, slot: str, context: str, closed: list[str], *, where: str = "local") -> Pick:
        spec = self.data["slots"][slot]
        user = render(closed, context)
        if spec["input"] == "domain_tags_only" and self.task_text and leaks(self.task_text, user, int(self.data["leak_shingle_words"])):
            self._log("slot_refused", {"slot": slot, "where": where, "reason": "task_text_in_tags_only_slot", "tag": "GUARD"})
            raise SlotRefused("task_text_in_tags_only_slot")
        error = ""
        for attempt in (1, 2):
            prompt = user if not error else f"{user}\nYour previous reply was refused: {error}\nReply again with the JSON object only."
            call = await (self._ask_local(slot, spec, prompt) if where == "local" else self._ask_node(slot, spec, prompt))
            try:
                choice = str(json.loads(extract_json_object(call.text)).get("choice", "")).strip()
            except (ActionParseError, ValueError, AttributeError):
                choice = ""
            match = next((c for c in closed if c.lower() == choice.lower()), None)
            self._record(call, prompt, match, attempt)
            if match is not None:
                return Pick(match, call)
            error = f"{choice[:40] or 'nothing'!r} is not on the list"
        raise SlotRefused("invalid_pick", "the model did not answer with a name from the list")

    async def second_opinion(self, slot: str, context: str, closed: list[str]) -> Pick:
        why = self.refusal(slot)
        if why:
            self._log("slot_refused", {"slot": slot, "where": "node", "reason": why, "tag": "GUARD"})
            raise SlotRefused(why)
        self.cloud_calls += 1
        return await self.ask(slot, context, closed, where="node")

    # ---- the two places a pick can come from ----------------------------------------------------------------------------------------------------

    async def _ask_local(self, slot: str, spec: dict, prompt: str) -> Call:
        model = self._local(spec.get("local_profile"))
        try:
            done = await model.complete(spec["instruction"], prompt)
        except ModelError as exc:
            raise SlotRefused("model_error", str(exc)[:160]) from None
        return Call(slot, "local", model.profile.model_id, done.model_returned, done.prompt_tokens, done.completion_tokens, round(done.latency_s, 3), done.text)

    async def _ask_node(self, slot: str, spec: dict, prompt: str) -> Call:
        t0 = time.monotonic()
        got = await self._remote(spec["instruction"] + "\n\n" + prompt, float(self.data["escalation"]["timeout_s"]))  # may raise SlotRefused (no route, offline, no proof)
        s = got["summary"] or {}
        usage = s.get("usage") or {}
        return Call(slot, "node", s.get("model_requested") or "", s.get("model_returned"), int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0), round(time.monotonic() - t0, 3),
                    got["text"], response_id=s.get("response_id"), node_host=s.get("node_host"))

    def _record(self, call: Call, prompt: str, match: str | None, attempt: int) -> None:
        self.calls.append(call)
        self._log("slot_call", {"slot": call.slot, "where": call.where, "attempt": attempt, "model_requested": call.model_requested, "model_returned": call.model_returned,
                                "usage": {"in": call.prompt_tokens, "out": call.completion_tokens}, "latency_s": call.latency_s, "response_id": call.response_id,
                                "node_host": call.node_host, "choice": match, "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest()[:16], "prompt_chars": len(prompt), "tag": "GUARD"})


# ---- the type decision: independent picks, a measured signal, and the owner when they do not settle -------------------------------------------------------

@dataclass
class Decision:
    picks: list[tuple[str, str]]  # (where, pick) in the order they were asked: the evidence, kept whole
    signals: list[str]
    resolved: str | None  # the pick two independent answers agree on, or None
    options: list[str]  # when resolved is None: the distinct picks the owner is asked to choose between
    escalated: str = ""  # '' | 'node' | the typed reason the cloud was not asked
    calls: int = 0


def resolve(picks: list[tuple[str, str]], rule: str) -> str | None:
    """`independent_pair`: a pick counts when the cloud model and at least one local pick gave it. Two answers of the same local model do not outvote another model."""
    if rule != "independent_pair":
        raise ValueError(f"unknown resolution rule {rule!r}")
    cloud = [v for w, v in picks if w == "node"]
    local = {v for w, v in picks if w == "local"}
    return next((v for v in cloud if v in local), None)


async def decide(slots: Slots, slot: str, names: list[str], context_for: Callable[[list[str]], str], *, first: str | None = None, none_value: str = "none_fits") -> Decision:
    """The slot procedure for a closed list: one pick (given, or asked), an independent re-pick with the list in the reverse order, the data-listed signals, and, only if one fired and the cloud may be asked,
    a second opinion. Code tallies; it never settles a disagreement itself: with no pair that agrees the options go to the owner."""
    picks: list[tuple[str, str]] = []
    failed = False
    if first is not None:
        picks.append(("local", first))
    for order in ([names] if first is None else []) + [list(reversed(names))]:
        try:
            picks.append(("local", (await slots.ask(slot, context_for(order), order)).choice))
        except SlotRefused as exc:
            if exc.code not in ("invalid_pick", "model_error"):
                raise
            failed = True  # a model that cannot answer from the list is itself the signal
    sigs = signals([v for _, v in picks], slots.data, none_value=none_value, check_failed=failed)
    if not sigs:
        return Decision(picks, [], picks[0][1], [], calls=len(slots.calls))
    why = slots.refusal(slot)
    escalated = why
    if not why:
        try:
            picks.append(("node", (await slots.second_opinion(slot, context_for(names), names)).choice))
            escalated = "node"
        except SlotRefused as exc:
            escalated = exc.code
    resolved = resolve(picks, slots.data["resolution"])
    distinct = list(dict.fromkeys(v for _, v in picks))
    if resolved is None and len(distinct) == 1:
        resolved = distinct[0]  # every answer is the same one (the cloud could not be asked, or agreed): nothing is left to choose between
    options = [] if resolved else distinct
    return Decision(picks, sigs, resolved, options, escalated, len(slots.calls))
