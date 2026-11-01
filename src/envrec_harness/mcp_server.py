"""The Claude Desktop adapter: a local MCP server over stdio (docs/HARNESS_APP_STANDARD.md).

It translates. Five entry tools map one-to-one onto routes of the harness core; the cards the core renders travel as MCP App UI (`ui://mas/cards`); a write is approved only by a confirmation the
core issues and this process obtains from the host's own UI (MCP elicitation) or asks the core to obtain from the operating system. Nothing here decides what is allowed: the core refuses, this
process reports the refusal. The protocol is hand-written (stdio JSON-RPC, a few methods) so the adapter needs no dependency; its capabilities are read from the live handshake."""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path

from envrec_harness import host as hosts
from envrec_harness.strings import t
from envrec_harness.client import CoreClient, CoreError

BASE_INSTRUCTIONS = "Multi Auto Setup: the owner's private network of computers. The harness does the work; you relay what the owner asks and what the harness reports."
CHAT_SKILL = "harness-app-chat"  # what the chat's model needs to know about these tools lives in skills, not in this file; the list of those skills is this skill's data
SKILLS_ROOT = Path(__file__).resolve().parents[2] / "skills"


def _body(name: str) -> str:
    text = (SKILLS_ROOT / name / "SKILL.md").read_text(encoding="utf-8")
    return text.split("---", 2)[2].strip() if text.startswith("---") else text


def instructions() -> str:
    """The server's instructions for the chat's model: one sentence, then the body of each skill it needs. The host has no way to load a skill of ours by name (a model that tries gets "Failed"), so the
    knowledge is delivered here, with the connection."""
    try:  # the skills folder next to the package (an adapter imports nothing from the coordinator or the OS layer: conformance HA1)
        names = json.loads((SKILLS_ROOT / CHAT_SKILL / "references" / "delivered.json").read_text(encoding="utf-8"))["skills"]
    except (OSError, ValueError, KeyError):
        names = [CHAT_SKILL]
    parts = []
    for name in names:
        try:
            parts.append(_body(name))
        except (OSError, IndexError):
            continue
    return BASE_INSTRUCTIONS + ("\n\n" + "\n\n".join(parts) if parts else "")


UI_URI = "ui://mas/cards"
UI_MIME = "text/html;profile=mcp-app"
SUPPORTED = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
APP_META = {"ui": {"resourceUri": UI_URI}, "ui/resourceUri": UI_URI}
ELICIT_TIMEOUT_S = 150.0

