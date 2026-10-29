import asyncio
import json
import shutil
from pathlib import Path

import httpx
import pytest

from envrec.contracts import Action, ActionType, ModelKind, ModelProfile
from envrec.model import (
    ActionParseError,
    ModelError,
    OpenAICompatModel,
    ScriptedModel,
    load_profile,
    make_adapter,
    parse_action,
    request_action,
    with_view_budget,
)

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "models.yaml"
FALLBACK = Action(action=ActionType.record_unknown, why="model gave no valid action")
GOOD = '{"action": "call_tool", "tool": "read_cpu", "why": "need cpu"}'


# ---- parsing -------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        GOOD,
        f"```json\n{GOOD}\n```",
        f"<think>maybe {{not json}} first</think>\n{GOOD}",
        f"Sure! Here is my answer:\n{GOOD}\nHope that helps.",
        '{"action": "call_tool", "tool": "read_cpu", "why": "has } and { inside"}',
        f"{GOOD}\n{{\"action\": \"done\"}}",  # two objects: the first one wins
    ],
)
def test_parse_accepts_the_usual_wrappings(text):
    a = parse_action(text)
    assert a.action == ActionType.call_tool and a.tool == "read_cpu"


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("I think we should look at the CPU", "no JSON object"),
        ('{"action": "call_tool"', "not closed"),
        ("{action: call_tool}", "not valid JSON"),
        ('{"action": "call_tool"}', "requires: tool"),
        ('{"action": "teleport"}', "action"),
        ('{"action": "done", "bonus": 1}', "bonus"),
    ],
)
def test_parse_rejections_are_readable(text, needle):
    with pytest.raises(ActionParseError) as exc:
        parse_action(text)
    assert needle in str(exc.value)


# ---- requester: retry once, then fall back -------------------------------------------------

def run_request(replies, events=None):
    model = ScriptedModel(replies)
    sink = [] if events is None else events
    result = asyncio.run(request_action(model, "SYS", "USER", fallback=FALLBACK,
                                        emit=lambda t, p: sink.append((t, p))))
    return model, result, sink


def test_valid_reply_needs_one_call():
    model, result, events = run_request([GOOD])
    assert result.attempts == 1 and not result.used_fallback
    assert result.action.tool == "read_cpu"
    assert [t for t, _ in events] == ["model_call"]


def test_invalid_then_valid_retries_once_with_the_error_in_the_prompt():
    model, result, events = run_request(["not json at all", GOOD])
    assert result.attempts == 2 and not result.used_fallback
    second_user_prompt = model.calls[1][1]
    assert "previous reply was rejected" in second_user_prompt and "no JSON object" in second_user_prompt
    assert "USER" in second_user_prompt
    assert [t for t, _ in events] == ["model_call", "model_invalid", "model_call"]


def test_invalid_twice_falls_back_to_record_unknown_and_logs_it():
    model, result, events = run_request(["nope", '{"action": "call_tool"}'])
    assert result.used_fallback and result.action.action == ActionType.record_unknown
    assert result.attempts == 2 and len(model.calls) == 2  # never a third call
    assert [t for t, _ in events][-1] == "model_fallback"
    assert len(result.errors) == 2


def test_backend_error_then_valid_still_succeeds():
    _, result, events = run_request([RuntimeError("connection refused"), GOOD])
    assert not result.used_fallback
    assert "backend error" in events[0][1]["error"]


def test_backend_error_twice_falls_back():
    _, result, _ = run_request([RuntimeError("down"), RuntimeError("still down")])
    assert result.used_fallback


def test_token_counts_are_summed_across_attempts():
    _, result, _ = run_request(["bad", GOOD])
    assert result.prompt_tokens > 0 and result.completion_tokens > 0


# ---- OpenAI-compatible client --------------------------------------------------------------

