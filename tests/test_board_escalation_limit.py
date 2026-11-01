"""The board's escalation limit is a coordinator setting (`board_escalation_limit`), as data: a bad value is refused with a typed reason, a number outside 0 to the router's maximum is clamped, and the value
reaches `Services.board_preapproved`. It counts steps, not money. SIMULATED-LOCAL (a real coordinator on loopback; the owner's hands are not involved)."""
from __future__ import annotations

import asyncio
import json

import pytest

from envrec_coordinator.router.facts import Policy
from envrec_coordinator.server import Coordinator
from envrec_mas import coordconfig


def run(coro):
    return asyncio.run(coro)


async def preapproved(tmp_path, **kw):
    c = Coordinator(tmp_path / "coord", "127.0.0.1", 0, discovery_port=None, **kw)
    await c.start()
    try:
        return c.services.board_preapproved
    finally:
        await c.stop()


def test_the_key_is_in_the_settings_data_with_the_routers_maximum_as_its_default():
    spec = coordconfig.load_schema()["keys"]["board_escalation_limit"]
    assert (spec["type"], spec["default"], spec["flag"]) == ("int", Policy.defaults().max_escalation, "--board-escalation-limit")
    assert "spend limit" in spec["help"] and "clamped" in spec["help"]


@pytest.mark.parametrize("bad", ["2", 1.5, True, [1], {"n": 1}])
def test_a_value_that_is_not_an_integer_is_refused_with_a_typed_reason_before_anything_is_written(tmp_path, bad):
    with pytest.raises(coordconfig.ConfigRefused) as e:
        coordconfig.write(tmp_path / "st", {"bind": "127.0.0.1", "board_escalation_limit": bad})
    assert e.value.reason == "bad_type" and "board_escalation_limit" in e.value.text and not (tmp_path / "st" / "coordinator.json").exists()
    (tmp_path / "st").mkdir(exist_ok=True)
    (tmp_path / "st" / "coordinator.json").write_text(json.dumps({"bind": "127.0.0.1", "board_escalation_limit": bad}))
    with pytest.raises(coordconfig.ConfigRefused) as e2:
        coordconfig.read(tmp_path / "st")
    assert e2.value.reason == "bad_type"  # the same refusal when the file was edited by hand


def test_null_is_refused_because_the_key_has_a_default_and_not_a_meaning_for_nothing(tmp_path):
    with pytest.raises(coordconfig.ConfigRefused) as e:
        coordconfig.check({"board_escalation_limit": None})
    assert e.value.reason == "bad_value"


def test_the_flag_beats_the_file_and_the_file_beats_the_default(tmp_path):
    st = tmp_path / "st"
    assert coordconfig.resolve({}, st)["board_escalation_limit"] == 2
    coordconfig.write(st, {"bind": "127.0.0.1", "board_escalation_limit": 1})
    assert coordconfig.resolve({}, st)["board_escalation_limit"] == 1
    assert coordconfig.resolve({"board_escalation_limit": 0}, st)["board_escalation_limit"] == 0  # an explicit zero is a value, not "not given"


@pytest.mark.parametrize("given,expected", [(0, 0), (1, 1), (2, 2), (3, 2), (99, 2), (-1, 0), (-99, 0)])
def test_the_value_reaches_the_board_clamped_to_zero_and_the_routers_maximum(tmp_path, given, expected):
    assert Policy.defaults().max_escalation == 2  # the bounds this test states are the router's
    assert run(preapproved(tmp_path, board_escalation_limit=given)) == expected


def test_a_coordinator_started_without_the_setting_keeps_the_default_of_two(tmp_path):
    assert run(preapproved(tmp_path)) == 2


def test_a_coordinator_handed_a_non_integer_directly_refuses_to_start_a_board_service(tmp_path):
    for bad in ("2", 1.5, True):
        async def go(bad=bad):
            c = Coordinator(tmp_path / f"c{bad!r}", "127.0.0.1", 0, discovery_port=None, board_escalation_limit=bad)
            try:
                await c.start()
            finally:
                await c.stop()
        with pytest.raises(TypeError, match="board_escalation_limit"):
            run(go())


def test_the_limit_is_shown_to_the_owner_with_the_board_and_says_steps(tmp_path):
    from envrec_coordinator import services_strings as S
    text = S.BOARD_LIMIT.format(n=1)
    assert "1" in text and "without asking you" in text
