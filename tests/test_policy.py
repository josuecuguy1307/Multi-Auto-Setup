import pytest

from envrec.contracts import Decision, Origin, Target, TargetKind, Tier, ToolSpec
from envrec.policy import PolicyState, decide


def tool(name="read_thing", tier=Tier.observe, ring=0, kinds=(TargetKind.host,), **kw):
    return ToolSpec(name=name, ring=ring, tier=tier, target_kinds=list(kinds),
                    one_line="does a thing", source="builtin", **kw)


def target(kind=TargetKind.host, ring=0, handle="this-machine", authorized=False):
    return Target(kind=kind, ring=ring, handle=handle, authorized=authorized)


def test_observe_and_read_are_allowed_without_authorization():
    assert decide(tool(tier=Tier.observe), target()).decision == Decision.allow
    assert decide(tool(tier=Tier.read), target()).decision == Decision.allow


def test_write_logged_needs_authorization_then_is_logged():
    t = tool(tier=Tier.write_logged)
    assert decide(t, target()).decision == Decision.deny
    assert decide(t, target(authorized=True)).decision == Decision.allow_logged


def test_write_with_approval_asks_then_runs_logged_once_approved():
    t = tool(tier=Tier.write_approval)
    tg = target(authorized=True)
    assert decide(t, tg).decision == Decision.needs_approval
    state = PolicyState(approved=frozenset({(t.name, tg.id)}))
    assert decide(t, tg, state).decision == Decision.allow_logged
    assert decide(t, target()).decision == Decision.deny  # unauthorized: no question asked at all


def test_generated_unverified_tool_is_always_denied_on_real_targets():
    g = tool(origin=Origin.generated, verified=False)
    for tg in (target(), target(authorized=True), target(ring=1)):
        d = decide(g, tg)
        assert d.decision == Decision.deny
        assert "unverified" in d.reason


def test_generated_unverified_tool_may_run_on_a_replay_device():
    g = tool(origin=Origin.generated, verified=False, kinds=(TargetKind.serial,))
    d = decide(g, target(kind=TargetKind.serial, ring=1, handle="replay:mystery"))
    assert d.decision == Decision.allow_logged


def test_verified_generated_tool_follows_its_tier():
    g = tool(origin=Origin.generated, verified=True)
    assert decide(g, target()).decision == Decision.allow


@pytest.mark.parametrize("ring_of_tool, ring_of_target", [(2, 0), (3, 0), (0, 2), (1, 3)])
def test_target_without_authorized_is_denied_for_ring_2_and_3(ring_of_tool, ring_of_target):
    d = decide(tool(ring=ring_of_tool), target(ring=ring_of_target))
    assert d.decision == Decision.deny
    assert "authorized" in d.reason


def test_authorized_target_passes_ring_2_and_3():
    assert decide(tool(ring=2), target(ring=2, authorized=True)).decision == Decision.allow
    assert decide(tool(ring=3), target(ring=3, authorized=True)).decision == Decision.allow


@pytest.mark.parametrize("tags,name", [
    (["delete"], "tidy"), (["format"], "tidy"), (["flash"], "tidy"), (["elevate"], "tidy"),
    ([], "delete_logs"), ([], "flash_firmware"), ([], "run_sudo_thing"),
])
def test_denylist_beats_tier_and_authorization(tags, name):
    d = decide(tool(name=name, tier=Tier.observe, tags=tags), target(authorized=True))
    assert d.decision == Decision.deny
    assert "denylist" in d.reason


def test_tool_must_apply_to_the_target_kind():
    d = decide(tool(kinds=(TargetKind.usb,)), target(kind=TargetKind.host))
    assert d.decision == Decision.deny


def test_every_decision_has_a_reason_and_names_the_tool():
    d = decide(tool(), target())
    assert d.reason and d.tool == "read_thing"
