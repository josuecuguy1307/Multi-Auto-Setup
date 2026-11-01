"""
title: Multi Auto Setup
author: Multi Auto Setup
version: 0.1
description: The owner's private network of computers, from Open WebUI. A native Pipe: it appears as the model "Multi Auto Setup" and forwards to the harness core API; the harness's own local model drives the runs.
"""
# Install: Admin Panel > Functions > New function, paste this file, save, enable it, and set the valve STATE_DIR to the coordinator's state directory (the one given to `mas coordinator start`).
# It translates and holds no rule: what a channel may see or decide is the core's (docs/HARNESS_APP_STANDARD.md). Standard library and pydantic only (Open WebUI ships pydantic).

import asyncio
import http.client
import json
from pathlib import Path

from pydantic import BaseModel, Field

CHANNEL = "openwebui"
MAX_ROUNDS = 90

# The Pipe's own words, in one place (a Function is a single file with nothing else of ours installed, so it cannot import the Harness App's strings module). Everything the coordinator says comes from the core.
S = {
    "connecting": "Connecting to the coordinator…", "need_dialogs": "Open this model from Open WebUI's chat: it needs its dialogs to ask you and to ask for your approval.",
    "set_valve": "Set the valve STATE_DIR of this function to the coordinator's state directory.", "menu_title": "Multi Auto Setup", "menu_ask": "What do you want to do?",
    "see_network": "See my network", "analyze": "Analyze {name}", "options": "Choose the AI on {name}", "prepare": "Prepare {name} (the option you chose)", "undo": "Undo what was set up on {name}",
    "run_started": "Run {id} started", "done": "Done", "nothing_new": "Nothing new.", "no_coordinator": "I cannot find the coordinator: start it with `mas coordinator start --web` and check the valve STATE_DIR.",
    "failed": "It could not be completed", "approve_title": "Approve this step", "your_answer": "Your answer: {state}.",
}


