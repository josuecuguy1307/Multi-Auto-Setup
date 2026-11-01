"""SIMULATED-LOCAL stand-ins for the board's end-to-end run. NOT the router: T4a has not merged it. `StandInRouter` follows docs/ROUTER_API.md (types, refusal codes, calls, rule order, filter) closely enough to drive the
board, and no further. `LoopbackModelServer` is a real HTTP server bound to 127.0.0.1 that answers like a chat-completions endpoint, one path prefix per machine. Nothing here is imported by `src/`."""
from __future__ import annotations

import hashlib
import json
import re
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

COST = {"local": 0, "api": 1, "subscription": 2}
RANK = {"public": 0, "internal": 1, "confidential": 2}
ENVS = [  # what the stand-in knows about the network: one local model on one machine, one subscription CLI on another
    {"environment_id": "llamacpp", "machine_id": "lenovo", "subtype": "local_model", "model_id": "qwen-local", "access_id": "local-hw", "cost": "local"},
    {"environment_id": "claude-code", "machine_id": "mac", "subtype": "provider", "model_id": "claude", "access_id": "plan-sub", "cost": "subscription"},
]
TEXT = {
    "confidential_from_cloud_chat": "This task is confidential, so it can only be created from the owner panel or a local chat, not from a cloud chat.",
    "sensitivity_missing": "The task has no sensitivity level.",
    "unknown_task_type": "There is no admission suite for this task type.",
    "no_candidate": "No environment can run this task on this network.",
    "no_local_candidate": "This task is confidential and no local environment on a paired machine can run it.",
    "provisional_only": "This task is confidential and only unmeasured combinations exist, so it waits for admission results.",
    "escalation_exhausted": "No further step is allowed for this task, so it needs your decision.",
    "no_owner_origin": "No owner event created or approved this task, so it was not sent.",
    "payload_missing": "The task's input was not found.",
    "stale_decision": "That decision is not the task's latest.",
    "weak_grader": "Only a code grader can fail a task; this verdict was ignored.",
    "no_grader": "No code grader is registered for this task.",
    "output_missing": "There is no such output for that task.",
    "sandbox_missing": "This grader runs model-written code and no sandbox is ready.",
    "confidential_cloud_environment": "An input is confidential and the chosen environment is in the cloud, so the task was not sent.",
}


def key(env: dict) -> str:
    return f"{env['environment_id']}@{env['machine_id']}"


