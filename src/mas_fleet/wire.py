"""JSON lines over one TCP connection: one object per line, at most MAX_LINE bytes, no duplicate keys, no NaN."""

from __future__ import annotations

import json
import socket

MAX_LINE = 8 << 20


class WireError(Exception):
    pass


def _no_dup(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            raise WireError(f"duplicate key {k!r}")
        d[k] = v
    return d


def _no_constant(name):
    raise WireError(f"{name} is not JSON")


def dumps(msg: dict) -> bytes:
    return json.dumps(msg, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii") + b"\n"


def loads(line: bytes) -> dict:
    if len(line) > MAX_LINE:
        raise WireError("line too long")
    try:
        msg = json.loads(line.decode("utf-8"), object_pairs_hook=_no_dup, parse_constant=_no_constant)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise WireError(f"not JSON ({type(exc).__name__})") from None
    if not isinstance(msg, dict):
        raise WireError("a message is an object")
    return msg


class Conn:
    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.r = sock.makefile("rb")

    def send(self, msg: dict) -> None:
        self.sock.sendall(dumps(msg))

    def recv(self) -> dict | None:
        line = self.r.readline(MAX_LINE + 1)
        if not line:
            return None
        if not line.endswith(b"\n"):
            raise WireError("line too long or cut")
        return loads(line)

    def close(self) -> None:
        try:
            self.r.close()
        finally:
            self.sock.close()
