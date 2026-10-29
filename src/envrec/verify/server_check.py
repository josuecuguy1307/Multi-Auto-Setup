"""Clean-context check that a model server is really running and really answering.

The check generates fresh random nonces at verification time and asks the server to repeat them, so no
earlier turn could have precomputed the answer. A server that returns the same canned text whatever it is
sent fails. Only after every round passes is the fact marked `verified`.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from envrec.contracts import CreatedBy, Evidence, Fact, FactStatus, Target
from envrec.mission.models import Transport
from envrec.remote.runner import RemoteRunner
from envrec.store import Store


class ServerClient(Protocol):
    def chat(self, content: str) -> str: ...


class TunnelClient:
    """Talks to the device's localhost server through an SSH tunnel the controller opened."""

    def __init__(self, local_port: int, timeout: float = 10.0) -> None:
        self.url, self.timeout = f"http://127.0.0.1:{local_port}/v1/chat/completions", timeout

    def chat(self, content: str) -> str:
        r = httpx.post(self.url, json={"messages": [{"role": "user", "content": content}]}, timeout=self.timeout)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


class ExecClient:
    """Sends the request from inside the device (docker exec), for transports that have no tunnel."""

    def __init__(self, runner: RemoteRunner, target_id: str, transport: Transport, port: int) -> None:
        self.runner, self.target_id, self.transport, self.port = runner, target_id, transport, port

    def chat(self, content: str) -> str:
        body = json.dumps({"messages": [{"role": "user", "content": content}]}).encode()
        r = self.runner.run(self.target_id, self.transport, "chat_request", {"port": self.port}, stdin=body)
        if not r.ok:
            raise ConnectionError(r.stderr.strip()[:160] or "request failed")
        return json.loads(r.stdout)["choices"][0]["message"]["content"]


@dataclass
class VerifyResult:
    ok: bool
    reason: str
    rounds: int = 0
    replies: list[str] = field(default_factory=list)


def verify_server(client: ServerClient, store: Store, target: Target, *, rounds: int = 2,
                  nonce: Callable[[], str] = lambda: secrets.token_hex(8)) -> VerifyResult:
    nonces = [nonce() for _ in range(rounds)]
    replies: list[str] = []
    for n in nonces:
        try:
            reply = client.chat(f"Repeat this token exactly: {n}")
        except Exception as exc:  # any transport failure is a failed check, not a crash
            return VerifyResult(False, f"no usable answer: {type(exc).__name__}: {str(exc)[:120]}", len(replies), replies)
        replies.append(reply)
        if n not in reply:
            return VerifyResult(False, "the reply did not contain the fresh nonce: the server is not answering this request",
                                len(replies), replies)
    if len(set(replies)) < len(replies):
        return VerifyResult(False, "identical replies to different requests", len(replies), replies)
    raw = store.put_raw(json.dumps({"nonces": nonces, "replies": replies}))
    store.write_fact(Fact(
        target_id=target.id, key="host.model_server", value="running", status=FactStatus.verified, created_by=CreatedBy.verifier,
        evidence=[Evidence(provider="verifier", probe="nonce_echo", raw_ref=raw, confidence=0.95,
                           limitations=["proves the server echoes fresh input; the test runtime is a stub, not a language model"])]))
    return VerifyResult(True, "fresh nonces were echoed", rounds, replies)
