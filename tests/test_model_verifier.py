"""R4: the real-model verifier. Each failing condition is tested on its own; the canned fixture and the old echo stub must fail."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from envrec.contracts import CreatedBy, Evidence, Fact, FactStatus, Target, TargetKind
from envrec.store import Store
from envrec.verify import ExpectedModel, TunnelClient, verify_model
from envrec.verify.model_check import plausible_tokens, reported_identities

FAKES = Path(__file__).parent / "fakes"
FILE = "tiny-test.gguf"
EXPECTED = ExpectedModel("qwen-tiny", FILE)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Probes:
    def __init__(self, listeners=("0100007F",), rss=123.0):
        self._l, self._rss = list(listeners), rss

    def listeners(self):
        return [f"{a}:1F90" for a in self._l]

    def rss_mib(self):
        return self._rss


class Raising(Probes):
    def listeners(self):
        raise OSError("no /proc")


def launch(script: str, *args: str, mode: str = "ok"):
    port = free_port()
    cmd = [sys.executable, str(FAKES / script), "--port", str(port), *args]
    proc = subprocess.Popen(cmd, env={**os.environ, "FAKE_MODE": mode}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=0.5).status_code == 200:
                return proc, port
        except httpx.HTTPError:
            time.sleep(0.05)
    proc.kill()
    raise RuntimeError("fixture server did not start")


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "s") as s:
        yield s


@pytest.fixture
def target(store):
    t = store.put_target(Target(kind=TargetKind.net_host, ring=2, handle="box", authorized=True), from_config=True)
    # the deploy has recorded its claim that the server runs; the verifier only judges it (a verifier never creates the claim it verifies)
    store.write_fact(Fact(target_id=t.id, key="host.model_server", value="running", status=FactStatus.hypothesis, created_by=CreatedBy.loop,
                          evidence=[Evidence(provider="deploy", probe="start_server+health", confidence=0.6)]))
    return t


def check(store, target, mode="ok", probes=None, expected=EXPECTED, script="fake_llama_server.py", args=None):
    proc, port = launch(script, *(args if args is not None else ["--host", "127.0.0.1", "-m", f"/m/{FILE}"]), mode=mode)
    try:
        return verify_model(TunnelClient(port), probes or Probes(), store, target, expected, startup_s=1.5)
    finally:
        proc.kill()
        proc.wait()


def test_a_real_looking_server_passes_and_facts_are_recorded(store, target):
    r = check(store, target)
    assert r.ok and r.failed == [] and r.reason == "all conditions held"
    f = store.current_fact(target.id, "host.model_server")
    assert f.status == FactStatus.verified and f.created_by.value == "verifier"
    assert store.current_fact(target.id, "host.model_identity").value == "qwen-tiny"
    for key, val in (("host.model_tokens_per_s", 50.0), ("host.model_rss_mib", 123.0), ("host.model_startup_s", 1.5)):
        fact = store.current_fact(target.id, key)
        assert fact.status == FactStatus.tested and fact.value == val
    assert "reported by the server" in store.current_fact(target.id, "host.model_tokens_per_s").evidence[0].limitations[0]


def test_wrong_model_identity_fails_only_identity(store, target):
    r = check(store, target, mode="wrong_model")
    assert r.failed == ["identity"] and "some-other-model.gguf" in r.details["identity"]
    assert store.current_fact(target.id, "host.model_server").status == FactStatus.hypothesis  # the deploy's claim stays a hypothesis: nothing was verified and store.current_fact(target.id, "host.model_tokens_per_s") is None


def test_manifest_entry_naming_another_file_fails_identity(store, target):
    r = check(store, target, expected=ExpectedModel("x", "not-this-file.gguf"))
    assert r.failed == ["identity"]


def test_canned_reply_server_fails_the_nonce_round_trip(store, target):
    r = check(store, target, mode="canned")
    assert "nonce_roundtrip" in r.failed and "distinct_replies" in r.failed and not r.ok
    assert store.current_fact(target.id, "host.model_server").status == FactStatus.hypothesis  # the deploy's claim stays a hypothesis: nothing was verified


def test_empty_text_fails_generation(store, target):
    r = check(store, target, mode="empty")
    assert "generation" in r.failed and not r.ok


def test_implausible_token_count_fails_token_count_only(store, target):
    r = check(store, target, mode="bad_usage")
    assert r.failed == ["token_count"] and "cap" in r.details["token_count"]


def test_missing_usage_fails_token_count(store, target):
    r = check(store, target, mode="no_usage")
    assert r.failed == ["token_count"] and "no completion token count" in r.details["token_count"]


def test_a_server_that_mirrors_the_prompt_fails(store, target):
    r = check(store, target, mode="mirror")
    assert "not_a_mirror" in r.failed and not r.ok


def test_listening_beyond_loopback_fails_only_bind(store, target):
    r = check(store, target, probes=Probes(listeners=("00000000",)))
    assert r.failed == ["bind_loopback"] and "00000000" in r.details["bind_loopback"]


def test_one_loopback_and_one_exposed_address_fails(store, target):
    assert check(store, target, probes=Probes(listeners=("0100007F", "00000000000000000000000000000000"))).failed == ["bind_loopback"]


def test_ipv6_loopback_is_fine(store, target):
    assert check(store, target, probes=Probes(listeners=("00000000000000000000000001000000",))).ok


def test_nothing_listening_or_unreadable_listeners_fails_bind(store, target):
    assert check(store, target, probes=Probes(listeners=())).failed == ["bind_loopback"]
    assert check(store, target, probes=Raising()).failed == ["bind_loopback"]


def test_the_old_echo_stub_fails(store, target):
    r = check(store, target, script="stub_echo_server.py", args=[])
    assert not r.ok and "identity" in r.failed and "not_a_mirror" in r.failed
    assert store.current_fact(target.id, "host.model_server").status == FactStatus.hypothesis  # the deploy's claim stays a hypothesis: nothing was verified


def test_the_old_echo_stub_that_claims_the_right_model_still_fails(store, target):
    r = check(store, target, script="stub_echo_server.py", args=["--claim-model", FILE])
    assert r.failed == ["not_a_mirror"] or ("not_a_mirror" in r.failed and "identity" not in r.failed)
    assert not r.ok


def test_unreachable_server_fails_every_network_condition(store, target):
    r = verify_model(TunnelClient(free_port(), timeout=1), Probes(), store, target, EXPECTED)
    assert not r.ok and {"identity", "generation", "nonce_roundtrip"} <= set(r.failed)


def test_nonces_are_fresh_each_run(store, target):
    a, b = check(store, target), check(store, target)
    assert a.ok and b.ok and not set(a.replies) & set(b.replies)


def test_helpers():
    assert reported_identities({"data": [{"id": "/models/a/b.gguf"}]}, {"model_path": "/x/c.gguf"}) == {"b.gguf", "c.gguf"}
    assert reported_identities(None, None) == set()
    assert plausible_tokens("hello there", 2, 48) is None
    assert plausible_tokens("hello", 0, 48) and plausible_tokens("hello", True, 48) and plausible_tokens("x" * 500, 1, 48)
    assert plausible_tokens("hi", 30, 48) and plausible_tokens("hi", 49, 48)
