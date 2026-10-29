from envrec.contracts import Decision, Origin, Target, TargetKind, Tier
from envrec.mission import DockerExecBlock, EntryLimits, TargetConfig, TargetEntry, Transport
from envrec.policy.config_gate import decide_for_fleet
from envrec.remote.recipes import RECIPES, register_generated_recipe


def cfg():
    return TargetConfig(targets=[TargetEntry(id="box-1", name="Box", host="10.0.0.2", allowed_transports=[Transport.docker_exec],
                                             docker_exec=DockerExecBlock(container="b"), limits=EntryLimits(max_disk_gb=5, max_model_params="3B"),
                                             owner_confirmed=True)])


def tgt(handle="box-1"):
    return Target(kind=TargetKind.net_host, ring=2, handle=handle)


def test_the_recipe_menu_is_the_closed_list_from_the_spec():
    assert [r.name for r in RECIPES] == ["check", "install_runtime", "select_model", "transfer_model",
                                         "start_server", "health_check", "stop_server", "uninstall"]
    assert all(r.origin == Origin.builtin for r in RECIPES)


def test_write_recipes_need_approval_and_read_recipes_do_not():
    for r in RECIPES:
        d = decide_for_fleet(r, tgt(), cfg())
        assert d.decision == (Decision.needs_approval if r.tier == Tier.write_approval else Decision.allow), r.name


def test_every_recipe_is_denied_for_a_device_that_is_not_in_targets_yaml():
    assert all(decide_for_fleet(r, tgt("found-by-discovery"), cfg()).decision == Decision.deny for r in RECIPES)


def test_a_generated_unverified_recipe_is_denied_on_a_real_target_even_if_enrolled():
    gen = register_generated_recipe("install_unfamiliar_os", Tier.write_approval, "Install runtime on an unfamiliar OS.")
    d = decide_for_fleet(gen, tgt(), cfg())
    assert d.decision == Decision.deny and "unverified" in d.reason


def test_a_generated_recipe_only_runs_on_a_replay_device():
    gen = register_generated_recipe("install_unfamiliar_os", Tier.write_approval, "Install runtime on an unfamiliar OS.")
    config = TargetConfig(targets=[cfg().targets[0].model_copy(update={"id": "replay-os"})])
    replay = Target(kind=TargetKind.net_host, ring=2, handle="replay:os")
    from envrec.policy import decide
    assert decide(gen, replay.model_copy(update={"authorized": True})).decision == Decision.allow_logged
    assert config.get("replay:os") is None  # and a replay handle is not a targets.yaml entry, so the fleet gate still refuses it
    assert decide_for_fleet(gen, replay, config).decision == Decision.deny


def test_a_verified_generated_recipe_follows_its_tier():
    gen = register_generated_recipe("install_unfamiliar_os", Tier.write_approval, "Install runtime on an unfamiliar OS.")
    ok = gen.model_copy(update={"verified": True})
    assert decide_for_fleet(ok, tgt(), cfg()).decision == Decision.needs_approval
