"""A GGUF header reader: the metadata key/values and the tensor table of a model file, read WITHOUT downloading the weights.

The source is anything with `size` and `read_at(offset, n)`: a local file, or `HttpRange` (HTTP range requests to a pinned URL). Nothing here writes to disk. The result is plain facts, every one of them
"verified-by-code" (read from the file's own header); `model-catalog-rules` says what they mean.

Format (GGUF v2 and v3, little endian): magic "GGUF", u32 version, u64 tensor_count, u64 kv_count, the key/values, then one record per tensor (name, dims, type, offset). The parameter count is the
sum of the tensors' element counts, not a number from a file name."""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from typing import Any, Protocol

MAGIC = b"GGUF"
SUPPORTED_VERSIONS = (2, 3)
KEEP_ARRAY_MAX = 64  # arrays longer than this (the tokenizer vocabulary, merges, scores) are counted, not kept
KEEP_STR_MAX = 4096  # a longer string (a chat template can be) is kept up to this many characters; its full length is recorded
_SCALARS = {0: ("<B", 1), 1: ("<b", 1), 2: ("<H", 2), 3: ("<h", 2), 4: ("<I", 4), 5: ("<i", 4), 6: ("<f", 4), 7: ("<?", 1), 10: ("<Q", 8), 11: ("<q", 8), 12: ("<d", 8)}
_STRING, _ARRAY = 8, 9


class GgufError(ValueError):
    """The bytes are not a GGUF header this reader understands. The message says where."""


class ByteSource(Protocol):
    size: int

    def read_at(self, offset: int, n: int) -> bytes: ...


class BytesSource:
    def __init__(self, data: bytes) -> None:
        self._d, self.size = data, len(data)

    def read_at(self, offset: int, n: int) -> bytes:
        return self._d[offset:offset + n]


class FileSource:
    def __init__(self, path: str) -> None:
        import os
        self._path, self.size = path, os.path.getsize(path)

    def read_at(self, offset: int, n: int) -> bytes:
        with open(self._path, "rb") as fh:
            fh.seek(offset)
            return fh.read(n)


class _Reader:
    """Sequential reads over a ByteSource with a read-ahead buffer, so a header costs a few requests, not thousands."""

    def __init__(self, src: ByteSource, chunk: int = 4 << 20) -> None:
        self.src, self.pos, self.chunk, self._buf, self._start = src, 0, chunk, b"", 0
        self.fetched = 0

    def read(self, n: int) -> bytes:
        if n < 0 or self.pos + n > self.src.size:
            raise GgufError(f"the header reads past the end of the file (offset {self.pos}, {n} bytes, file {self.src.size})")
        end = self.pos + n
        if not (self._start <= self.pos and end <= self._start + len(self._buf)):
            want = max(n, self.chunk)
            self._buf = self.src.read_at(self.pos, min(want, self.src.size - self.pos))
            self._start = self.pos
            self.fetched += len(self._buf)
            if len(self._buf) < n:
                raise GgufError(f"short read at offset {self.pos}: wanted {n}, got {len(self._buf)}")
        out = self._buf[self.pos - self._start:end - self._start]
        self.pos = end
        return out

    def skip(self, n: int) -> None:
        if n < 0 or self.pos + n > self.src.size:
            raise GgufError("a skip runs past the end of the file")
        self.pos += n

    def u32(self) -> int:
        return struct.unpack("<I", self.read(4))[0]

    def u64(self) -> int:
        return struct.unpack("<Q", self.read(8))[0]

    def string(self, keep: int | None = None) -> tuple[str, int]:
        n = self.u64()
        if n > 1 << 28:
            raise GgufError(f"an implausible string length ({n}) at offset {self.pos}")
        if keep is None or n <= keep:
            return self.read(n).decode("utf-8", "replace"), n
        head = self.read(keep).decode("utf-8", "replace")
        self.skip(n - keep)
        return head, n


@dataclass
class Tensor:
    name: str
    dims: tuple[int, ...]
    ggml_type: int
    offset: int

    @property
    def elements(self) -> int:
        return math.prod(self.dims) if self.dims else 1


@dataclass
class Header:
    version: int
    metadata: dict[str, Any]
    tensors: list[Tensor]
    string_lengths: dict[str, int] = field(default_factory=dict)  # full length of a string that was cut at KEEP_STR_MAX
    array_lengths: dict[str, int] = field(default_factory=dict)  # element count of an array that was not kept
    bytes_read: int = 0
    file_size: int = 0

    @property
    def architecture(self) -> str | None:
        return self.metadata.get("general.architecture")

    def param_count(self) -> int:
        return sum(t.elements for t in self.tensors)