def profile(**kw):
    base = dict(name="p", model_id="my-model", endpoint="http://host:1234/v1/",
                context_tokens=4096, view_budget_facts=4, menu_max=4)
    return ModelProfile(**{**base, **kw})


def mock_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_openai_compat_sends_the_configured_model_and_options(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={
            "choices": [{"message": {"content": GOOD}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 7}})

    monkeypatch.setenv("MY_KEY_ENV", "sk-test-not-real")
    model = OpenAICompatModel(profile(api_key_env="MY_KEY_ENV"), json_mode=True,
                              extra_body={"reasoning_effort": "none"}, client=mock_client(handler))
    done = asyncio.run(model.complete("sys", "usr"))
    assert seen["url"] == "http://host:1234/v1/chat/completions"
    assert seen["body"]["model"] == "my-model"
    assert seen["body"]["response_format"] == {"type": "json_object"}
    assert seen["body"]["reasoning_effort"] == "none"
    assert seen["body"]["messages"][0] == {"role": "system", "content": "sys"}
    assert seen["auth"] == "Bearer sk-test-not-real"
    assert (done.text, done.prompt_tokens, done.completion_tokens) == (GOOD, 12, 7)


def test_openai_compat_without_key_env_sends_no_auth_header():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]})

    asyncio.run(OpenAICompatModel(profile(), client=mock_client(handler)).complete("s", "u"))
    assert seen["auth"] is None


@pytest.mark.parametrize("response", [
    httpx.Response(500, text="boom"),
    httpx.Response(200, text="not json"),
    httpx.Response(200, json={"choices": []}),
])
def test_openai_compat_failures_become_model_errors(response):
    model = OpenAICompatModel(profile(), client=mock_client(lambda r: response))
    with pytest.raises(ModelError):
        asyncio.run(model.complete("s", "u"))


def test_openai_compat_connection_error_becomes_model_error():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(ModelError):
        asyncio.run(OpenAICompatModel(profile(), client=mock_client(handler)).complete("s", "u"))


# ---- config ----------------------------------------------------------------------------------

def test_profiles_load_from_yaml_and_build_the_right_adapters():
    glimmer, g_opts = load_profile(CONFIG, "muse-glimmer", include_local=False)
    spark, s_opts = load_profile(CONFIG, "muse-spark", include_local=False)
    assert isinstance(make_adapter(glimmer, g_opts), OpenAICompatModel)
    assert isinstance(make_adapter(spark, s_opts), OpenAICompatModel)
    assert glimmer.view_budget_facts < spark.view_budget_facts


def test_a_local_file_overrides_and_adds_profiles(tmp_path):
    (tmp_path / "models.yaml").write_text(
        "profiles:\n  a: {name: a, kind: openai_compat, model_id: m1, endpoint: http://x/v1, supports_tool_calling: false,"
        " context_tokens: 100, view_budget_facts: 1, menu_max: 2}\n", encoding="utf-8")
    (tmp_path / "models.local.yaml").write_text(
        "profiles:\n  a: {name: a, kind: openai_compat, model_id: m2, endpoint: http://y/v1, supports_tool_calling: false,"
        " context_tokens: 100, view_budget_facts: 1, menu_max: 2}\n"
        "  b: {name: b, kind: openai_compat, model_id: m3, endpoint: http://z/v1, supports_tool_calling: false,"
        " context_tokens: 100, view_budget_facts: 1, menu_max: 2}\n", encoding="utf-8")
    assert load_profile(tmp_path / "models.yaml", "a")[0].model_id == "m2"
    assert load_profile(tmp_path / "models.yaml", "b")[0].model_id == "m3"
    assert load_profile(tmp_path / "models.yaml", "a", include_local=False)[0].model_id == "m1"


def test_unknown_profile_name_lists_the_known_ones():
    with pytest.raises(KeyError) as exc:
        load_profile(CONFIG, "nope", include_local=False)
    assert "muse-glimmer" in str(exc.value)


