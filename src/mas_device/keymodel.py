"""Reference model of the key's firmware (SIMULATED: Python, not hardware). The C firmware in device/firmware must behave identically;
the tests in device/tests/test_approval.py are the shared contract, and test vectors from this model check the C build.

The key holds an Ed25519 key pair and a monotonic counter in its own storage. For each request it shows the summary and the next counter,
waits for one physical press after the request and inside the press window (measured on its own tick), commits the incremented counter to
storage, and only then releases the signature. A power cut after the commit loses that approval but never reuses a counter.
"""

from __future__ import annotations

import json
import math
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

from mas_device.approval import Class, KeyRequest, key_id_of, pack_body

_EVENTS = [0]  # one ordering for presses and requests, so "after the request" does not depend on clock resolution


def _next_event() -> int:
    _EVENTS[0] += 1
    return _EVENTS[0]


class KeyRefused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class Button:
    """The physical button. Its only public operation is a press. In this Python model that is a convention (name mangling can be
    bypassed); in the firmware the latch is set only by the button's interrupt and no opcode reaches it."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.__clock = clock
        self.__last: tuple[int, float] | None = None

    def press(self) -> None:
        self.__last = (_next_event(), self.__clock())

    def _take(self) -> tuple[int, float] | None:  # firmware-internal: read and clear the latched press
        last, self.__last = self.__last, None
        return last


@dataclass(frozen=True)
class RequestHandle:
    seq: int
    event: int
    at: float
    req: KeyRequest


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class KeyModel:
    PRESS_WINDOW_S = 30
    MIN_DWELL_S = 1  # a press counts only after the summary has been on screen this long (DECISIONS D-017)

    def __init__(self, folder: Path, private: Ed25519PrivateKey, counter: int, clock: Callable[[], float]) -> None:
        self._folder, self.__private, self._counter, self._clock = Path(folder), private, counter, clock
        self.public_key = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.key_id = key_id_of(self.public_key)
        self.button = Button(clock)
        self._pending: RequestHandle | None = None
        self._seq = 0

    @classmethod
    def create(cls, folder: Path, *, clock: Callable[[], float] = time.monotonic) -> "KeyModel":
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        priv = Ed25519PrivateKey.generate()
        _write_atomic(folder / "secret.bin", priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()))
        k = cls(folder, priv, 0, clock)
        k._save()
        return k

    @classmethod
    def load(cls, folder: Path, *, clock: Callable[[], float] = time.monotonic) -> "KeyModel":
        folder = Path(folder)
        priv = Ed25519PrivateKey.from_private_bytes((folder / "secret.bin").read_bytes())
        state = json.loads((folder / "state.json").read_text(encoding="utf-8"))
        counter = state.get("counter")
        if isinstance(counter, bool) or not isinstance(counter, int) or not 0 <= counter < 2**64 - 1:
            raise ValueError(f"stored counter {counter!r} is not an integer in [0, 2^64-1)")
        return cls(folder, priv, counter, clock)

    def _save(self) -> None:
        state = {"key_id": self.key_id.hex(), "public_key": self.public_key.hex(), "counter": self._counter}
        _write_atomic(self._folder / "state.json", json.dumps(state).encode())

    def request(self, req: KeyRequest) -> RequestHandle:
        fields = (req.session_id, req.nonce, req.action_hash, req.undo_hash, req.summary)
        if not all(type(f) in (bytes, bytearray) for f in fields):
            raise KeyRefused("malformed")  # bytes only: a str or list of the right length is not a field
        lens = tuple(len(f) for f in fields)
        if lens != (32, 16, 32, 32, 32):
            raise KeyRefused("malformed")
        if not all(0x20 <= c < 0x7F for c in req.summary):
            raise KeyRefused("summary_not_printable")
        if type(req.cls) is not int or type(req.press_window_s) is not int:
            raise KeyRefused("malformed")  # the firmware reads two bytes: only exact integers model them
        if req.cls not in {int(c) for c in Class} or not 1 <= req.press_window_s <= 255:
            raise KeyRefused("malformed")
        if self._counter >= 2**64 - 2:
            raise KeyRefused("counter_exhausted")  # no next counter to sign (the firmware answers nv_fault)
        self._seq += 1
        self.button._take()  # a press latched before this request never counts for it
        self._pending = RequestHandle(self._seq, _next_event(), self._clock(), req)
        return self._pending

    def display_text(self) -> str:
        if self._pending is None:
            return ""
        return f"{self._pending.req.summary.decode('ascii').rstrip()}  #{self._counter + 1}"

    def sign(self, handle: RequestHandle) -> bytes:
        p = self._pending
        if p is None or handle.seq != p.seq:
            raise KeyRefused("stale_request")
        press = self.button._take()
        if press is None or press[0] <= p.event:
            raise KeyRefused("no_press")
        if not (math.isfinite(press[1]) and math.isfinite(p.at)):
            raise KeyRefused("no_press")  # a broken tick never signs (the firmware's tick is an integer and cannot be NaN)
        if press[1] - p.at < self.MIN_DWELL_S:
            raise KeyRefused("no_press")  # too soon for anyone to have read the screen; the request stays pending
        self._pending = None  # one press, one request
        if press[1] - p.at > min(p.req.press_window_s, self.PRESS_WINDOW_S):
            raise KeyRefused("press_window")
        body = pack_body(self.key_id, self._counter + 1, p.req)  # built first: no counter is spent on a body that cannot exist
        self._counter += 1
        self._save()  # committed before the signature exists
        return self._release(body + self.__private.sign(body))

    def _release(self, token: bytes) -> bytes:
        return token

    def _secret_bytes_for_test(self) -> bytes:
        return self.__private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
