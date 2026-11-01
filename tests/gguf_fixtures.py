"""Build tiny GGUF headers in memory for the tests: a model's metadata and tensor table, no weights."""

from __future__ import annotations

import struct

U8, I8, U16, I16, U32, I32, F32, BOOL, STR, ARR, U64, I64, F64 = range(13)


def _s(x: str) -> bytes:
    b = x.encode()
    return struct.pack("<Q", len(b)) + b


def kv(key: str, vtype: int, value, etype: int | None = None) -> bytes:
    out = _s(key) + struct.pack("<I", vtype)
    if vtype == STR:
        return out + _s(value)
    if vtype == ARR:
        out += struct.pack("<IQ", etype, len(value))
        for v in value:
            out += _s(v) if etype == STR else struct.pack({U32: "<I", I32: "<i", F32: "<f", U8: "<B"}[etype], v)
        return out
    return out + struct.pack({U8: "<B", I8: "<b", U16: "<H", I16: "<h", U32: "<I", I32: "<i", F32: "<f", BOOL: "<?", U64: "<Q", I64: "<q", F64: "<d"}[vtype], value)


def gguf(kvs: list[bytes], tensors: list[tuple[str, tuple[int, ...]]] | None = None, *, version: int = 3, payload: int = 64) -> bytes:
    tensors = tensors or []
    head = b"GGUF" + struct.pack("<IQQ", version, len(tensors), len(kvs)) + b"".join(kvs)
    for name, dims in tensors:
        head += _s(name) + struct.pack("<I", len(dims)) + b"".join(struct.pack("<Q", d) for d in dims) + struct.pack("<IQ", 0, 0)
    return head + b"\0" * payload  # a stand-in for the weights: the reader never looks at it


def model(arch: str = "qwen3", *, layers: int = 4, heads: int = 8, kv_heads: int = 2, emb: int = 64, vocab: int = 100, experts: tuple[int, int] | None = None, **extra) -> bytes:
    kvs = [kv("general.architecture", STR, arch), kv("general.type", STR, extra.pop("gtype", "model")), kv(f"{arch}.block_count", U32, layers), kv(f"{arch}.attention.head_count", U32, heads),
           kv(f"{arch}.attention.head_count_kv", U32, kv_heads), kv(f"{arch}.embedding_length", U32, emb), kv(f"{arch}.context_length", U32, 4096)]
    if experts:
        kvs += [kv(f"{arch}.expert_count", U32, experts[0]), kv(f"{arch}.expert_used_count", U32, experts[1])]
    if extra.pop("template", True):
        kvs.append(kv("tokenizer.chat_template", STR, extra.pop("template_text", "{% for m in messages %}{{ m }}{% endfor %}")))
    tensors = [("token_embd.weight", (emb, vocab))] + [(f"blk.{i}.attn_q.weight", (emb, emb)) for i in range(layers)]
    if experts:
        tensors += [(f"blk.{i}.ffn_up_exps.weight", (emb, emb, experts[0])) for i in range(layers)]
    return gguf(kvs, tensors)
