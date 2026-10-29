"""P6: 'the server is running' is verified with fresh nonces; a canned-reply server must fail."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from envrec.contracts import FactStatus, Target, TargetKind
from envrec.store import Store
from envrec.verify import TunnelClient, verify_server


def serve(handler_cls):
    srv = HTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def chat_handler(reply_fn):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers["content-length"])
            msgs = json.loads(self.rfile.read(n))["messages"]
            body = json.dumps({"choices": [{"message": {"role": "assistant", "content": reply_fn(msgs[-1]["content"])}}]}).encode()
            self.send_response(200)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    return H


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "s") as s:
        yield s


@pytest.fixture
def target(store):
    return store.put_target(Target(kind=TargetKind.net_host, ring=2, handle="box", authorized=True), from_config=True)


def test_an_echoing_server_passes_and_the_fact_becomes_verified(store, target):
    srv = serve(chat_handler(lambda c: f"[echo] {c}"))
    try:
        r = verify_server(TunnelClient(srv.server_port), store, target)
    finally:
        srv.shutdown()
    assert r.ok and r.rounds == 2
    f = store.current_fact(target.id, "host.model_server")
    assert f.status == FactStatus.verified and f.created_by.value == "verifier" and f.value == "running"
    assert store.read_raw(f.evidence[0].raw_ref)  # the nonces and replies are kept by hash


def test_a_server_with_one_canned_reply_fails_and_nothing_is_verified(store, target):
    srv = serve(chat_handler(lambda c: "Hello! I am running fine."))
    try:
        r = verify_server(TunnelClient(srv.server_port), store, target)
    finally:
        srv.shutdown()
    assert not r.ok and "nonce" in r.reason
    assert store.current_fact(target.id, "host.model_server") is None


def test_a_server_that_precomputes_one_answer_cannot_pass_because_nonces_are_fresh(store, target):
    seen = []
    srv = serve(chat_handler(lambda c: (seen.append(c), "token: deadbeefdeadbeef")[1]))
    try:
        r = verify_server(TunnelClient(srv.server_port), store, target)
    finally:
        srv.shutdown()
    assert not r.ok and len({m.split(": ")[1] for m in seen}) == len(seen)  # every request carried a different nonce


def test_nonces_differ_between_runs(store, target):
    seen = []
    srv = serve(chat_handler(lambda c: (seen.append(c), c)[1]))
    try:
        verify_server(TunnelClient(srv.server_port), store, target)
        verify_server(TunnelClient(srv.server_port), store, target)
    finally:
        srv.shutdown()
    assert len(set(seen)) == 4


def test_an_unreachable_server_fails_cleanly(store, target):
    import socket
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    r = verify_server(TunnelClient(port, timeout=1), store, target)
    assert not r.ok and "no usable answer" in r.reason and store.current_fact(target.id, "host.model_server") is None


def test_a_server_that_only_echoes_the_first_request_fails_the_second_round(store, target):
    calls = []
    srv = serve(chat_handler(lambda c: (calls.append(c), c if len(calls) == 1 else "cached")[1]))
    try:
        r = verify_server(TunnelClient(srv.server_port), store, target)
    finally:
        srv.shutdown()
    assert not r.ok and r.rounds == 2