class StandInRouter:
    """Follows docs/ROUTER_API.md as updated 2026-10-30 (exclude, result_of / set_grader / grade, result-of references, decompositions with slots). Not the router."""

    def __init__(self, server_port: int, store: dict[str, str], *, admission: dict[str, int] | None = None, open_providers_for: tuple = ("public",)) -> None:
        self.port, self.store, self.admission, self.open_providers_for = server_port, store, admission or {}, open_providers_for
        self.tried: dict[str, list[str]] = {}
        self.decisions: dict[str, dict] = {}
        self.tasks: dict[str, dict] = {}  # what route() saw: sensitivity, refs, exclude (travels with the task)
        self.outputs: dict[str, dict] = {}  # output_ref -> {task_id, text, level}
        self.task_outputs: dict[str, str] = {}
        self.graders: dict[str, dict] = {}
        self.results: dict[str, dict] = {}
        self.decomps: dict[str, dict] = {}
        self.events: dict[str, dict] = {}
        self.origin: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.filtered: list[tuple[dict, dict]] = []

    # ---- owner origin (section 12): the decision is an event in the router's store, re-checked by id ---------------------------------------------------
    def record_owner_decision(self, parent_task_id: str, subtasks: list[dict], gesture: str) -> str:
        self.calls.append(("record_owner_decision", parent_task_id, gesture))
        ev = f"owner-ev-{len(self.events) + 1}"
        self.events[ev] = {"tag": "OWNER" if gesture in ("os_dialog", "host_dialog", "panel") else "CLAUDE-CODE", "parent": parent_task_id, "subtasks": json.loads(json.dumps(subtasks))}
        return ev

    def approve_decomposition(self, parent_task_id: str, subtasks: list[dict], owner_event_id: str):
        self.calls.append(("approve_decomposition", parent_task_id, len(subtasks)))
        e = self.events.get(owner_event_id)
        if not e or e["tag"] != "OWNER" or e["parent"] != parent_task_id or e["subtasks"] != subtasks:  # character for character
            return self._refuse(parent_task_id, "no_owner_origin")
        did = "dec_" + hashlib.sha256(json.dumps([parent_task_id, subtasks, owner_event_id], sort_keys=True).encode()).hexdigest()[:20]
        self.decomps[did] = {"event": owner_event_id, "subtasks": {x["id"]: x for x in subtasks}, "held": {}}
        return did

    def own_subtask(self, task_id: str, decomposition_id: str, subtask_id: str):
        self.calls.append(("own_subtask", task_id, subtask_id))
        d = self.decomps.get(decomposition_id)
        if not d or subtask_id not in d["subtasks"] or d["held"].get(subtask_id, task_id) != task_id:
            return None
        d["held"][subtask_id] = task_id
        self.origin[task_id] = {"event": d["event"], "listed": d["subtasks"][subtask_id]}
        return d["event"]

    def _refuse(self, tid: str, code: str, details=None) -> dict:
        return {"task_id": tid, "code": code, "reason": TEXT[code], "details": details or {}}

    def _level_of(self, ref: str, own: str) -> str:
        if ref.startswith("result-of:"):
            src = self.tasks.get(ref.split(":", 1)[1])
            return src["effective"] if src else "confidential"  # unknown source: confidential
        if ref.startswith("instr:"):
            return "public"  # project-written text with no task data never raises a level
        return own

    def _decide(self, tid: str, step: int) -> dict:
        task = self.tasks[tid]
        sens, ex = task["effective"], task.get("exclude") or {}
        cands = [e for e in ENVS if key(e) not in self.tried.get(tid, [])]
        trace, cur = [], list(cands)

        def stepx(name, out, **kw):
            nonlocal cur
            trace.append({"step": name, "candidates_in": [key(e) for e in cur], "candidates_out": [key(e) for e in out], **kw})
            cur = out
        if sens == "confidential":
            local = [e for e in cur if e["cost"] == "local"]
            stepx("sensitivity", local)
        elif sens in self.open_providers_for:
            stepx("sensitivity", cur)
        else:
            stepx("sensitivity", [e for e in cur if e["cost"] == "local"])
        provisional_only = False
        if sens == "confidential" and cur:
            measured = [e for e in cur if self.admission.get(key(e), 0) >= 8]
            provisional_only = not measured
            cur = measured or cur
        if ex:
            stepx("exclusion", [e for e in cur if e["machine_id"] not in ex.get("machine_ids", []) and e["environment_id"] not in ex.get("environment_ids", [])])
        if not cur:
            if sens == "confidential":
                return self._refuse(tid, "no_local_candidate")
            return self._refuse(tid, "escalation_exhausted" if step else "no_candidate")
        if provisional_only:
            return self._refuse(tid, "provisional_only")
        low = min(COST[e["cost"]] for e in cur)
        stepx("cost_class", [e for e in cur if COST[e["cost"]] == low], values={key(e): e["cost"] for e in cur})
        best = max(self.admission.get(key(e), -1) for e in cur)
        stepx("admission_rate", [e for e in cur if self.admission.get(key(e), -1) == best], values={key(e): self.admission.get(key(e), "no data") for e in cur}, margin=0.1)
        env = sorted(cur, key=lambda e: e["environment_id"])[0]
        stepx("tie_break", [env])
        self.tried.setdefault(tid, []).append(key(env))
        n = self.admission.get(key(env))
        route = {k: env[k] for k in ("environment_id", "subtype", "model_id", "access_id", "machine_id")}
        d = {"task_id": tid, "route": route, "rule_trace": trace, "escalation_step": step, "evidence_label": f"measured here {n}/10" if n is not None else "no data", "effective_sensitivity": sens}
        self.decisions[tid] = d
        return d

    def route(self, task: dict) -> dict:
        self.calls.append(("route", task["task_id"]))
        tid = task["task_id"]
        if task.get("sensitivity") not in RANK:
            return self._refuse(tid, "sensitivity_missing")
        if task["task_type"] not in ("code", "text"):
            return self._refuse(tid, "unknown_task_type")
        refs = [task["payload_ref"], *task.get("input_refs", [])]
        eff = max([task["sensitivity"], *[self._level_of(r, task["sensitivity"]) for r in refs]], key=lambda x: RANK[x])
        self.tasks[tid] = {"sensitivity": task["sensitivity"], "effective": eff, "refs": refs, "exclude": task.get("exclude"), "type": task["task_type"]}
        if eff == "confidential" and task["entry_chat"]["kind"] != "local":
            return self._refuse(tid, "confidential_from_cloud_chat")
        return self._decide(tid, 0)

    def _text_of(self, ref: str) -> str | None:
        if ref.startswith("result-of:"):
            ref_out = self.task_outputs.get(ref.split(":", 1)[1])
            return self.outputs[ref_out]["text"] if ref_out else None
        return self.store.get(ref)

    def dispatch(self, decision: dict) -> dict:
        tid, m = decision["task_id"], decision["route"]["machine_id"]
        self.calls.append(("dispatch", tid, m))
        if tid not in self.origin:
            return self._refuse(tid, "no_owner_origin")
        env = next(e for e in ENVS if e["machine_id"] == m)
        task = self.tasks[tid]
        listed = self.origin[tid]["listed"]  # the task actually being sent must match what the owner listed: same type, level at least the listed one
        if task["type"] != listed["type"] or RANK[task["effective"]] < RANK[listed["sensitivity"]]:
            return self._refuse(tid, "no_owner_origin")
        for r in task["refs"]:
            if r.startswith("result-of:") and self._level_of(r, "public") == "confidential" and env["cost"] != "local":
                return self._refuse(tid, "confidential_cloud_environment")
        texts = [self._text_of(r) for r in task["refs"]]
        if any(t is None for t in texts):
            return self._refuse(tid, "payload_missing")
        prompt = texts[0] if len(texts) == 1 else texts[0] + "".join(f"\n--- input {n} ---\n{t}" for n, t in enumerate(texts[1:], 2))
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/{m}/v1/chat/completions", data=json.dumps({"messages": [{"role": "user", "content": prompt}]}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            out = json.load(r)["choices"][0]["message"]["content"]
        h = {"task_id": tid, "decision_ref": f"{tid}@{decision['escalation_step']}", "machine_id": m, "task_state": "done"}
        self.results[h["decision_ref"]] = {"state": "done", "text": out}
        return h

    def result_of(self, handle: dict) -> dict:
        self.calls.append(("result_of", handle["task_id"]))
        res = self.results[handle["decision_ref"]]
        if res["state"] != "done":
            return {"state": res["state"], "output_ref": None}
        ref = hashlib.sha256((handle["task_id"] + res["text"]).encode()).hexdigest()
        self.outputs[ref] = {"task_id": handle["task_id"], "text": res["text"], "level": self.tasks[handle["task_id"]]["effective"]}
        self.task_outputs[handle["task_id"]] = ref
        return {"state": "done", "output_ref": ref}

    def set_grader(self, task_id: str, spec: dict) -> None:
        self.calls.append(("set_grader", task_id, spec["kind"]))
        self.graders[task_id] = spec

    def grade(self, task_id: str, output_ref: str) -> dict:
        self.calls.append(("grade", task_id))
        out = self.outputs.get(output_ref)
        if out is None or out["task_id"] != task_id:
            return self._refuse(task_id, "output_missing")
        spec = self.graders.get(task_id)
        if spec is None or spec["kind"] in ("model", "human"):
            return self._refuse(task_id, "no_grader" if spec is None else "weak_grader")
        if spec["kind"] == "run_tests":
            return self._refuse(task_id, "sandbox_missing")
        text, v = out["text"], spec["value"]
        passed = {"equals": text == v, "contains": v in text, "regex": re.search(v, text) is not None, "sha256": hashlib.sha256(text.encode()).hexdigest() == v}[spec["kind"]]
        return {"passed": passed, "grader": "code", "evidence_ref": hashlib.sha256(f"{task_id}|{output_ref}|{spec['kind']}|{passed}".encode()).hexdigest()}

    def report_result(self, task_id: str, escalation_step: int, grader_result: dict) -> dict | None:
        self.calls.append(("report_result", task_id, escalation_step, grader_result["passed"]))
        if self.decisions.get(task_id, {}).get("escalation_step") != escalation_step:
            return self._refuse(task_id, "stale_decision")
        if grader_result.get("grader") != "code":
            return self._refuse(task_id, "weak_grader")
        if grader_result["passed"]:
            return None
        if escalation_step >= 2:
            return self._refuse(task_id, "escalation_exhausted")
        d = self._decide(task_id, escalation_step + 1)
        return d if "route" in d else self._refuse(task_id, "escalation_exhausted")

    def filter_outbound(self, intent: dict, entry_chat: dict) -> dict:
        """ROUTER_API section 5."""
        self.filtered.append((intent, entry_chat))
        known = entry_chat.get("kind") in ("local", "cloud") and intent.get("sensitivity") in RANK
        kind, sens = (entry_chat["kind"], intent["sensitivity"]) if known else ("cloud", "confidential")
        if kind == "local":
            return intent
        if sens == "confidential":
            p = intent.get("payload", {})
            slim = {"task_id": p.get("task_id"), "state": p.get("state"), "machine": p.get("machine_id") or p.get("node_id")}
            return {**intent, "sensitivity": "confidential", "payload": slim, "redacted": True,
                    "text_fallback": f"Task {slim['task_id'] or '?'}: {slim['state'] or 'update'} on {slim['machine'] or 'the network'}."}
        return intent


class LoopbackModelServer:
    """A chat-completions look-alike on 127.0.0.1. `answer(machine, prompt) -> str` is the scripted behavior."""

    def __init__(self, answer, port: int = 0) -> None:
        outer = self
        self.answer, self.requests = answer, []

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                machine = self.path.strip("/").split("/")[0]
                prompt = body["messages"][-1]["content"]
                outer.requests.append((machine, prompt))
                data = json.dumps({"choices": [{"message": {"role": "assistant", "content": outer.answer(machine, prompt)}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), H)
        self.port = self.httpd.server_address[1]
        self._t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._t.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