def _value(r: _Reader, vtype: int, key: str, hdr_arrays: dict[str, int], hdr_strings: dict[str, int]) -> Any:
    if vtype in _SCALARS:
        fmt, size = _SCALARS[vtype]
        return struct.unpack(fmt, r.read(size))[0]
    if vtype == _STRING:
        s, n = r.string(KEEP_STR_MAX)
        if n > KEEP_STR_MAX:
            hdr_strings[key] = n
        return s
    if vtype == _ARRAY:
        etype = r.u32()
        count = r.u64()
        if etype == _ARRAY:
            raise GgufError(f"nested arrays are not read (key {key!r})")
        if etype in _SCALARS:
            size = _SCALARS[etype][1]
            if count <= KEEP_ARRAY_MAX:
                return [_value_scalar(r, etype) for _ in range(count)]
            r.skip(count * size)  # a numeric array too long to keep: counted, never fetched
            hdr_arrays[key] = count
            return None
        if etype == _STRING:
            keep = count <= KEEP_ARRAY_MAX
            items = []
            for _ in range(count):  # strings have to be walked: each length says where the next one starts
                s, _n = r.string(KEEP_STR_MAX if keep else 0)
                if keep:
                    items.append(s)
            if keep:
                return items
            hdr_arrays[key] = count
            return None
        raise GgufError(f"unknown array element type {etype} (key {key!r})")
    raise GgufError(f"unknown value type {vtype} (key {key!r})")


def _value_scalar(r: _Reader, vtype: int) -> Any:
    fmt, size = _SCALARS[vtype]
    return struct.unpack(fmt, r.read(size))[0]


def read_header(src: ByteSource, *, chunk: int = 4 << 20) -> Header:
    r = _Reader(src, chunk)
    if r.read(4) != MAGIC:
        raise GgufError("not a GGUF file (the first four bytes are not 'GGUF')")
    version = r.u32()
    if version not in SUPPORTED_VERSIONS:
        raise GgufError(f"GGUF version {version} is not read here (supported: {SUPPORTED_VERSIONS})")
    n_tensors, n_kv = r.u64(), r.u64()
    if n_kv > 100_000 or n_tensors > 1_000_000:
        raise GgufError(f"an implausible header (tensors {n_tensors}, key/values {n_kv})")
    md: dict[str, Any] = {}
    arrays: dict[str, int] = {}
    strings: dict[str, int] = {}
    for _ in range(n_kv):
        key, _n = r.string()
        vtype = r.u32()
        md[key] = _value(r, vtype, key, arrays, strings)
        if md[key] is None and key in arrays:
            del md[key]
    tensors = []
    for _ in range(n_tensors):
        name, _n = r.string()
        nd = r.u32()
        if nd > 8:
            raise GgufError(f"a tensor with {nd} dimensions (name {name!r})")
        dims = tuple(r.u64() for _ in range(nd))
        gtype = r.u32()
        off = r.u64()
        tensors.append(Tensor(name, dims, gtype, off))
    return Header(version, md, tensors, strings, arrays, r.fetched, src.size)


# ---- facts: what the rules read --------------------------------------------------------------------------------------------------------------------------------------------------------------

def _arch_key(arch: str | None, rest: str) -> str:
    return f"{arch}.{rest}" if arch else rest


def facts_from_header(h: Header, template_markers: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """Flat `gguf.*` facts. Architecture-prefixed keys are also given as `gguf.arch.<rest>` (so a rule says `gguf.arch.block_count`, whatever the architecture is called). Long arrays and
    strings are summarized, never copied. Every value is verified-by-code (read from the header)."""
    arch = h.architecture
    out: dict[str, Any] = {}
    for k, v in h.metadata.items():
        if k == "tokenizer.chat_template":
            continue  # summarized below
        out[f"gguf.{k}"] = v
        if arch and k.startswith(arch + "."):
            out[f"gguf.arch.{k[len(arch) + 1:]}"] = v
    for k, n in h.array_lengths.items():
        out[f"gguf.len.{k}"] = n
    tmpl = h.metadata.get("tokenizer.chat_template")
    out["gguf.has_chat_template"] = bool(tmpl) if isinstance(tmpl, str) else False
    if isinstance(tmpl, str) and tmpl:
        out["gguf.chat_template_chars"] = h.string_lengths.get("tokenizer.chat_template", len(tmpl))
        for flag, needles in (template_markers or {}).items():
            out[f"gguf.template.{flag}"] = any(nd in tmpl for nd in needles)
    out["gguf.tensor_count"] = len(h.tensors)
    out["gguf.has_lora_tensors"] = any(t.name.endswith((".lora_a", ".lora_b")) for t in h.tensors)
    out["gguf.param_count"] = h.param_count()
    ec, eu = out.get("gguf.arch.expert_count"), out.get("gguf.arch.expert_used_count")
    if isinstance(ec, int) and ec > 1 and isinstance(eu, int) and 0 < eu <= ec:
        expert = sum(t.elements for t in h.tensors if "_exps" in t.name)  # the stacked per-expert tensors
        out["gguf.active_param_count"] = (h.param_count() - expert) + round(expert * eu / ec)
    if "gguf.arch.attention.head_count_kv" in out and isinstance(out["gguf.arch.attention.head_count_kv"], list):  # per-layer KV heads (hybrid models): keep the largest and the count of layers that have any
        arr = out["gguf.arch.attention.head_count_kv"]
        out["gguf.arch.attention.head_count_kv_layers"] = sum(1 for x in arr if x)
        out["gguf.arch.attention.head_count_kv"] = max(arr) if arr else 0
    out["gguf.file_size"] = h.file_size
    out["gguf.header_bytes_read"] = h.bytes_read
    return out
