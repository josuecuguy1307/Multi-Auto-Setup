"""Verifier for a REAL language-model server (R4). The nonce echo in server_check.py is not enough: an echo stub passes it.

`host.model_server` becomes `verified` only when every condition below holds. All conditions are evaluated (no early
exit) so a failure report names each one that broke:

  identity            the server reports the model file the manifest entry names (/v1/models or /props)
  bind_loopback       nothing listens on the server port except 127.0.0.1 / ::1
  generation          a generation request returns non-empty text
  token_count         the reported completion token count is plausible for the text and within the cap
  nonce_roundtrip     a fresh random token sent now comes back in the reply, every round, over the same channel the caller uses
  not_a_mirror        the reply does not just contain the prompt (a stub that prefixes and echoes fails here)
  distinct_replies    different requests did not get the same reply

Measurements (tokens/s, resident memory, startup time) are recorded as `tested` facts, with where each number came from,
and only when the server is verified: a number measured on the wrong model is not a fact about the right one.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from envrec.contracts import CreatedBy, Evidence, Fact, FactStatus, Target
from envrec.store import Store

CONDITIONS = ("identity", "bind_loopback", "generation", "token_count", "nonce_roundtrip", "not_a_mirror", "distinct_replies")
LOOPBACK = {"0100007F", "00000000000000000000000001000000"}  # /proc/net/tcp and tcp6 notation for 127.0.0.1 and ::1
THROUGHPUT_PROMPT = "Write a short sentence about the sea."
MAX_TOKENS = 48


class ModelServerClient(Protocol):
    def get_json(self, path: str) -> Any: ...
    def chat_raw(self, content: str, max_tokens: int) -> dict: ...


class DeviceProbes(Protocol):
    def listeners(self) -> list[str]:
        """Hex addresses (as /proc/net/tcp prints them) listening on the server's port."""
    def rss_mib(self) -> float | None: ...


class RemoteProbes:
    """DeviceProbes over the template runner: which addresses listen on the port, and the server process's resident memory."""

    def __init__(self, runner, target_id: str, transport, port: int, binary: str) -> None:
        self.runner, self.target_id, self.transport, self.port, self.binary = runner, target_id, transport, port, binary

    def listeners(self) -> list[str]:
        r = self.runner.run(self.target_id, self.transport, "listeners", {"port": self.port})
        if not r.ok:
            raise ConnectionError(r.stderr.strip()[:120] or "listeners failed")
        return r.stdout.split()

    def rss_mib(self) -> float | None:
        r = self.runner.run(self.target_id, self.transport, "server_rss", {"binary": self.binary})
        kb = [int(x) for x in r.stdout.split() if x.isdigit()] if r.ok else []
        return round(max(kb) / 1024, 1) if kb else None


@dataclass(frozen=True)
class ExpectedModel:
    entry_id: str  # catalog / manifest id
    file: str  # file name the server must report


@dataclass
class ModelVerification:
    ok: bool
    failed: list[str]
    details: dict[str, str] = field(default_factory=dict)
    tokens_per_s: float | None = None
    tokens_per_s_source: str = ""
    rss_mib: float | None = None
    startup_s: float | None = None
    replies: list[str] = field(default_factory=list)

    @property
    def reason(self) -> str:
        return "all conditions held" if self.ok else "; ".join(f"{c}: {self.details.get(c, 'failed')}" for c in self.failed)


def reported_identities(models: Any, props: Any) -> set[str]:
    """Every name the server uses for its model: ids and paths, reduced to file names."""
    names: set[str] = set()
    for m in (models or {}).get("data", []) if isinstance(models, dict) else []:
        for k in ("id", "name", "model", "path"):
            if isinstance(m.get(k), str):
                names.add(m[k])
    if isinstance(props, dict):
        for k in ("model_path", "model_alias", "model"):
            if isinstance(props.get(k), str):
                names.add(props[k])
    return {os.path.basename(n) for n in names if n}


def plausible_tokens(text: str, tokens: Any, cap: int) -> str | None:
    """None when the count is believable for this text; otherwise why not."""
    if not isinstance(tokens, int) or isinstance(tokens, bool):
        return "the server reported no completion token count"
    if tokens < 1:
        return f"{tokens} tokens for non-empty text"
    if tokens > cap + 1:
        return f"{tokens} tokens is over the {cap} token cap that was requested"
    if tokens > len(text) + 1:
        return f"{tokens} tokens for {len(text)} characters"
    if len(text) > 40 * tokens:
        return f"{len(text)} characters in {tokens} tokens"
    return None


