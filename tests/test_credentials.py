"""The node's credential module: the OS credential store through keyring, NO plaintext fallback, values never returned to a caller that only asks whether something is present. SIMULATED-LOCAL: an in-memory
backend stands for the OS store; the real Windows, macOS and Linux stores are UNTESTED here."""
from __future__ import annotations

import pytest

keyring = pytest.importorskip("keyring")
from keyring.backend import KeyringBackend  # noqa: E402
from keyring.backends import fail, null  # noqa: E402

from envrec_node import credentials as C  # noqa: E402

SENTINEL = "LLM_0_SENTINEL_value"


class Mem(KeyringBackend):
    priority = 1

    def __init__(self):
        super().__init__()
        self.d: dict[tuple[str, str], str] = {}

    def set_password(self, service, username, password):
        self.d[(service, username)] = password

    def get_password(self, service, username):
        return self.d.get((service, username))

    def delete_password(self, service, username):
        if (service, username) not in self.d:
            raise keyring.errors.PasswordDeleteError("absent")
        del self.d[(service, username)]


@pytest.fixture
def store():
    old = keyring.get_keyring()
    mem = Mem()
    keyring.set_keyring(mem)
    yield mem
    keyring.set_keyring(old)


def test_set_stores_in_the_os_store_and_presence_never_returns_the_value(store):
    C.set("MODEL_API_KEY", SENTINEL)
    assert store.d[(C.SERVICE, "MODEL_API_KEY")] == SENTINEL and C.has("MODEL_API_KEY") is True and C.has("OTHER_KEY") is False
    assert SENTINEL not in repr(C.present(["MODEL_API_KEY", "OTHER_KEY"], environ={})) and C.present(["MODEL_API_KEY", "OTHER_KEY"], environ={})["names"] == ["MODEL_API_KEY"]


def test_presence_counts_the_nodes_environment_too_by_name_only():
    out = C.present(["MODEL_API_KEY"], environ={"MODEL_API_KEY": SENTINEL})
    assert out["names"] == ["MODEL_API_KEY"] and SENTINEL not in repr(out)


def test_lookup_is_for_the_environment_run_only_store_first_then_none(store):
    assert C.lookup("MODEL_API_KEY") is None
    C.set("MODEL_API_KEY", SENTINEL)
    assert C.lookup("MODEL_API_KEY") == SENTINEL


@pytest.mark.parametrize("bad", ["", "lower", "A", "HAS SPACE", "X" * 80, "a-b", "../etc"])
def test_a_name_that_is_not_a_plain_variable_name_is_refused(store, bad):
    with pytest.raises(C.CredentialError) as e:
        C.set(bad, "v")
    assert e.value.code == "bad_credential_name"


@pytest.mark.parametrize("backend", [fail.Keyring(), null.Keyring()])
def test_with_no_real_store_every_write_is_refused_and_nothing_is_kept_anywhere(backend, tmp_path, monkeypatch):
    old = keyring.get_keyring()
    keyring.set_keyring(backend)
    monkeypatch.chdir(tmp_path)
    try:
        assert C.backend()["ok"] is False and C.backend()["reason"] == "credential_store_missing"
        for call in (lambda: C.set("MODEL_API_KEY", SENTINEL), lambda: C.stash("MODEL_API_KEY"), lambda: C.restore("MODEL_API_KEY")):
            with pytest.raises(C.CredentialError) as e:
                call()
            assert e.value.code == "credential_store_missing"
        assert C.lookup("MODEL_API_KEY") is None and list(tmp_path.iterdir()) == []  # no file, no fallback
    finally:
        keyring.set_keyring(old)


def test_remove_moves_the_value_aside_inside_the_store_and_restore_brings_it_back_without_the_value_leaving_the_module(store):
    C.set("MODEL_API_KEY", SENTINEL)
    out = C.stash("MODEL_API_KEY")
    assert out == {"name": "MODEL_API_KEY", "present": False} and C.has("MODEL_API_KEY") is False and C.lookup("MODEL_API_KEY") is None
    assert store.d[(C.SERVICE, "MODEL_API_KEY.removed")] == SENTINEL
    back = C.restore("MODEL_API_KEY")
    assert back == {"name": "MODEL_API_KEY", "present": True} and C.lookup("MODEL_API_KEY") == SENTINEL and (C.SERVICE, "MODEL_API_KEY.removed") not in store.d


def test_stash_and_restore_of_something_absent_are_typed_not_silent(store):
    with pytest.raises(C.CredentialError) as e:
        C.stash("MODEL_API_KEY")
    assert e.value.code == "credential_absent"
    with pytest.raises(C.CredentialError) as e2:
        C.restore("MODEL_API_KEY")
    assert e2.value.code == "credential_absent"


def test_a_store_that_fails_reports_the_kind_of_error_not_its_message(store, monkeypatch):
    def boom(*a):
        raise keyring.errors.PasswordSetError(f"cannot store {SENTINEL}")
    monkeypatch.setattr(store, "set_password", boom)
    with pytest.raises(C.CredentialError) as e:
        C.set("MODEL_API_KEY", SENTINEL)
    assert e.value.code == "credential_store_failed" and SENTINEL not in str(e.value) and SENTINEL not in repr(e.value)
