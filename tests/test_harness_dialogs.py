"""Adding or removing an access from a chat is raised by the HARNESS as the operating system's own dialog, with no model action, and the hidden window for the key opens right after the approval. (Unpair, undo,
admission, a task's route, a board's decision and setup steps are NOT raised by the harness yet: they keep the intent_id in the text for `mas_request_approval`; see PROGRESS.) Found in the first real use: "connect the new computer to Meta's API" asked for approval and nothing appeared, because every flow
waited for the chat's model to call `mas_request_approval`. These tests do NOT play the owner: the fake operating system records what it was asked and HOLDS; nothing here calls `mas_request_approval`, and
everything is asserted before the fake lets go. SIMULATED-LOCAL (a real coordinator and node on loopback; the real dialogs are shown only in a supervised run)."""
from __future__ import annotations

import asyncio
import json
import threading

import pytest

from acceptance_world import SENTINEL, build, wait_job
from envrec_coordinator import harness as H
from test_acceptance_run import sc, text
from mas_world import events

CLAUDE = H.CHANNELS["claude-desktop"]


def run(coro):
    return asyncio.run(coro)


class Hold:
    """The operating system: it records the request, holds until the test lets go, then answers like a person would."""

    def __init__(self, result="approved", value=SENTINEL):
        self.result, self.value, self.calls, self.opened, self.release = result, value, [], threading.Event(), threading.Event()

    def confirm(self, title, message, **kw):
        self.calls.append(message)
        self.opened.set()
        self.release.wait(20)
        return {"result": self.result, "mechanism": "fake_os_dialog", "strong": True, "assurance": "strong"}

    def secret(self, title, message, **kw):
        self.calls.append(message)
        self.opened.set()
        self.release.wait(20)
        return {"result": "entered" if self.result == "approved" else self.result, "mechanism": "fake_hidden_window", "value": self.value if self.result == "approved" else None}


async def opened(h: Hold):
    assert await asyncio.to_thread(h.opened.wait, 10), "the harness never opened the dialog by itself"


def owner_world(c):
    """From the played-owner world to one where the harness opens the dialogs and the 'operating system' holds."""
    confirm, secret = Hold(), Hold()
    c.harness.native_confirm, c.harness.native_secret = confirm.confirm, secret.secret
    c.harness.raise_dialogs = True
    return confirm, secret


def no_request_approval(c):
    assert not [e for e in events(c, "confirmation_started") if e.payload.get("mechanism") != "os_dialog"]  # only the operating system's dialog was ever started


def test_add_access_the_harness_asks_for_the_approval_then_the_hidden_window_opens_right_after(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path, key=False)
        try:
            from envrec_node import credentials as C
            confirm, secret = owner_world(c)
            seen = []
            r = await host.tool("mas_add_access", node="lenovo", access="meta-model-api:api-key")
            seen.append(r)
            j = sc(r)["job_id"]
            await opened(confirm)  # NO mas_request_approval was called
            assert "MODEL_API_KEY" in confirm.calls[0] and secret.calls == [] and C.has("MODEL_API_KEY") is False  # the approval comes first; no window for the key yet
            confirm.release.set()
            await opened(secret)  # the hidden-field window opens right after the approval
            assert C.has("MODEL_API_KEY") is False
            secret.release.set()
            final = await wait_job(host, j, until=("done", "failed", "declined", "refused"))
            seen.append(final)
            assert sc(final)["state"] == "done" and C.has("MODEL_API_KEY") is True
            assert SENTINEL not in json.dumps(seen, default=str) and no_request_approval(c) is None
        finally:
            await teardown()
    run(go())


def test_add_access_declined_in_the_approval_never_opens_the_window_for_the_key(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path, key=False)
        try:
            from envrec_node import credentials as C
            confirm, secret = owner_world(c)
            confirm.result = "declined"
            j = sc(await host.tool("mas_add_access", node="lenovo", access="meta-model-api:api-key"))["job_id"]
            await opened(confirm)
            confirm.release.set()
            assert sc(await wait_job(host, j, until=("done", "failed", "declined", "refused")))["state"] == "declined"
            assert secret.calls == [] and C.has("MODEL_API_KEY") is False
        finally:
            await teardown()
    run(go())


def test_remove_access_the_harness_opens_the_dialog_and_the_key_stays_until_the_owner_answers(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            from envrec_node import credentials as C
            confirm, _ = owner_world(c)
            j = sc(await host.tool("mas_remove_access", node="lenovo", access="meta-model-api:api-key"))["job_id"]
            await opened(confirm)
            assert "MODEL_API_KEY" in confirm.calls[0] and C.has("MODEL_API_KEY") is True
            confirm.release.set()
            assert sc(await wait_job(host, j, until=("done", "failed", "declined", "refused")))["state"] == "done" and C.has("MODEL_API_KEY") is False
        finally:
            await teardown()
    run(go())












@pytest.mark.parametrize("flow", ["add_access", "unpair"])
def test_when_no_dialog_can_be_raised_the_chat_is_still_shown_the_id_to_use(tmp_path, monkeypatch, flow):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path, key=False)
        try:
            owner_world(c)
            monkeypatch.setattr(H.osconfirm, "available", lambda *a, **k: {"available": False, "mechanism": None})
            if flow == "unpair":
                r = await host.tool("mas_unpair", machine="lenovo")
                assert "intent_id for mas_request_approval:" in text(r)
            else:
                j = sc(await host.tool("mas_add_access", node="lenovo", access="meta-model-api:api-key"))["job_id"]
                for _ in range(100):
                    if c.services.jobs[j].intent_id:
                        break
                    await asyncio.sleep(0.1)
                assert c.services.jobs[j].intent_id in text(await host.tool("mas_job", job_id=j))
            assert [e.payload["reason"] for e in events(c, "confirmation_dialog_not_raised")] == (["os_dialog_unavailable"] if flow == "add_access" else [])
        finally:
            await teardown()
    run(go())