TOOLS = {  # name: (description, schema, shows cards)
    "mas_network": ("Show the owner's private network of computers: each machine, whether it is online or busy, and what is known about it. Use it when the owner asks about his machines.",
                    {"type": "object", "properties": {}}, True),
    "mas_start": ("Start a run on one paired machine. goal 'analyze' reads the machine (read-only) and 'options' asks the owner which AI to use there. node is the machine's name or id. "
                  "The harness's own agent does the work; follow it with mas_status.",
                  {"type": "object", "properties": {"goal": {"type": "string", "enum": ["analyze", "options"]}, "node": {"type": "string"}}, "required": ["goal", "node"]}, True),
    "mas_prepare": ("Prepare one paired machine for the option the owner picked there (only after the machine was analysed and HE picked an option): the harness's own agent builds the setup plan and the whole plan comes back as text before anything is approved. "
                    "The plan is then ONE question to the owner in the chat (every step, exact command, size and undo): show it complete and relay HIS answer with mas_answer; you cannot answer for him. Follow with mas_status. node is the machine's name or id.",
                    {"type": "object", "properties": {"node": {"type": "string"}}, "required": ["node"]}, True),
    "mas_undo": ("Undo, in a new run, what a finished setup wrote on one paired machine (only what that setup's own plan wrote, newest step first). The undo plan comes back as text before anything is approved; each undo step "
                 "then waits for the owner as ONE question in the chat (relay HIS answer with mas_answer); follow with mas_status. node is the machine's name or id.",
                 {"type": "object", "properties": {"node": {"type": "string"}}, "required": ["node"]}, True),
    "mas_activity": ("What the owner's machines are doing and have done since the last call, one line each with the machine, what happened and who did it (MODEL, GUARD, SCRIPT, OWNER). Use it to follow a run or to answer "
                     "what is happening on my machines. It takes no arguments.", {"type": "object", "properties": {}}, True),
    "mas_connect": ("Connect a CHAT the owner already uses (Open WebUI, Claude Desktop, a terminal agent) to Multi Auto Setup on THIS computer. Only for connecting a chat host: never for giving a computer access to a model, an API or a key (that is mas_add_access). Without host the owner is asked which of the chats that were found; with host (one of "
                    "openwebui, claude-desktop, claude-code, codex, opencode, muse-code) that is their answer. The whole plan comes back as text before anything is approved; each step then waits for the owner like a setup step: "
                    "call mas_request_approval with the step's intent_id and follow with mas_status.", {"type": "object", "properties": {"host": {"type": "string"}}}, True),
    "mas_disconnect": ("Undo, in a new run, the connection of a chat that was made earlier (only what that connection wrote, newest step first). host is the chat's name, as for mas_connect.",
                       {"type": "object", "properties": {"host": {"type": "string"}}, "required": ["host"]}, True),
    "mas_pair": ("Pair a new computer with this network. Without machine: say who is waiting to join, the pairing code in use, or the computer that typed the code and now needs the owner's approval. With machine "
                 "(the name of a waiting computer, as listed): show its 6-digit code for the owner to type on THAT computer. You cannot complete a pairing: when a computer has typed the code, the harness itself opens a confirmation dialog on the owner's screen "
                 "(name and key fingerprint); the owner answers there and you call nothing. Ask again for the outcome. If no dialog could be opened this returns the confirmation with its intent_id for mas_request_approval. wait_s waits for the state to change.",
                 {"type": "object", "properties": {"machine": {"type": "string"}, "wait_s": {"type": "number", "minimum": 0, "maximum": 20}}}, True),
    "mas_unpair": ("Ask the owner to unpair a paired computer (machine: its name). Nothing is removed by this call: it returns a confirmation; call mas_request_approval with its intent_id and the owner answers in a "
                   "system dialog. Only that approval removes the computer's key.", {"type": "object", "properties": {"machine": {"type": "string"}}, "required": ["machine"]}, True),
    "mas_status": ("What happened in a run since the last call: progress, a question for the owner, the machine card, the options. It WAITS (up to wait_s, default 20) until something needs the owner, a result arrives or the run ends, "
                   "and returns the progress that piled up meanwhile: call it once, then act on what it says; do not call it again in a row to check progress.",
                   {"type": "object", "properties": {"run_id": {"type": "string"}, "wait_s": {"type": "number", "minimum": 0, "maximum": 20}}, "required": ["run_id"]}, True),
    "mas_answer": ("Give the owner's answer to a closed question the harness asked (his profile, a proposal, a task's level, the whole plan of a setup or an undo): intent_id from the question, and option = the option's id or its number in the question's numbered list. "
                   "Only after the OWNER has answered in the chat; never for him. The options are an unordered set that he chooses from; do not recommend or rank them. It cannot approve a pairing or a key (those are system dialogs).",
                   {"type": "object", "properties": {"intent_id": {"type": "string"}, "option": {"type": "string", "description": "an option id exactly as printed after 'id:', or its number (1, 2, 3...)"}}, "required": ["intent_id", "option"]}, True),
    "mas_request_approval": ("Ask the owner to approve a pending plan that writes to a machine. The owner answers in his own screen, not through you; you only learn the outcome.",
                             {"type": "object", "properties": {"intent_id": {"type": "string"}}, "required": ["intent_id"]}, True),
}
APP_ONLY = {  # called by the card's own view, never offered to the chat's model (MCP Apps tool visibility)
    "mas_live": ("The network map with what each machine is doing now, for the app view to refresh itself while a run is active.", {"type": "object", "properties": {}}),
    "mas_view": ("The app view reports how the host displays it (display mode, size), as evidence.", {"type": "object", "properties": {"context": {"type": "object"}}}),
}
LIVE_EVERY_S = 3


