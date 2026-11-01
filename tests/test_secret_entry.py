"""Secret entry end to end: the key is typed into the operating system's hidden window, travels once, sealed, in one write task to the node, and lands in the node's credential store. SIMULATED-LOCAL: a real
coordinator and a real node agent over loopback TLS, a fake hidden window played by the test, an in-memory credential store. The real OS windows and stores are UNTESTED here."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

keyring = pytest.importorskip("keyring")
from keyring.backend import KeyringBackend  # noqa: E402

from envrec_coordinator import harness as H  # noqa: E402
from envrec_coordinator.harness import CHANNELS, Refused  # noqa: E402
from envrec_net import task as T  # noqa: E402
from envrec_node import credentials as C  # noqa: E402
from mas_world import coordinator  # noqa: E402
from test_mas_tasks import agent_up, down, finished  # noqa: E402

SENTINEL = "LLM_0_SENTINEL_typed_in_the_window"
ACCESS, NAME = "meta-model-api:api-key", "MODEL_API_KEY"
CLAUDE, OWUI = CHANNELS["claude-desktop"], CHANNELS["openwebui"]


class Mem(KeyringBackend):
    priority = 1

    def __init__(self):
        super().__init__()
        self.d = {}

    def set_password(self, s, u, p):
        self.d[(s, u)] = p

    def get_password(self, s, u):
        return self.d.get((s, u))

    def delete_password(self, s, u):
        del self.d[(s, u)]


@pytest.fixture
def store():
    old = keyring.get_keyring()
    mem = Mem()
    keyring.set_keyring(mem)
    yield mem
    keyring.set_keyring(old)


@pytest.fixture(autouse=True)
def os_dialog_exists(monkeypatch):
    monkeypatch.setattr(H.osconfirm, "available", lambda *a, **k: {"available": True, "mechanism": "fake_dialog"})


class Window:
    def __init__(self, result="entered", value=SENTINEL):
        self.result, self.value, self.calls = result, value, []

    def __call__(self, title, message, **kw):
        self.calls.append((title, message))
        return {"result": self.result, "mechanism": "fake_hidden_window", "value": self.value if self.result == "entered" else None}


class Confirm:
    def __init__(self, result="approved"):
        self.result, self.calls = result, []

    def __call__(self, title, message, **kw):
        self.calls.append((title, message))
        return {"result": self.result, "mechanism": "fake_dialog", "strong": None, "assurance": "weak"}


async def settle(pred, timeout=5.0):
    for _ in range(int(timeout / 0.02)):
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("never happened")


def plan(c, nid, action):
    h = T.credential_plan_hash(nid, ACCESS, NAME, action)
    c.log.add("credential_plan", {"plan_hash": h, "node": nid, "access": ACCESS, "name": NAME, "action": action})
    return h


def everything_under(*roots: Path) -> str:
    out = []
    for root in roots:
        for p in Path(root).rglob("*"):
            if p.is_file():
                try:
                    out.append(p.read_bytes().decode("utf-8", "ignore"))
                except OSError:
                    pass
    return "\n".join(out)


async def enter_and_send(c, nid, window):
    c.harness.native_secret = window
    h = plan(c, nid, "set")
    p = c.harness.request_secret_entry(None, summary="Store MODEL_API_KEY on the node", plan_hash=h, command="store it in the node's credential store", undo="remove this access", credential=NAME)
    b = c.harness.begin_confirmation(CLAUDE, p.intent.id, "os_dialog")
    await settle(lambda: c.harness.ticket(CLAUDE, b["ticket"])["state"] != "waiting")
    return h, p, b


def test_the_typed_key_reaches_the_nodes_store_once_and_appears_nowhere_else(tmp_path, store):
    async def go():
        async with coordinator(tmp_path / "c") as c:
            agent, tsk, nid = await agent_up(c, tmp_path)
            try:
                sent = []
                real = c.tasks.sessions(nid).send

                async def spy(t, p):
                    sent.append((t, p))
                    await real(t, p)
                c.tasks.sessions(nid).send = spy
                win = Window()
                h, p, b = await enter_and_send(c, nid, win)
                assert c.harness.ticket(CLAUDE, b["ticket"])["state"] == "approved" and SENTINEL not in json.dumps(p.intent.to_dict()) and SENTINEL not in json.dumps(b)
                value = c.harness.take_secret(p.intent.id)
                assert value == SENTINEL and c.harness.take_secret(p.intent.id) is None  # once
                rec = c.tasks.start_credential_step(nid, NAME, "set", h, value)
                await finished(rec)
                assert rec.status == "done" and rec.result["present"] is True and SENTINEL not in json.dumps(rec.result)
                assert store.d[(C.SERVICE, NAME)] == SENTINEL
                frames = [x for x in sent if x[0] == "task" and SENTINEL in json.dumps(x[1])]
                assert len(frames) == 1 and frames[0][1]["task_type"] == "credential_step"  # the one sealed task is the only place the value travels
                assert "hidden" not in win.calls[0][1] and SENTINEL not in win.calls[0][1]
                logs = [json.dumps(e.payload) for e in c.log.events()]
                assert not any(SENTINEL in l for l in logs)
            finally:
                await down(agent, tsk)
            assert SENTINEL not in everything_under(tmp_path)
    asyncio.run(go())


def test_a_window_the_owner_cancels_stores_nothing_and_a_missing_window_is_not_an_approval(tmp_path, store):
    async def go():
        async with coordinator(tmp_path / "c") as c:
            agent, tsk, nid = await agent_up(c, tmp_path)
            try:
                for result, state in (("declined", "declined"), ("unavailable", "unavailable"), ("timeout", "timeout")):
                    h, p, b = await enter_and_send(c, nid, Window(result))
                    assert c.harness.ticket(CLAUDE, b["ticket"])["state"] == state and c.harness.take_secret(p.intent.id) is None
                    rec = c.tasks.start_credential_step(nid, NAME, "set", h, SENTINEL)  # even if a caller had a value
                    assert rec.status == "rejected" and rec.reason == "no_owner_approval"
                assert (C.SERVICE, NAME) not in store.d
            finally:
                await down(agent, tsk)
    asyncio.run(go())


def test_a_secret_can_only_be_answered_by_the_os_window_never_by_a_host_form_or_a_chat_dialog(tmp_path, store):
    async def go():
        async with coordinator(tmp_path / "c") as c:
            c.harness.caps["claude-desktop"] = {"elicitation": True}
            h = plan(c, "n1", "set")
            p = c.harness.request_secret_entry(None, summary="s", plan_hash=h, command="c", undo="u", credential=NAME)
            for mech in ("host_elicitation", "panel_click", "host_dialog", "cli_typed"):
                with pytest.raises(Refused) as e:
                    c.harness.begin_confirmation(CLAUDE, p.intent.id, mech)
                assert e.value.reason in ("secret_needs_os_dialog", "mechanism_unavailable")
            with pytest.raises(Refused):
                c.harness.panel_decide(p.intent.id, h, True)  # the panel's click cannot approve a secret either
            assert c.harness.take_secret(p.intent.id) is None
    asyncio.run(go())


def test_a_credential_task_is_refused_without_the_owners_confirmation_for_exactly_that_plan(tmp_path, store):
    async def go():
        async with coordinator(tmp_path / "c") as c:
            agent, tsk, nid = await agent_up(c, tmp_path)
            try:
                other = plan(c, nid, "remove")  # a plan nobody approved
                assert c.tasks.start_credential_step(nid, NAME, "remove", other).reason == "no_owner_approval"
                h, p, b = await enter_and_send(c, nid, Window())
                assert c.tasks.start_credential_step(nid, NAME, "set", other, SENTINEL).reason == "no_owner_approval"  # approval of "set" is not approval of "remove"
                assert c.tasks.start_credential_step(nid, "OTHER_KEY", "set", h, SENTINEL).reason == "no_owner_approval"
                assert c.tasks.start_credential_step(nid, NAME, "set", "sha256:" + "0" * 64, SENTINEL).reason == "no_owner_approval"
                c.log.add("credential_plan", {"plan_hash": "sha256:" + "f" * 64, "node": nid, "access": ACCESS, "name": NAME, "action": "set"})  # a forged plan record: its hash is not its content's
                assert c.tasks.start_credential_step(nid, NAME, "set", "sha256:" + "f" * 64, SENTINEL).reason == "no_owner_approval"
                assert c.tasks.start(nid, "credential_step", {"name": NAME, "action": "set", "plan_hash": h, "value": SENTINEL}).reason == "write_refused"  # the generic door refuses a write
            finally:
                await down(agent, tsk)
    asyncio.run(go())


def test_removing_an_access_sets_the_key_aside_with_an_approval_and_the_undo_brings_it_back(tmp_path, store):
    async def go():
        async with coordinator(tmp_path / "c") as c:
            agent, tsk, nid = await agent_up(c, tmp_path)
            try:
                C.set(NAME, SENTINEL)
                c.harness.native_confirm = Confirm()
                results = {}
                for action in ("remove", "restore"):
                    h = plan(c, nid, action)
                    p = c.harness.request_confirmation(None, summary=f"{action} {NAME}", plan_hash=h, command="move it aside in the store", undo="restore it")
                    b = c.harness.begin_confirmation(CLAUDE, p.intent.id, "os_dialog")
                    await settle(lambda: c.harness.ticket(CLAUDE, b["ticket"])["state"] != "waiting")
                    rec = c.tasks.start_credential_step(nid, NAME, action, h)
                    await finished(rec)
                    results[action] = rec
                    assert rec.status == "done" and SENTINEL not in json.dumps(rec.result)
                    if action == "remove":
                        assert (C.SERVICE, NAME) not in store.d and store.d[(C.SERVICE, NAME + ".removed")] == SENTINEL
                assert store.d[(C.SERVICE, NAME)] == SENTINEL and (C.SERVICE, NAME + ".removed") not in store.d
            finally:
                await down(agent, tsk)
    asyncio.run(go())


def test_the_node_refuses_a_credential_task_whose_token_names_another_credential_action_or_plan():
    import pytest as _p
    params = T.validate_params("credential_step", {"name": NAME, "action": "set", "plan_hash": "sha256:" + "a" * 64, "value": "v"})
    good = T.credential_scope("sha256:" + "a" * 64, NAME, "set", "evt1")
    T.check_credential_scope({"scope": good}, params)
    for bad, why in (({**good, "credential": "OTHER_KEY"}, "bad_scope"), ({**good, "action": "remove"}, "bad_scope"), ({**good, "plan_hash": "sha256:" + "b" * 64}, "plan_altered"),
                     ({**good, "approval": {**good["approval"], "actor": "GUARD"}}, "no_owner_approval"), ({k: v for k, v in good.items() if k != "approval"}, "no_owner_approval"),
                     ({**good, "approval": {**good["approval"], "name": "OTHER_KEY"}}, "plan_altered")):
        with _p.raises(T.TokenRejected) as e:
            T.check_credential_scope({"scope": bad}, params)
        assert e.value.reason == why, (bad, why)


def test_a_value_only_belongs_to_set_and_the_validation_error_never_echoes_it():
    for body in ({"action": "set"}, {"action": "remove", "value": SENTINEL}, {"action": "restore", "value": SENTINEL}):
        with pytest.raises(ValueError) as e:
            T.validate_params("credential_step", {"name": NAME, "plan_hash": "sha256:" + "a" * 64, **body})
        assert str(e.value) == "bad_parameters" and SENTINEL not in str(e.value)
    with pytest.raises(ValueError) as e2:
        T.validate_params("credential_step", {"name": "lowercase", "action": "set", "plan_hash": "sha256:" + "a" * 64, "value": SENTINEL})
    assert SENTINEL not in str(e2.value) and SENTINEL not in repr(T.CredentialStepParams(name=NAME, action="set", plan_hash="sha256:" + "a" * 64, value=SENTINEL))


def test_with_no_credential_store_the_node_refuses_and_nothing_is_kept(tmp_path):
    from keyring.backends import fail
    old = keyring.get_keyring()
    keyring.set_keyring(fail.Keyring())

    async def go():
        async with coordinator(tmp_path / "c") as c:
            agent, tsk, nid = await agent_up(c, tmp_path)
            try:
                h, p, b = await enter_and_send(c, nid, Window())
                rec = c.tasks.start_credential_step(nid, NAME, "set", h, c.harness.take_secret(p.intent.id))
                await finished(rec)
                assert rec.status == "failed" and rec.reason == "credential_store_missing"
            finally:
                await down(agent, tsk)
        assert SENTINEL not in everything_under(tmp_path)
    try:
        asyncio.run(go())
    finally:
        keyring.set_keyring(old)