class Core:
    """The harness core API as plain calls (a copy of what envrec_harness.client does, because a Function is a single file with nothing else of ours installed)."""

    def __init__(self, state_dir: str) -> None:
        self.dir = Path(state_dir) / "coordinator" / "harness"

    def call(self, method, path, body=None, timeout=40):
        ep = json.loads((self.dir / "endpoint.json").read_text())
        token = (self.dir / f"{CHANNEL}.token").read_text().strip()
        conn = http.client.HTTPConnection(ep["host"], ep["port"], timeout=timeout)
        headers = {"Host": f"{ep['host']}:{ep['port']}", "Authorization": f"Bearer {token}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            conn.request(method, "/harness/v1/" + path, body=json.dumps(body) if body is not None else None, headers=headers)
            r = conn.getresponse()
            raw = r.read()
        finally:
            conn.close()
        doc = json.loads(raw) if raw.startswith(b"{") else {}
        if r.status != 200:
            raise RuntimeError(f"{doc.get('error', r.status)}: {doc.get('message', '')}".strip())
        return doc


class Pipe:
    class Valves(BaseModel):
        STATE_DIR: str = Field(default="", description="The coordinator's state directory (the one given to `mas coordinator start`)")
        WAIT_S: int = Field(default=8, description="Seconds to wait for news from a run before asking again")

    def __init__(self):
        self.valves = self.Valves()

    def pipes(self):
        return [{"id": "mas", "name": "Multi Auto Setup"}]

    async def pipe(self, body: dict, __user__: dict | None = None, __event_emitter__=None, __event_call__=None) -> str:
        if not self.valves.STATE_DIR:
            return S["set_valve"]
        core = Core(self.valves.STATE_DIR)
        io = lambda *a, **k: asyncio.to_thread(core.call, *a, **k)  # noqa: E731

        async def status(text, done=False):
            if __event_emitter__:
                await __event_emitter__({"type": "status", "data": {"description": text, "done": done, "hidden": False}})

        slots: dict = {}  # slot name -> card html; the whole list is re-emitted (replace) whenever one card changes, so the map stays one live card in the conversation

        async def embed(intent_id, slot=None):
            if __event_emitter__:
                html = (await io("GET", f"card?intent_id={intent_id}&host=openwebui&fragment=0"))["html"]
                if slots.get(slot or intent_id) != html:
                    slots[slot or intent_id] = html
                    await __event_emitter__({"type": "embeds", "data": {"embeds": list(slots.values()), "replace": True}})

        async def choose(title, text, options):
            """A closed question in the chat's own dialog. The reply is an option's label or its number; anything else is no answer."""
            reply = await __event_call__({"type": "input", "data": {"title": title, "message": text, "placeholder": "1", "input": {"type": "select", "options": [o["label"] for o in options]}}})
            if not reply or isinstance(reply, dict):
                return None
            for n, o in enumerate(options, 1):
                if str(reply).strip() in (str(n), o["label"], o["id"]):
                    return o["id"]
            return None

        try:
            await status(S["connecting"])
            if __event_call__ is None:  # Open WebUI also calls a pipe for background tasks (the chat's title, tags): no dialogs there, and they say nothing about what the chat can do, so they declare nothing
                return S["need_dialogs"]
            caps = await io("POST", "capabilities", {"declared": {"event_call": True, "rich_ui": True, "host": "open-webui"}})  # the live evidence of what this chat can do
            net = await io("GET", "network")
            nodes = net["payload"]["data"]["nodes"]
            await embed(net["id"], "network")
            menu = [{"id": "network", "label": S["see_network"]}] + [{"id": f"analyze:{n['id']}", "label": S["analyze"].format(name=n["name"])} for n in nodes] + [{"id": f"options:{n['id']}", "label": S["options"].format(name=n["name"])} for n in nodes] + [{"id": f"prepare:{n['id']}", "label": S["prepare"].format(name=n["name"])} for n in nodes] + [{"id": f"undo:{n['id']}", "label": S["undo"].format(name=n["name"])} for n in nodes]
            picked = await choose(S["menu_title"], S["menu_ask"] + "\n" + "\n".join(f"{i}. {o['label']}" for i, o in enumerate(menu, 1)), menu)
            if picked is None or picked == "network":
                await status(S["done"], True)
                return net["text_fallback"]
            goal, node = picked.split(":", 1)
            run = await io("POST", "runs", {"goal": goal, "node": node})
            await status(S["run_started"].format(id=run["id"]))
            since, text_out = 0, []
            act_since = (await io("GET", "activity?since=0"))["seq"]  # from here on: what else the machines do while this run goes (the run's own lines come with the run)
            for _ in range(MAX_ROUNDS):
                out = await io("GET", f"runs/{run['id']}/intents?since={since}&wait={self.valves.WAIT_S}", timeout=self.valves.WAIT_S + 40)
                await embed((await io("GET", "network"))["id"], "network")  # what each machine is doing now, re-emitted when it changed
                act = await io("GET", f"activity?since={act_since}")
                for line in act["intents"]:
                    act_since = max(act_since, line["seq"])
                    if line.get("run_id") != run["id"]:
                        await status(f"{line['actor']}: {line['text_fallback']}")
                for i in out["intents"]:
                    since = max(since, i["seq"])
                    if i["kind"] == "progress":
                        await status(f"{i['actor']}: {i['text_fallback']}")
                    elif i["kind"] == "show_data":
                        await embed(i["id"], "network" if i["payload"].get("data_kind") == "network" else None)
                        text_out.append(i["text_fallback"])
                    elif i["kind"] == "ask_choice" and i.get("state") == "pending":
                        opts = i["payload"]["options"]
                        answer = await choose(S["menu_title"], i["text_fallback"], opts)
                        if answer is not None:
                            await io("POST", "decisions", {"intent_id": i["id"], "option": answer})
                    elif i["kind"] == "confirm" and i.get("state") == "pending":
                        text_out.append(await self_confirm(io, __event_call__, i))
                if out["run"]["finished"] and not out["waiting"]:
                    break
            await status(S["done"], True)
            return "\n\n".join(text_out) or S["nothing_new"]
        except FileNotFoundError:
            return S["no_coordinator"]
        except Exception as exc:  # the owner sees what failed, in plain words
            await status(S["failed"], True)
            return f"Multi Auto Setup: {type(exc).__name__}: {str(exc)[:300]}"


async def self_confirm(io, event_call, intent):
    """A write: the chat's own confirmation dialog, bound to the plan hash. The harness's text is shown as it is; the answer goes back with the hash the dialog was made for."""
    b = await io("POST", "confirmations", {"intent_id": intent["id"], "mechanism": "host_dialog"})
    ok = await event_call({"type": "confirmation", "data": {"title": S["approve_title"], "message": b["prompt"]["message"]}})
    res = await io("POST", f"confirmations/{b['ticket']}/complete", {"plan_hash": b["plan_hash"], "action": "accept" if ok is True else "decline"})
    return S["your_answer"].format(state=res["state"])