class McpAdapter:
    def __init__(self, client: CoreClient, out=None, evidence: Path | None = None, rpc_log: Path | None = None) -> None:
        self.core, self.out, self.evidence, self.rpc_log = client, out or sys.stdout, evidence, rpc_log
        self.lock = threading.Lock()
        self.waiters: dict = {}
        self.next_id = 0
        self.client_caps: dict = {}
        self.declared: dict = {}
        self.cursor: dict[str, int] = {}
        self.activity_cursor = 0
        self._specs: dict[str, dict] | None = None

    # ---- wire -----------------------------------------------------------------------------------------------------------------------------------

    def send(self, msg: dict) -> None:
        with self.lock:
            self.out.write(json.dumps(msg, separators=(",", ":")) + "\n")
            self.out.flush()

    def request(self, method: str, params: dict, timeout: float) -> dict | None:
        """A request to the host (elicitation). None when the host did not answer in time."""
        self.next_id += 1
        rid = f"mas-{self.next_id}"
        ev = threading.Event()
        self.waiters[rid] = [ev, None]
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        ev.wait(timeout)
        return self.waiters.pop(rid)[1]

    def feed(self, msg: dict) -> None:
        """One incoming message. Requests run in their own thread so a tool can wait for the host's answer while the next message is still read."""
        if "method" not in msg:
            w = self.waiters.get(msg.get("id"))
            if w:
                w[1] = msg
                w[0].set()
            return
        if "id" not in msg:
            if msg["method"] == "notifications/initialized":
                self._announce()
            return
        threading.Thread(target=self._answer, args=(msg,), daemon=True).start()

    def _log(self, row: dict) -> None:
        """What the host asked and what we answered, as names, uris and sizes (never arguments or text): the evidence of whether the host ever reads our UI resource or only lists it."""
        if self.rpc_log is None:
            return
        try:
            who = str(self.client_caps.get("client", {}).get("name", "?"))[:40]
            with self.rpc_log.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"t": round(time.time(), 3), "client": who, **row}) + "\n")
        except OSError:
            pass

    def _answer(self, msg: dict) -> None:
        p = msg.get("params") or {}
        if msg["method"] == "initialize":
            self.client_caps = {**self.client_caps, "client": p.get("clientInfo") or {}}  # so even the first row says who asked
        self._log({"dir": "in", "method": msg["method"], "id": msg["id"], **({"tool": p.get("name")} if msg["method"] == "tools/call" else {}), **({"uri": p.get("uri")} if msg["method"] == "resources/read" else {})})
        try:
            result = self.handle(msg["method"], p)
            self._log({"dir": "out", "id": msg["id"], "method": msg["method"], "ok": True, "bytes": len(json.dumps(result)), "is_error": bool(isinstance(result, dict) and result.get("isError")),
                       "cards": len((result.get("_meta") or {}).get("mas/cards", [])) if isinstance(result, dict) else 0})
            self.send({"jsonrpc": "2.0", "id": msg["id"], "result": result})
        except KeyError:
            self._log({"dir": "out", "id": msg["id"], "method": msg["method"], "ok": False, "error": "method not found"})
            self.send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": f"method not found: {msg['method']}"}})
        except Exception as exc:  # the host never sees a traceback
            self.send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32603, "message": type(exc).__name__}})

    # ---- the handshake: what this host says it can do ---------------------------------------------------------------------------------------------

    def handle(self, method: str, p: dict) -> dict:
        return {"initialize": self.initialize, "ping": lambda _: {}, "tools/list": self.tools_list, "tools/call": self.tools_call, "resources/list": self.resources_list,
                "resources/read": self.resources_read, "prompts/list": lambda _: {"prompts": []}}[method](p)

    def initialize(self, p: dict) -> dict:
        caps = p.get("capabilities") or {}
        self.client_caps = {"client": p.get("clientInfo") or {}, "protocol_requested": p.get("protocolVersion"), "capabilities": caps}
        ext = caps.get("extensions") or {}
        self.declared = {"elicitation": "elicitation" in caps, "elicitation_form": "elicitation" in caps and ("form" in (caps["elicitation"] or {}) or not caps["elicitation"]),
                         "mcp_apps": "io.modelcontextprotocol/ui" in ext, "protocol": str(p.get("protocolVersion", ""))[:20]}
        version = p.get("protocolVersion") if p.get("protocolVersion") in SUPPORTED else SUPPORTED[0]
        return {"protocolVersion": version, "capabilities": {"tools": {}, "resources": {}}, "serverInfo": {"name": "multi-auto-setup", "version": "1"},
                "instructions": instructions()}

    def _announce(self) -> None:
        """After `initialized`: tell the core what the live handshake declared, and keep that evidence on disk (names and booleans only)."""
        try:
            self.core.declare(self.declared)
            if self.evidence:
                name = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(self.client_caps.get("client", {}).get("name", "client")))[:50]
                self.evidence.with_name(f"{self.evidence.stem}-{name}.json").write_text(json.dumps({"declared": self.declared, "handshake": self.client_caps, "at": time.time()}, indent=1, default=str))
        except (CoreError, OSError):
            pass

    # ---- tools -------------------------------------------------------------------------------------------------------------------------------------

    def _core_tools(self) -> dict[str, dict]:
        """The tools the core publishes (its services, as data): listed and called exactly as it says, the adapter adds nothing."""
        if self._specs is None:
            try:
                self._specs = {t["name"]: t for t in self.core.tools()["tools"]}
            except (CoreError, OSError, KeyError, AttributeError):  # no core to ask (or it is not up): the entry tools alone
                return {}
        return self._specs

    def tools_list(self, _p: dict) -> dict:
        extra = [{"name": n, "description": t["description"], "inputSchema": t["inputSchema"], "_meta": {}, "annotations": {"readOnlyHint": bool(t.get("readOnly")), "openWorldHint": False}} for n, t in self._core_tools().items()]
        tools = [{"name": n, "description": d, "inputSchema": s, "_meta": APP_META if ui else {}, "annotations": {"readOnlyHint": n not in ("mas_request_approval", "mas_pair", "mas_unpair"), "openWorldHint": False}} for n, (d, s, ui) in TOOLS.items()]
        tools += [{"name": n, "description": d, "inputSchema": s, "_meta": {"ui": {"visibility": ["app"]}}, "annotations": {"readOnlyHint": True, "openWorldHint": False}} for n, (d, s) in APP_ONLY.items()]
        return {"tools": tools + extra}

    def resources_list(self, _p: dict) -> dict:
        return {"resources": [{"uri": UI_URI, "name": "Multi Auto Setup cards", "mimeType": UI_MIME}]}

    def resources_read(self, p: dict) -> dict:
        if p.get("uri") != UI_URI:
            raise KeyError(p.get("uri"))
        return {"contents": [{"uri": UI_URI, "mimeType": UI_MIME, "text": hosts.document("", "mcp"), "_meta": {"ui": {"prefersBorder": False}}}]}

    def tools_call(self, p: dict) -> dict:
        name, args = p.get("name"), p.get("arguments") or {}
        if name in self._core_tools():
            try:
                out = self.core.tool(self._specs[name]["tool"], args if isinstance(args, dict) else {})
            except CoreError as exc:
                return {"content": [{"type": "text", "text": t("en", "mcp_rejected").format(error=exc.error, message=exc.message).strip()}], "isError": True}
            return {"content": [{"type": "text", "text": out.get("text", "")}], "structuredContent": {k: v for k, v in out.items() if k != "text"}, "_meta": {"mas/cards": []}, **({} if out.get("ok", True) else {"isError": True})}
        if name not in TOOLS and name not in APP_ONLY:
            raise KeyError(name)
        try:
            return getattr(self, "tool_" + name[4:])(args)
        except CoreError as exc:
            return {"content": [{"type": "text", "text": t("en", "mcp_rejected").format(error=exc.error, message=exc.message).strip()}], "isError": True}

    def _result(self, intents: list[dict], extra_text: str = "", live: bool = False, questions: list | None = None, **structured) -> dict:
        cards = [{"slot": "network" if i["payload"].get("data_kind") == "network" else i["id"], "intent_id": i["id"], "html": self.core.card(i["id"], "mcp")}
                 for i in intents if i["kind"] == "show_data" and i["payload"].get("data_kind") and not i["payload"].get("truncated")]
        text = "\n\n".join(x for x in [*(i["text_fallback"] for i in intents), extra_text] if x)
        return {"content": [{"type": "text", "text": text or t("en", "mcp_nothing_new")}],
                "structuredContent": {"intents": [{"id": i["id"], "kind": i["kind"], "actor": i["actor"]} for i in intents], **({"questions": questions} if questions else {}), **structured},
                "_meta": {"mas/cards": cards, "mas/live": {"active": live, "every_s": LIVE_EVERY_S}}}  # the card HTML is for the app's view: it is not put where the chat's model reads it

    def tool_network(self, _a: dict) -> dict:
        i = self.core.network()
        return self._result([i], live=bool(i["payload"].get("data", {}).get("live")))

    def tool_live(self, _a: dict) -> dict:
        i = self.core.network()
        return self._result([i], live=bool(i["payload"].get("data", {}).get("live")))

    def tool_view(self, a: dict) -> dict:
        self.core.view(a.get("context") if isinstance(a.get("context"), dict) else {})
        return {"content": [{"type": "text", "text": "ok"}], "_meta": {"mas/cards": []}}

    def tool_start(self, a: dict) -> dict:
        run = self.core.start(str(a.get("goal", "")), str(a.get("node", "")))
        self.cursor[run["id"]] = 0
        out = self._poll(run["id"], 6.0)
        return self._result(out["intents"], f"run_id: {run['id']}", live=not out["run"]["ended"], questions=out["questions"], run_id=run["id"], status=out["run"]["status"], waiting=out["waiting"])

    def tool_activity(self, _a: dict) -> dict:
        out = self.core.activity(self.activity_cursor)
        self.activity_cursor = out["seq"]
        lines = [f"[{i['actor']}] {i['text_fallback']}" for i in out["intents"]]
        return {"content": [{"type": "text", "text": "\n".join(lines) or t("en", "act_nothing")}], "structuredContent": {"lines": len(lines), "busy": out["busy"]},
                "_meta": {"mas/cards": [], "mas/live": {"active": bool(out["busy"]), "every_s": LIVE_EVERY_S}}}

    def tool_connect(self, a: dict) -> dict:
        run = self.core.start("connect_host", str(a.get("host", "")))
        self.cursor[run["id"]] = 0
        out = self._poll(run["id"], 6.0)
        return self._result(out["intents"], f"run_id: {run['id']}", live=not out["run"]["ended"], questions=out["questions"], run_id=run["id"], status=out["run"]["status"], waiting=out["waiting"])

    def tool_disconnect(self, a: dict) -> dict:
        run = self.core.start("disconnect_host", str(a.get("host", "")))
        self.cursor[run["id"]] = 0
        out = self._poll(run["id"], 6.0)
        return self._result(out["intents"], f"run_id: {run['id']}", live=not out["run"]["ended"], questions=out["questions"], run_id=run["id"], status=out["run"]["status"], waiting=out["waiting"])

    def tool_prepare(self, a: dict) -> dict:
        run = self.core.start("prepare", str(a.get("node", "")))
        self.cursor[run["id"]] = 0
        out = self._poll(run["id"], 6.0)
        return self._result(out["intents"], f"run_id: {run['id']}", live=not out["run"]["ended"], questions=out["questions"], run_id=run["id"], status=out["run"]["status"], waiting=out["waiting"])

    def tool_undo(self, a: dict) -> dict:
        run = self.core.start("undo", str(a.get("node", "")))
        self.cursor[run["id"]] = 0
        out = self._poll(run["id"], 6.0)
        return self._result(out["intents"], f"run_id: {run['id']}", live=not out["run"]["ended"], questions=out["questions"], run_id=run["id"], status=out["run"]["status"], waiting=out["waiting"])

    def tool_pair(self, a: dict) -> dict:
        machine = str(a["machine"]).strip()[:80] if isinstance(a.get("machine"), str) and a["machine"].strip() else None
        out = self.core.pairing(machine)
        end = time.monotonic() + min(max(float(a.get("wait_s") or 0), 0.0), 20.0)
        while out["state"] in ("waiting", "exchange") and time.monotonic() < end:  # the owner is typing the code on the other computer: wait for the next state
            time.sleep(1.0)
            again = self.core.pairing(None)
            if again["state"] != out["state"]:
                out = again
        return self._result(out["intents"], state=out["state"])

    def tool_unpair(self, a: dict) -> dict:
        out = self.core.unpair(str(a.get("machine", "")).strip()[:80])
        return self._result(out["intents"])

    def tool_status(self, a: dict) -> dict:
        run_id = str(a.get("run_id", ""))
        out = self._poll(run_id, min(float(a.get("wait_s", 20) or 0), 20.0), "change")
        return self._result(out["intents"], t("en", "mcp_status").format(run_id=run_id, status=out["run"]["status"]) + (t("en", "mcp_waiting").format(ids=", ".join(out["waiting"])) if out["waiting"] else ""), live=not out["run"]["ended"],
                            questions=out["questions"], run_id=run_id, status=out["run"]["status"], waiting=out["waiting"], finished=out["run"]["finished"])

    def _poll(self, run_id: str, wait: float, until: str = "") -> dict:
        out = self.core.intents(run_id, self.cursor.get(run_id, 0), wait, until)
        if out["intents"]:
            self.cursor[run_id] = max(self.cursor.get(run_id, 0), *(i["seq"] for i in out["intents"]))
        return out

    def tool_answer(self, a: dict) -> dict:
        r = self.core.decide(str(a.get("intent_id", "")), str(a.get("option", "")))
        return {"content": [{"type": "text", "text": (t("en", "mcp_recorded") + "\n\n" + r["next"]) if r.get("next") else t("en", "mcp_answered").format(channel=r["channel"])}], "structuredContent": {**r}, "_meta": {"mas/cards": []}}

    def tool_request_approval(self, a: dict) -> dict:
        """Obtain the owner's answer from something a model does not drive, in the order the core lists them. The prompt is the core's text, never ours."""
        intent_id = str(a.get("intent_id", ""))
        for mech in self.core.capabilities()["confirmations"]:
            if mech == "host_elicitation" and not self.declared.get("elicitation_form"):
                continue  # this host did not say it can show a form: the next mechanism the core lists
            if mech == "host_elicitation":
                try:
                    b = self.core.begin_confirmation(intent_id, mech)
                except CoreError:
                    continue  # the core refused this way of asking for this question (a key is only typed into the operating system's window): the next way it lists
                res = self.request("elicitation/create", {"message": b["prompt"]["message"], "requestedSchema": {"type": "object", "properties": {"approve": {"type": "boolean", "title": "Approve exactly this plan", "description": b["plan_hash"]}}, "required": ["approve"]}}, ELICIT_TIMEOUT_S)
                r = (res or {}).get("result") or {}
                action = "accept" if r.get("action") == "accept" and (r.get("content") or {}).get("approve") is True else "decline" if r.get("action") in ("accept", "decline") else "cancel"
                if res is None:
                    return self._approval_text(t("en", "ap_host_timeout"))
                if action == "cancel":
                    return self._approval_text(t("en", "ap_dismissed"))
                done = self.core.complete_confirmation(b["ticket"], b["plan_hash"], action)
                state = done["state"]
                return self._approval_text(t("en", "ap_host_answer").format(state=done["state_text"]), state=state, mechanism=mech, plan_hash=b["plan_hash"])
            if mech == "os_dialog":
                b = self.core.begin_confirmation(intent_id, mech)
                state = self._wait_ticket(b["ticket"])
                tk = self.core.ticket(b["ticket"])
                weak = tk.get("assurance") == "weak"
                return self._approval_text(t("en", "ap_os_answer").format(state=tk["state_text"]) + (t("en", "ap_weak_note") if weak else "") + t("en", "ap_if_not"),
                                           state=state, mechanism=mech, plan_hash=b["plan_hash"], **({"assurance": "weak"} if weak else {}))
        return self._approval_text(t("en", "ap_none"), state="unavailable")

    def _wait_ticket(self, ticket: str) -> str:
        end = time.monotonic() + ELICIT_TIMEOUT_S
        while time.monotonic() < end:
            st = self.core.ticket(ticket)["state"]
            if st != "waiting":
                return st
            time.sleep(0.5)
        return "timeout"

    @staticmethod
    def _approval_text(text: str, **structured) -> dict:
        return {"content": [{"type": "text", "text": text}], "structuredContent": {**structured}, "_meta": {"mas/cards": []}}


def serve(adapter: McpAdapter, stdin=None) -> None:
    for line in stdin or sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if isinstance(msg, dict):
            adapter.feed(msg)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m envrec_harness.mcp_server", description="Multi Auto Setup: local MCP server for Claude Desktop (stdio).")
    ap.add_argument("--state-dir", required=True, help="the coordinator's state directory (the one given to `mas coordinator start`)")
    ap.add_argument("--channel", default="claude-desktop")
    ap.add_argument("--operator", action="store_true", help="a verification run driven by the assistant on the owner's behalf: the core logs starts and answers as CLAUDE-CODE, never as the owner")
    a = ap.parse_args(argv)
    hdir = Path(a.state_dir) / "coordinator" / "harness"
    serve(McpAdapter(CoreClient(hdir, a.channel, operator=a.operator), evidence=hdir / f"handshake-{a.channel}.json", rpc_log=hdir / f"rpc-{a.channel}.jsonl"))


if __name__ == "__main__":
    main()
