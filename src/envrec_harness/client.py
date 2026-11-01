"""A thin client of the harness core API (`/harness/v1/*`), for chat adapters. Standard library only, so an adapter that runs inside another program (Open WebUI) needs nothing installed.

It reads the endpoint and its channel's token from the coordinator's `harness/` folder and sends them; it makes no decision and keeps no state but the connection details."""

from __future__ import annotations

import http.client
import json
from pathlib import Path


class CoreError(Exception):
    def __init__(self, status: int, error: str, message: str = "") -> None:
        super().__init__(f"{status} {error}")
        self.status, self.error, self.message = status, error, message


class CoreClient:
    def __init__(self, harness_dir: Path, channel: str, *, timeout: float = 30.0, operator: bool = False) -> None:
        self.dir, self.channel, self.timeout, self.operator = Path(harness_dir), channel, timeout, operator  # operator: a verification run driven by the assistant, not by the owner: the core logs it as such

    def _conn(self) -> tuple[http.client.HTTPConnection, dict]:
        ep = json.loads((self.dir / "endpoint.json").read_text())
        token = (self.dir / f"{self.channel}.token").read_text().strip()
        return http.client.HTTPConnection(ep["host"], ep["port"], timeout=self.timeout), {"Host": f"{ep['host']}:{ep['port']}", "Authorization": f"Bearer {token}"}

    def call(self, method: str, path: str, body: dict | None = None, *, timeout: float | None = None) -> dict:
        conn, headers = self._conn()
        if timeout:
            conn.timeout = timeout
        try:
            if body is not None:
                headers["Content-Type"] = "application/json"
            conn.request(method, "/harness/v1/" + path, body=json.dumps(body) if body is not None else None, headers=headers)
            r = conn.getresponse()
            data = r.read()
        finally:
            conn.close()
        doc = json.loads(data) if data.startswith(b"{") else {}
        if r.status != 200:
            raise CoreError(r.status, str(doc.get("error", "error")), str(doc.get("message", "")))
        return doc

    def _by(self) -> dict:
        return {"by": "claude-code"} if self.operator else {}

    # one method per route of the core API, nothing more
    def capabilities(self) -> dict: return self.call("GET", "capabilities")
    def declare(self, declared: dict) -> dict: return self.call("POST", "capabilities", {"declared": declared})
    def view(self, context: dict) -> dict: return self.call("POST", "view", {"context": context})
    def network(self) -> dict: return self.call("GET", "network")
    def tools(self) -> dict: return self.call("GET", "tools")
    def tool(self, name: str, arguments: dict) -> dict: return self.call("POST", f"tools/{name}", {"arguments": arguments})
    def activity(self, since: int = 0) -> dict: return self.call("GET", f"activity?since={int(since)}")
    def start(self, goal: str, node: str) -> dict: return self.call("POST", "runs", {"goal": goal, "node": node, **self._by()})
    def intents(self, run_id: str, since: int = 0, wait: float = 0) -> dict: return self.call("GET", f"runs/{run_id}/intents?since={int(since)}&wait={wait}", timeout=self.timeout + wait)
    def pairing(self, machine: str | None = None) -> dict: return self.call("POST", "pairing", {"machine": machine} if machine else {})
    def unpair(self, machine: str) -> dict: return self.call("POST", "unpair", {"machine": machine})
    def decide(self, intent_id: str, option: str) -> dict: return self.call("POST", "decisions", {"intent_id": intent_id, "option": option, **self._by()})
    def begin_confirmation(self, intent_id: str, mechanism: str) -> dict: return self.call("POST", "confirmations", {"intent_id": intent_id, "mechanism": mechanism})
    def ticket(self, ticket: str) -> dict: return self.call("GET", f"confirmations/{ticket}")
    def complete_confirmation(self, ticket: str, plan_hash: str, action: str) -> dict: return self.call("POST", f"confirmations/{ticket}/complete", {"plan_hash": plan_hash, "action": action})
    def card(self, intent_id: str, host: str) -> str: return self.call("GET", f"card?intent_id={intent_id}&host={host}&fragment=1")["html"]
