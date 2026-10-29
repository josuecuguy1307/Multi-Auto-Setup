"""A1: the tool registry. The docstring format is enforced, the menu is generated from the registry, tier and platform filter, and the command validator still applies."""

from __future__ import annotations

import pytest

from envrec.adapters import RING0_KEYS
from envrec.contracts import Tier, ToolSpec
from envrec.discovery import LocalCollector, ReplayCollector
from envrec.tools import DocstringError, ToolContext, all_tools, get_tool, register_tool, run_tool, tool, tool_specs
from envrec.tools.registry import REGISTRY, Tool, parse_docstring

GOOD = """Do one small generic thing.

Args:
    x: the input.
Returns:
    a typed result.
Tier: observe
Platforms: any
Notes: none
"""


@pytest.fixture
def scratch():
    before = set(REGISTRY)
    yield
    for k in set(REGISTRY) - before:
        REGISTRY.pop(k)


def ctx(platform="macos", approve=None, collector=None):
    return ToolContext(collector=collector or LocalCollector(platform), platform=platform, approve=approve)


# ---- the docstring format is enforced ---------------------------------------------------------------------------------------

def test_every_registered_tool_has_a_docstring_in_the_fixed_format():
    tools = all_tools()
    assert len(tools) >= 24
    for t in tools:
        d = parse_docstring(t.fn.__doc__, t.name)
        assert d.summary == t.summary and len(t.summary.split()) <= 12
        assert d.tier in ("observe", "read", "write") and d.platforms and d.notes and d.returns


@pytest.mark.parametrize("doc,why", [
    (None, "needs a docstring"),
    ("This summary has far too many words to be a summary of anything useful at all.\n\nArgs:\n    none\nReturns:\n    x\nTier: observe\nPlatforms: any\nNotes: none", "summary"),
    ("Short.\n\nArgs:\n    none\nReturns:\n    x\nTier: observe\nPlatforms: any", "missing section"),
    ("Short.\n\nArgs:\n    none\nReturns:\n    x\nTier: sudo\nPlatforms: any\nNotes: none", "Tier must be"),
    ("Short.\n\nArgs:\n    none\nReturns:\n    x\nTier: observe\nPlatforms: plan9\nNotes: none", "Platforms"),
    ("Short.\n\nArgs:\n    bad line\nReturns:\n    x\nTier: observe\nPlatforms: any\nNotes: none", "Args lines"),
])
def test_a_docstring_that_breaks_the_format_is_refused(doc, why):
    with pytest.raises(DocstringError, match=why):
        parse_docstring(doc, "f")


def test_the_decorator_checks_the_signature_against_the_docstring(scratch):
    with pytest.raises(DocstringError, match="not documented"):
        @tool()
        def a(ctx, y: int):
            """Do one small generic thing.

            Args:
                x: the input.
            Returns:
                a typed result.
            Tier: observe
            Platforms: any
            Notes: none
            """
    with pytest.raises(DocstringError, match="type annotation"):
        @tool()
        def b(ctx, x):
            """Do one small generic thing.

            Args:
                x: the input.
            Returns:
                a typed result.
            Tier: observe
            Platforms: any
            Notes: none
            """
    with pytest.raises(DocstringError, match="first parameter"):
        @tool()
        def c(x: int):
            """Do one small generic thing.

            Args:
                x: the input.
            Returns:
                a typed result.
            Tier: observe
            Platforms: any
            Notes: none
            """


def test_a_tool_with_a_good_docstring_registers_and_is_callable(scratch):
    @tool(tags=("demo",))
    def demo_tool(ctx, x: int, y: str = "a"):
        """Do one small generic thing.

        Args:
            x: the input.
            y: a label.
        Returns:
            data.echo with the input.
        Tier: observe
        Platforms: any
        Notes: none
        """
        from envrec.tools import ToolResult
        return ToolResult(data={"echo": [x, y]})
    t = get_tool("demo_tool")
    assert t.signature_text() == "demo_tool(x, y?)"
    assert run_tool(t, ctx(), {"x": "7"}).data == {"echo": [7, "a"]}  # "7" is coerced to the declared int


