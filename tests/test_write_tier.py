"""The write tier: a write tool shows its exact action AND its undo, the owner answers (a refusal too is an event), the undo entry is on the record BEFORE the effect, and the tool
refuses to run any other way."""
from __future__ import annotations

import asyncio
import threading

import pytest

from envrec.contracts import Target, TargetKind
from envrec.discovery import Salts
from envrec.discovery.types import this_platform
from envrec.store import Store
from envrec.tools import ToolContext, run_tool
from envrec.tools.bridge import RegistryAdapter
from envrec.tools.ctx import ToolResult, prepare_write, run_write_tool
from envrec.tools.registry import REGISTRY, DocstringError, tool

EFFECTS: list = []


@pytest.fixture
def dummy_write_tool():
    EFFECTS.clear()
    store_ref = {}

    @tool(tags=("test_only",), kinds=("media",), approval=lambda ctx, level: f"set the dummy level to {level}", undo=lambda ctx, level: {"tool": "dummy_set_level", "args": {"level": 0}, "text": "set the dummy level back to 0"})
    def dummy_set_level(ctx: ToolContext, level: int) -> ToolResult:
        """Set a dummy level, for the write-tier tests.

        Args:
            level: the level.
        Returns:
            Nothing.
        Tier: write
        Platforms: any
        Notes: test only.
        """
        if threading.get_ident() == ctx.owner_thread:  # on the store's thread: look at the log itself
            undo_first = [e.payload["step"] for e in ctx.store.events(type="undo_entry")]
        else:  # on a worker thread the store is not ours to touch: the tool can only see that it was cleared
            undo_first = ["cleared" if ctx.write_cleared else "NOT CLEARED"]
        EFFECTS.append((level, undo_first))
        return ToolResult(ok=True)

    yield REGISTRY["dummy_set_level"]
    REGISTRY.pop("dummy_set_level", None)


def ctx_for(store, answer):
    t = store.put_target(Target(kind=TargetKind.media, ring=1, handle="this-mac", authorized=True), from_config=True)
    shown = []
    return ToolContext(None, this_platform(), store, t, Salts(), {}, lambda text: shown.append(text) or answer), shown


def test_a_write_tool_must_declare_how_it_asks_and_how_it_is_undone():
    with pytest.raises(DocstringError, match="approval.*undo"):
        @tool()
        def broken_write(ctx: ToolContext) -> ToolResult:
            """Broken.

            Args:
                none
            Returns:
                Nothing.
            Tier: write
            Platforms: any
            Notes: none
            """
            return ToolResult(ok=True)
    assert "broken_write" not in REGISTRY


def test_a_write_tool_does_not_run_without_the_approval_step(dummy_write_tool, tmp_path):
    with Store(tmp_path / "s") as st:
        ctx, _ = ctx_for(st, True)
        res = run_tool(dummy_write_tool, ctx, {"level": 3})
    assert not res.ok and "write tool" in res.detail and EFFECTS == []


def test_the_owner_sees_the_exact_action_and_its_undo_and_the_undo_is_recorded_before_the_effect(dummy_write_tool, tmp_path):
    with Store(tmp_path / "s") as st:
        ctx, shown = ctx_for(st, True)
        res = run_write_tool(dummy_write_tool, ctx, {"level": 3})
        approvals = [e for e in st.events(type="approval")]
        undos = [e for e in st.events(type="undo_entry")]
    assert res.ok and shown == ["set the dummy level to 3\n  undo: set the dummy level back to 0"]
    assert EFFECTS == [(3, ["dummy_set_level"])]  # when the effect ran, the undo entry was already in the log
    assert [e.tag for e in approvals] == ["OWNER"] and approvals[0].payload["granted"] is True and approvals[0].payload["tier"] == "write"
    assert undos[0].payload["undo"] == "dummy_set_level" and undos[0].payload["values"] == {"level": 0} and not ctx.write_cleared


def test_a_refusal_changes_nothing_records_the_refusal_and_no_undo(dummy_write_tool, tmp_path):
    with Store(tmp_path / "s") as st:
        ctx, _ = ctx_for(st, False)
        res = run_write_tool(dummy_write_tool, ctx, {"level": 3})
        approvals, undos = st.events(type="approval"), st.events(type="undo_entry")
    assert not res.ok and EFFECTS == [] and [e.payload["granted"] for e in approvals] == [False] and undos == []


def test_bad_arguments_are_refused_before_the_owner_is_asked(dummy_write_tool, tmp_path):
    with Store(tmp_path / "s") as st:
        ctx, shown = ctx_for(st, True)
        ok, why = prepare_write(dummy_write_tool, ctx, {"level": 3, "oops": 1})
    assert not ok and "oops" in why and shown == [] and EFFECTS == []


def test_through_the_loops_adapter_a_write_tool_asks_once_and_runs_on_the_worker_thread_after_the_undo_is_recorded(dummy_write_tool, tmp_path):
    from envrec.contracts import ProbeResult
    from envrec.adapters.base import ToolContext as LoopContext
    shown = []
    with Store(tmp_path / "s") as st:
        t = st.put_target(Target(kind=TargetKind.media, ring=1, handle="this-mac", authorized=True), from_config=True)
        reg = RegistryAdapter(platform=this_platform(), approve=lambda text: shown.append(text) or True)
        assert "dummy_set_level" in {s.name for s in reg.tool_specs()}
        res = asyncio.run(reg.run("dummy_set_level", {"level": 5}, LoopContext(store=st, target=t, event_id=None)))
        assert isinstance(res, ProbeResult) and res.ok
        assert [e.payload["step"] for e in st.events(type="undo_entry")] == ["dummy_set_level"]
    assert len(shown) == 1 and EFFECTS == [(5, ["cleared"])]
    refused = RegistryAdapter(platform=this_platform(), approve=lambda text: False)
    EFFECTS.clear()
    with Store(tmp_path / "s2") as st:
        t = st.put_target(Target(kind=TargetKind.media, ring=1, handle="this-mac", authorized=True), from_config=True)
        res = asyncio.run(refused.run("dummy_set_level", {"level": 5}, LoopContext(store=st, target=t, event_id=None)))
    assert not res.ok and "not approved" in res.error and EFFECTS == []
