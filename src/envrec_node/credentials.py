"""The node's credential store: the operating system's own (Windows Credential Manager, macOS Keychain, Linux Secret Service) through `keyring`.

Rules this module enforces: there is NO plaintext fallback (with no real store every write is refused with `credential_store_missing`; nothing is written to a file, a log or the environment); a value
leaves this module only through `lookup`, which is called for the process of one environment run and nowhere else; everything else answers presence by NAME. A name is a plain variable name."""

from __future__ import annotations

import re
from collections.abc import Mapping

SERVICE = "multi-auto-setup"
NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
ASIDE = ".removed"  # where `stash` keeps a value inside the same store, so a removal can be undone without the value ever leaving the module


class CredentialError(Exception):
    """A typed refusal: `code` is a key of the node's rejections table; the text never holds a value."""

    def __init__(self, code: str, text: str = "") -> None:
        super().__init__(text or code)
        self.code = code


def _kr():
    import keyring
    return keyring


def backend() -> dict:
    """What the store is, without touching any secret: {"ok", "backend", "reason"}. A store is real when keyring resolved to something other than its fail or null backend."""
    try:
        kr = _kr().get_keyring()
    except Exception:  # noqa: BLE001 - keyring itself could not be imported or initialised: the same answer
        return {"ok": False, "backend": None, "reason": "credential_store_missing"}
    name = f"{type(kr).__module__}.{type(kr).__name__}"
    bad = name.startswith(("keyring.backends.fail.", "keyring.backends.null.")) or getattr(kr, "priority", 1) <= 0
    return {"ok": not bad, "backend": name, "reason": "credential_store_missing" if bad else None}


def _need() -> None:
    if not backend()["ok"]:
        raise CredentialError("credential_store_missing", "this computer has no credential store to keep the key in")


def _check(name: str) -> str:
    if not isinstance(name, str) or not NAME.match(name):
        raise CredentialError("bad_credential_name", "a credential name is a plain variable name such as MODEL_API_KEY")
    return name


def _wrap(call):
    try:
        return call()
    except CredentialError:
        raise
    except Exception as exc:  # noqa: BLE001 - the store's message could hold anything: only the kind is reported
        raise CredentialError("credential_store_failed", type(exc).__name__) from None


def set(name: str, value: str) -> dict:  # noqa: A001 - the module's verb
    _check(name)
    _need()
    if not isinstance(value, str) or not value:
        raise CredentialError("bad_credential_value", "a credential value is a non-empty string")
    _wrap(lambda: _kr().set_password(SERVICE, name, value))
    return {"name": name, "present": True}


def lookup(name: str) -> str | None:
    """The value, for the process of one environment run; None when absent or when there is no real store (the caller then falls back to the node's environment, never to a file)."""
    if not isinstance(name, str) or not NAME.match(name) or not backend()["ok"]:
        return None
    try:
        return _kr().get_password(SERVICE, name)
    except Exception:  # noqa: BLE001
        return None


def has(name: str) -> bool:
    return lookup(name) is not None


def present(names: list[str], environ: Mapping[str, str] | None = None) -> dict:
    """Which of these NAMES exist, in the store or in the environment. Never a value."""
    if environ is None:
        import os
        environ = os.environ
    return {"names": [n for n in names if isinstance(n, str) and NAME.match(n) and (has(n) or n in environ)], "backend": backend()["backend"], "store_ok": backend()["ok"]}


def stash(name: str) -> dict:
    """Remove an access in a way that can be undone: the value moves, inside the store, to `<NAME>.removed`."""
    _check(name)
    _need()
    kr = _kr()
    value = _wrap(lambda: kr.get_password(SERVICE, name))
    if value is None:
        raise CredentialError("credential_absent", "there is nothing stored under that name")
    _wrap(lambda: kr.set_password(SERVICE, name + ASIDE, value))
    _wrap(lambda: kr.delete_password(SERVICE, name))
    return {"name": name, "present": False}


def restore(name: str) -> dict:
    _check(name)
    _need()
    kr = _kr()
    value = _wrap(lambda: kr.get_password(SERVICE, name + ASIDE))
    if value is None:
        raise CredentialError("credential_absent", "nothing was set aside under that name")
    _wrap(lambda: kr.set_password(SERVICE, name, value))
    _wrap(lambda: kr.delete_password(SERVICE, name + ASIDE))
    return {"name": name, "present": True}


def apply(action: str, name: str, value: str | None = None) -> dict:
    """Do one approved change and report it WITHOUT the value: {"name", "present", "backend"}."""
    out = {"set": lambda: set(name, value or ""), "remove": lambda: stash(name), "restore": lambda: restore(name)}[action]()
    return {**out, "backend": backend()["backend"]}
