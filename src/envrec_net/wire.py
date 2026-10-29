"""Framing: a 4-byte big-endian length, then that many bytes of UTF-8 JSON. Small on purpose (a frame over the limit closes the connection)."""

from __future__ import annotations

import asyncio
import json
import struct

MAX_FRAME = 64 * 1024


class FrameError(Exception):
    pass


async def read_frame(reader: asyncio.StreamReader, *, timeout: float | None = None) -> dict:
    try:
        head = await asyncio.wait_for(reader.readexactly(4), timeout)
        (n,) = struct.unpack(">I", head)
        if n == 0 or n > MAX_FRAME:
            raise FrameError(f"frame of {n} bytes refused")
        body = await asyncio.wait_for(reader.readexactly(n), timeout)
    except asyncio.IncompleteReadError:
        raise FrameError("the peer closed the connection") from None
    except TimeoutError:
        raise FrameError("timed out waiting for the peer") from None
    try:
        obj = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise FrameError("the frame is not JSON") from None
    if not isinstance(obj, dict):
        raise FrameError("the frame is not an object")
    return obj


async def write_frame(writer: asyncio.StreamWriter, obj: dict) -> None:
    body = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(body) > MAX_FRAME:
        raise FrameError("frame too large to send")
    writer.write(struct.pack(">I", len(body)) + body)
    await writer.drain()