def verify_model(client: ModelServerClient, probes: DeviceProbes, store: Store, target: Target, expected: ExpectedModel, *,
                 rounds: int = 2, startup_s: float | None = None, nonce: Callable[[], str] = lambda: secrets.token_hex(8),
                 clock: Callable[[], float] = time.monotonic) -> ModelVerification:
    failed: dict[str, str] = {}

    def fail(cond: str, why: str) -> None:
        failed.setdefault(cond, why)

    # identity
    try:
        models = client.get_json("/v1/models")
    except Exception as exc:
        models = None
        fail("identity", f"/v1/models unavailable: {type(exc).__name__}")
    try:
        props = client.get_json("/props")
    except Exception:
        props = None
    from_models, from_props = reported_identities(models, None), reported_identities(None, props)
    if "identity" not in failed:
        # every source that names a model must name the expected one: a server that says two different things fails
        if expected.file not in from_models:
            fail("identity", f"/v1/models reports {sorted(from_models) or 'no model name'}, the manifest entry is {expected.file}")
        elif from_props and expected.file not in from_props:
            fail("identity", f"/props reports {sorted(from_props)}, /v1/models and the manifest entry say {expected.file}")

    # bind address
    try:
        addrs = probes.listeners()
    except Exception as exc:
        addrs = []
        fail("bind_loopback", f"could not read listeners: {type(exc).__name__}")
    if "bind_loopback" not in failed:
        exposed = sorted({a.split(":")[0] for a in addrs} - LOOPBACK)
        if not addrs:
            fail("bind_loopback", "nothing is listening on the server port")
        elif exposed:
            fail("bind_loopback", f"listening beyond loopback: {exposed}")

    # generation + token count on a free-form prompt, timed for throughput
    text, tps, tps_src = "", None, ""
    try:
        t0 = clock()
        resp = client.chat_raw(THROUGHPUT_PROMPT, MAX_TOKENS)
        wall = max(clock() - t0, 1e-6)
        text = (resp["choices"][0]["message"]["content"] or "")
        usage = resp.get("usage") or {}
        toks = usage.get("completion_tokens")
        if not text.strip():
            fail("generation", "the reply text is empty")
        else:
            bad = plausible_tokens(text, toks, MAX_TOKENS)
            if bad:
                fail("token_count", bad)
            elif isinstance(toks, int):
                server_tps = (resp.get("timings") or {}).get("predicted_per_second")
                if isinstance(server_tps, (int, float)) and server_tps > 0:
                    tps, tps_src = round(float(server_tps), 2), "reported by the server (timings.predicted_per_second)"
                else:
                    tps, tps_src = round(toks / wall, 2), "tokens over wall-clock time, includes request overhead"
    except Exception as exc:
        fail("generation", f"generation request failed: {type(exc).__name__}: {str(exc)[:100]}")
    if "generation" in failed:
        fail("token_count", "no generation to count")

    # fresh nonce round trips, and the mirror and distinctness checks over them
    nonces, replies = [nonce() for _ in range(rounds)], []
    for n in nonces:
        prompt = f"Repeat this token exactly: {n}"
        try:
            r = client.chat_raw(prompt, MAX_TOKENS)["choices"][0]["message"]["content"] or ""
        except Exception as exc:
            fail("nonce_roundtrip", f"request failed: {type(exc).__name__}: {str(exc)[:100]}")
            break
        replies.append(r)
        if n not in r:
            fail("nonce_roundtrip", "the reply did not contain the fresh token")
        if prompt in r:
            fail("not_a_mirror", "the reply contains the whole prompt")
    if len(replies) < rounds:
        fail("nonce_roundtrip", failed.get("nonce_roundtrip", "round trips incomplete"))
    elif len(set(replies)) < len(replies):
        fail("distinct_replies", "different requests got identical replies")

    rss = None
    try:
        rss = probes.rss_mib()
    except Exception:
        pass

    result = ModelVerification(not failed, [c for c in CONDITIONS if c in failed], failed, tps, tps_src, rss, startup_s, replies)
    if result.ok:
        _record(store, target, expected, result, nonces, text)
    return result


def _record(store: Store, target: Target, expected: ExpectedModel, res: ModelVerification, nonces: list[str], sample: str) -> None:
    raw = store.put_raw(json.dumps({"expected": expected.entry_id, "file": expected.file, "nonces": nonces, "replies": res.replies,
                                    "sample": sample, "tokens_per_s": res.tokens_per_s, "rss_mib": res.rss_mib}))
    ev = lambda probe, conf, lim: Evidence(provider="verifier", probe=probe, raw_ref=raw, confidence=conf, limitations=lim)  # noqa: E731
    store.write_fact(Fact(target_id=target.id, key="host.model_server", value="running", status=FactStatus.verified, created_by=CreatedBy.verifier,
                          evidence=[ev("model_check", 0.95, ["identity, loopback bind, non-empty plausible generation and a fresh-nonce round trip held at one moment"])]))
    store.write_fact(Fact(target_id=target.id, key="host.model_identity", value=expected.entry_id, status=FactStatus.tested, created_by=CreatedBy.verifier,
                          evidence=[ev("model_identity", 0.9, ["the server's own report of its model file matches the manifest entry"])]))
    measured = [("host.model_tokens_per_s", res.tokens_per_s, res.tokens_per_s_source), ("host.model_rss_mib", res.rss_mib, "resident set of the server process"),
                ("host.model_startup_s", res.startup_s, "from process start to the health endpoint answering")]
    for key, value, src in measured:
        if value is not None:
            store.write_fact(Fact(target_id=target.id, key=key, value=round(float(value), 2), status=FactStatus.tested, created_by=CreatedBy.verifier,
                                  evidence=[ev("model_measure", 0.8, [src, "one sample, small prompt"])]))