def test_unknown_missing_and_mistyped_arguments_are_refused(scratch):
    t = get_tool("regex_extract")
    assert not run_tool(t, ctx(), {"text": "a", "pattern": "a", "nope": 1}).ok
    assert not run_tool(t, ctx(), {"text": "a"}).ok
    assert not run_tool(t, ctx(), {"text": 5, "pattern": "a"}).ok
    assert run_tool(t, ctx(), {"text": "a1 b2", "pattern": r"([a-z])(\d)"}).data["matches"] == [["a", "1"], ["b", "2"]]


# ---- the menu is generated from the registry ------------------------------------------------------------------------------------

def test_specs_come_from_signature_and_docstring_not_from_a_hand_written_list():
    specs = {s.name: s for s in tool_specs("macos")}
    s = specs["run_command"]
    assert isinstance(s, ToolSpec) and s.one_line == get_tool("run_command").summary and "capture" in s.tags
    assert s.input_model["required"] == ["argv"] and set(s.input_model["properties"]) == {"argv", "name", "timeout_s", "merge_stderr", "window_s", "optional", "answer_if_output"}
    assert s.tier == Tier.observe and s.verified and s.origin.value == "builtin"


def test_ring0_tools_produce_exactly_the_original_seventeen_keys():
    produced = {k for t in all_tools() if "ring0" in t.tags for k in t.produces}
    assert produced == set(RING0_KEYS) and len(RING0_KEYS) == 17


def test_the_real_ring0_tools_run_and_keep_their_keys():
    keys = set()
    c = ctx("macos")
    order = ["read_os", "read_os_native", "read_cpu_ram", "read_hw_native", "read_disks", "read_battery", "read_display", "read_virtualization", "read_tool_versions", "read_net_interfaces"]
    for name in order:
        r = run_tool(get_tool(name), c, {})
        assert r.ok and r.facts is not None and r.captures
        keys |= set(r.facts)
    assert keys <= set(RING0_KEYS) and len(keys) >= 12


# ---- tier and platform -------------------------------------------------------------------------------------------------------

def test_platform_filtering(scratch):
    @tool()
    def only_linux(ctx, x: int):
        """Do one small generic thing.

        Args:
            x: the input.
        Returns:
            nothing.
        Tier: observe
        Platforms: linux
        Notes: none
        """
    assert "only_linux" in {s.name for s in tool_specs("linux")}
    assert "only_linux" not in {s.name for s in tool_specs("macos")}
    r = run_tool(get_tool("only_linux"), ctx("macos"), {"x": 1})
    assert not r.ok and r.error.value == "not_applicable"


def test_read_tier_tools_never_run_without_the_humans_yes(scratch):
    from envrec.tools import ToolResult
    ran = []

    @tool(approval=lambda c, x: f"send {x} to the thing")
    def sender(ctx, x: int):
        """Do one small generic thing.

        Args:
            x: the input.
        Returns:
            nothing.
        Tier: read
        Platforms: any
        Notes: sends a packet.
        """
        ran.append(x)
        return ToolResult()
    t = get_tool("sender")
    assert not run_tool(t, ctx(), {"x": 1}).ok and ran == []
    asked = []
    assert not run_tool(t, ctx(approve=lambda text: asked.append(text) or False), {"x": 1}).ok and ran == [] and asked == ["send 1 to the thing"]
    assert run_tool(t, ctx(approve=lambda text: True), {"x": 2}).ok and ran == [2]
    assert "sender" in {s.name for s in tool_specs("macos")} and "sender" not in {s.name for s in tool_specs("macos", tier_max="observe")}


