"""The GGUF header reader: metadata and the tensor table read without the weights, from bytes, a file or HTTP range requests."""
from __future__ import annotations

import httpx
import pytest

from envrec.catalog import gguf as G
from envrec.catalog.hub import Hub, HttpRange, HubError
from gguf_fixtures import ARR, F32, STR, U32, U64, gguf, kv, model


def test_metadata_and_the_parameter_count_come_from_the_header_not_from_a_name():
    h = G.read_header(G.BytesSource(model("qwen2", layers=2, emb=32, vocab=50)))
    assert h.architecture == "qwen2" and h.metadata["qwen2.block_count"] == 2 and h.version == 3
    assert h.param_count() == 32 * 50 + 2 * 32 * 32  # the embedding plus two attention matrices: the sum of the tensors' elements
    f = G.facts_from_header(h, {"thinking": ["<think>"]})
    assert f["gguf.param_count"] == 3_648 and f["gguf.arch.block_count"] == 2 and f["gguf.has_chat_template"] is True and f["gguf.template.thinking"] is False
    assert f["gguf.arch.attention.head_count_kv"] == 2 and f["gguf.tensor_count"] == 3 and f["gguf.has_lora_tensors"] is False


def test_a_thinking_template_is_noticed_from_the_marker_the_rules_name():
    f = G.facts_from_header(G.read_header(G.BytesSource(model(template_text="{% if t %}<think>{% endif %}"))), {"thinking": ["<think>"]})
    assert f["gguf.template.thinking"] is True


def test_active_parameters_of_a_mixture_of_experts_count_only_the_experts_that_run():
    f = G.facts_from_header(G.read_header(G.BytesSource(model("qwen3moe", layers=2, emb=16, vocab=10, experts=(8, 2)))))
    expert_total = 2 * 16 * 16 * 8
    other = 16 * 10 + 2 * 16 * 16
    assert f["gguf.param_count"] == other + expert_total and f["gguf.active_param_count"] == other + expert_total * 2 // 8


def test_a_long_array_is_counted_and_never_kept_and_a_long_string_is_cut_but_its_length_is_known():
    vocab = [f"t{i}" for i in range(500)]
    data = gguf([kv("general.architecture", STR, "llama"), kv("tokenizer.ggml.tokens", ARR, vocab, STR), kv("tokenizer.ggml.scores", ARR, [0.5] * 300, F32),
                 kv("tokenizer.chat_template", STR, "x" * 10_000)], [("w", (4, 4))])
    h = G.read_header(G.BytesSource(data))
    assert "tokenizer.ggml.tokens" not in h.metadata and h.array_lengths == {"tokenizer.ggml.tokens": 500, "tokenizer.ggml.scores": 300}
    assert len(h.metadata["tokenizer.chat_template"]) == G.KEEP_STR_MAX and h.string_lengths["tokenizer.chat_template"] == 10_000 and h.param_count() == 16
    assert G.facts_from_header(h)["gguf.chat_template_chars"] == 10_000


def test_a_short_array_is_kept_and_hybrid_models_report_the_layers_that_have_attention():
    data = gguf([kv("general.architecture", STR, "granitehybrid"), kv("granitehybrid.attention.head_count_kv", ARR, [0, 0, 4, 0, 4], U32)])
    f = G.facts_from_header(G.read_header(G.BytesSource(data)))
    assert f["gguf.arch.attention.head_count_kv"] == 4 and f["gguf.arch.attention.head_count_kv_layers"] == 2


@pytest.mark.parametrize("data,why", [(b"NOPE" + b"\0" * 40, "not a GGUF"), (b"GGUF" + (1).to_bytes(4, "little") + b"\0" * 20, "version 1"),
                                       (gguf([kv("general.architecture", STR, "llama")])[:30], "past the end"), (b"GGUF" + b"\x03\0\0\0" + (2 ** 40).to_bytes(8, "little") + b"\0" * 8, "implausible")])
def test_bytes_that_are_not_a_header_this_reader_understands_are_refused_with_the_reason(data, why):
    with pytest.raises(G.GgufError, match=why):
        G.read_header(G.BytesSource(data))


def test_a_header_costs_a_few_requests_not_one_per_field():
    data = model("llama", layers=8)
    calls = []

    class Counting(G.BytesSource):
        def read_at(self, offset, n):
            calls.append((offset, n))
            return super().read_at(offset, n)
    G.read_header(Counting(data), chunk=1 << 20)
    assert len(calls) == 1  # everything is in the first read-ahead
    calls.clear()
    G.read_header(Counting(model("llama", layers=8)), chunk=64)
    assert 1 < len(calls) < 40


def _client(files: dict[str, bytes], seen: list[httpx.Request] | None = None, redirect_to: str | None = None) -> httpx.Client:
    def handler(req: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(req)
        if redirect_to and req.url.host == "huggingface.co":
            return httpx.Response(302, headers={"location": redirect_to + req.url.path})
        data = files.get(req.url.path)
        if data is None:
            return httpx.Response(404)
        a, b = req.headers["range"].split("=")[1].split("-")
        a, b = int(a), min(int(b), len(data) - 1)
        return httpx.Response(206, content=data[a:b + 1], headers={"content-range": f"bytes {a}-{b}/{len(data)}"})
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_a_header_is_read_over_http_range_requests_and_the_size_comes_from_content_range():
    data = model("llama", layers=3)
    seen: list[httpx.Request] = []
    src = HttpRange(_client({"/o/r/resolve/" + "a" * 40 + "/m.gguf": data}, seen), "https://huggingface.co/o/r/resolve/" + "a" * 40 + "/m.gguf")
    assert src.size == len(data)
    assert G.read_header(src).architecture == "llama"
    assert all(r.headers["range"].startswith("bytes=") for r in seen)  # never a plain GET of the whole file


def test_only_the_hub_is_asked_and_a_redirect_that_leaves_the_hubs_domains_is_refused():
    with pytest.raises(HubError, match="not on the list"):
        HttpRange(_client({}), "https://evil.example/o/r/resolve/x/m.gguf")
    with pytest.raises(HubError, match="left the hub's domains"):
        HttpRange(_client({}, redirect_to="https://evil.example"), "https://huggingface.co/o/r/resolve/x/m.gguf")
    ok = HttpRange(_client({"/o/r/resolve/x/m.gguf": model()}, redirect_to="https://cas-bridge.xethub.hf.co"), "https://huggingface.co/o/r/resolve/x/m.gguf")
    assert ok.size == len(model())  # the file server behind the hub's own redirect is allowed


def test_a_revision_that_is_not_a_pinned_commit_is_refused_before_any_request():
    with pytest.raises(HubError, match="pinned"):
        Hub(client=_client({})).repo_info("o/r", "main")