def test_view_budget_override_changes_only_the_dials():
    small, _ = load_profile(CONFIG, "muse-glimmer", include_local=False)
    big = with_view_budget(small, 30, 8)
    a, b = small.model_dump(), big.model_dump()
    changed = {k for k in a if a[k] != b[k]}
    assert changed == {"view_budget_facts", "menu_max"}


def test_no_model_name_is_hard_coded_in_the_source():
    import re

    names = re.compile(r"\b(muse|glimmer|spark|gpt-|llama)", re.I)  # \b keeps 'ollama' legal
    for path in (ROOT / "src" / "envrec").rglob("*.py"):
        hit = names.search(path.read_text(encoding="utf-8"))
        assert hit is None, f"{path.name} mentions {hit.group(0)!r}"


# ---- live smoke tests (skipped when the backend is absent) ------------------------------------

def _ollama_has(model_id: str) -> bool:
    try:
        tags = httpx.get("http://localhost:11434/api/tags", timeout=2).json()
    except Exception:
        return False
    return any(m["name"] == model_id for m in tags.get("models", []))


@pytest.mark.live
def test_live_local_model_returns_a_valid_action():
    prof, opts = load_profile(CONFIG, "muse-glimmer")
    if not _ollama_has(prof.model_id):
        pytest.skip("ollama is not running or the configured model is not installed")
    system = "You control a recognition harness. Reply with one JSON object only."
    user = 'MENU\n1 read_cpu  read cpu model\nReply with {"action":"call_tool","tool":"read_cpu","why":"..."}'
    result = asyncio.run(request_action(make_adapter(prof, opts), system, user, fallback=FALLBACK))
    assert not result.used_fallback and result.action.action == ActionType.call_tool


# ---- narrow, menu-bound repair of two small-model form slips -----------------------------------------

from envrec.model.parse import parse_action_repairing  # noqa: E402

MENU = {"read_cpu", "read_os"}


def test_a_tool_name_in_the_action_field_is_read_as_call_tool():
    action, note = parse_action_repairing('{"action": "read_cpu", "why": "need cpu"}', MENU)
    assert (action.action, action.tool) == (ActionType.call_tool, "read_cpu") and "read as call_tool" in note


def test_a_missing_action_with_a_menu_tool_is_read_as_call_tool():
    action, note = parse_action_repairing('{"tool": "read_os"}', MENU)
    assert (action.action, action.tool) == (ActionType.call_tool, "read_os") and note


def test_nothing_outside_the_menu_is_repaired():
    for text in ('{"action": "teleport"}', '{"tool": "teleport"}', '{"action": "read_gpu"}'):
        with pytest.raises(ActionParseError):
            parse_action_repairing(text, MENU)


def test_without_a_menu_there_is_no_repair_and_valid_replies_are_untouched():
    with pytest.raises(ActionParseError):
        parse_action_repairing('{"action": "read_cpu"}', None)
    action, note = parse_action_repairing(GOOD, MENU)
    assert note is None and action.tool == "read_cpu"
    done, note = parse_action_repairing('{"action": "done"}', MENU)
    assert note is None and done.action == ActionType.done


def test_repairs_are_counted_and_reported_by_the_requester():
    model = ScriptedModel(['{"action": "read_cpu"}'])
    events = []
    result = asyncio.run(request_action(model, "S", "U", fallback=FALLBACK, repair_tools=MENU,
                                        emit=lambda t, p: events.append(t)))
    assert result.repaired == 1 and not result.used_fallback and result.attempts == 1
    assert "model_repaired" in events and "model_invalid" not in events


def test_the_retry_prompt_carries_the_format_hint():
    model = ScriptedModel(['{"action": "dance"}', GOOD])
    asyncio.run(request_action(model, "S", "U", fallback=FALLBACK, retry_hint="HINT: tool names go in tool"))
    assert "HINT: tool names go in tool" in model.calls[1][1]