def test_a_generated_tool_cannot_be_registered_yet():
    t = Tool("evil", lambda ctx: None, "x", (), "x", "observe", ("any",), "none", provenance="generated")
    with pytest.raises(PermissionError):
        register_tool(t)
    assert get_tool("evil") is None


# ---- the command validator still guards every command tool --------------------------------------------------------------------

@pytest.mark.parametrize("argv", [["rm", "-rf", "/tmp/x"], ["sudo", "ls"], ["sh", "-c", "ls"], ["curl", "http://x"], ["cat", "/etc/passwd"], ["python3", "-c", "print(1)"]])
def test_run_command_refuses_what_the_validator_refuses(argv):
    r = run_tool(get_tool("run_command"), ctx("linux"), {"argv": argv})
    assert not r.ok and r.error.value == "denied"


def test_run_command_runs_an_allowed_read_only_command():
    r = run_tool(get_tool("run_command"), ctx("macos"), {"argv": ["uname", "-s"], "name": "u"})
    assert r.ok and r.captures[0].text.strip() == "Darwin" and r.captures[0].name == "u"


def test_read_file_only_reads_the_fixed_paths():
    assert run_tool(get_tool("read_file"), ctx("linux"), {"path": "/etc/shadow"}).error.value == "denied"
    assert run_tool(get_tool("read_file"), ctx("linux"), {"path": "/Users/x/.ssh/id_rsa"}).error.value == "denied"


def test_http_get_is_loopback_only():
    r = run_tool(get_tool("http_get"), ctx("macos"), {"url": "http://example.com/"})
    assert not r.ok and r.error.value == "denied"


def test_lookup_tools_report_a_missing_table_instead_of_guessing():
    from envrec.discovery import tables
    tables.override("usb_ids", None)
    from golden_tables import install, uninstall
    install()
    try:
        r = run_tool(get_tool("lookup_usb_id"), ctx(), {"vid": "1a86", "pid": "7523"})
        assert r.ok and r.data["found"] and "QinHeng" in (r.data["name"] or "")
        assert run_tool(get_tool("lookup_usb_id"), ctx(), {"vid": "ffff"}).data["found"] is False
    finally:
        uninstall()


def test_registry_adapter_matches_the_old_builtin_adapter_on_a_real_machine(tmp_path):
    """Parity for the 17 Ring 0 facts: the same tools through the registry and through BuiltinRing0Adapter give the same keys, statuses and stable values."""
    import asyncio

    from envrec.adapters import BuiltinRing0Adapter
    from envrec.adapters import ToolContext as LoopCtx
    from envrec.contracts import Target, TargetKind
    from envrec.store import Store
    from envrec.tools.bridge import RegistryAdapter
    order = ["read_os", "read_os_native", "read_cpu_ram", "read_hw_native", "read_disks", "read_battery", "read_display", "read_virtualization",
             "read_tool_versions", "read_net_interfaces", "classify_host"]
    got = {}
    for label, adapter in (("old", BuiltinRing0Adapter()), ("new", RegistryAdapter())):
        with Store(tmp_path / label) as st:
            host = st.put_target(Target(kind=TargetKind.host, ring=0, handle="this-machine"))
            for name in order:
                r = asyncio.run(adapter.run(name, {}, LoopCtx(store=st, target=host, event_id=None)))
                assert r.ok, (label, name, r.error)
                for f in r.facts:
                    st.write_fact(f)
            got[label] = {f.key: (f.status.value, f.value, len(f.evidence), f.evidence[0].provider if f.evidence else None) for f in st.current_facts(host.id)}
    assert set(got["old"]) == set(got["new"]) and len(got["new"]) >= 15
    volatile = {"hw.disk_free_gib"}
    for k in got["old"]:
        assert got["old"][k][0] == got["new"][k][0], k  # same status
        assert got["old"][k][2:] == got["new"][k][2:], k  # same evidence count and provider
        if k not in volatile:
            assert got["old"][k][1] == got["new"][k][1], k
