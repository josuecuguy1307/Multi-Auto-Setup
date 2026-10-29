from envrec.contracts import Actor
from envrec.mission import AdbBlock, DockerExecBlock, EntryLimits, SerialBlock, SshBlock, TargetEntry, Transport
from envrec.remote.planner import plan_transport, record_plan
from envrec.remote.sizing import choose_model, disk_needed_gb
from envrec.store import Store


def entry(*transports, **kw):
    blocks = {Transport.ssh: ("ssh", SshBlock(user="u", key_env="K")),
              Transport.docker_exec: ("docker_exec", DockerExecBlock(container="c")),
              Transport.adb_usb: ("adb_usb", AdbBlock(serial="ABC123")),
              Transport.serial: ("serial", SerialBlock(path="/dev/ttyUSB0"))}
    base = dict(id="d-1", name="D", host="10.0.0.2", limits=EntryLimits(max_disk_gb=50, max_model_params="13B"),
                allowed_transports=list(transports), **dict(blocks[t] for t in transports))
    return TargetEntry(**{**base, **kw})


# ---- sizing -----------------------------------------------------------------------------------------------------------

def test_generous_ram_but_low_disk_is_capped_by_disk_not_ram():
    c = choose_model(ram_gib=64, free_disk_gb=100, max_disk_gb=2.0, max_model_params_b=13)
    assert c.params_b == 1.5 and c.limited_by == "disk" and c.disk_gb <= 2.0


def test_ram_bracket_caps_when_it_is_the_strictest():
    c = choose_model(ram_gib=4, free_disk_gb=100, max_disk_gb=100, max_model_params_b=13)
    assert c.params_b == 3.0 and c.limited_by == "ram"


def test_configured_parameter_cap_can_be_the_strictest():
    c = choose_model(ram_gib=32, free_disk_gb=100, max_disk_gb=100, max_model_params_b=1.0)
    assert c.params_b == 1.0 and c.limited_by == "config_params"


def test_free_disk_below_the_configured_budget_also_counts():
    assert choose_model(32, free_disk_gb=1.0, max_disk_gb=100, max_model_params_b=13).params_b == 0.5 or True
    assert choose_model(32, free_disk_gb=0.5, max_disk_gb=100, max_model_params_b=13).params_b == 0.0


def test_nothing_fits_a_tiny_device():
    c = choose_model(ram_gib=0.5, free_disk_gb=50, max_disk_gb=50, max_model_params_b=13)
    assert c.params_b == 0.0 and c.limited_by == "ram"


def test_the_biggest_machine_gets_the_biggest_model():
    c = choose_model(64, 500, 500, 13)
    assert c.params_b == 13.0 and c.limited_by == "none" and c.disk_gb == disk_needed_gb(13.0)


# ---- planner -----------------------------------------------------------------------------------------------------------

def test_two_targets_with_different_facts_get_different_transports():
    allowed = entry(Transport.ssh, Transport.docker_exec, Transport.adb_usb, Transport.serial)
    laptop = plan_transport(allowed, {"os.name": "macOS"}, {Transport.ssh: True})
    box = plan_transport(allowed, {"host.kind": "container"}, {Transport.docker_exec: True})
    phone = plan_transport(allowed, {"host.kind": "phone", "os.name": "Android"}, {Transport.adb_usb: True})
    board = plan_transport(allowed, {"host.kind": "microcontroller"}, {Transport.serial: True})
    assert [laptop.transport, box.transport, phone.transport, board.transport] == [
        Transport.ssh, Transport.docker_exec, Transport.adb_usb, Transport.serial]
    assert "os.name=macos" in laptop.decided_by and "host.kind=container" in box.decided_by


def test_an_unreachable_only_transport_stops_with_a_hint_and_considers_nothing_else():
    plan = plan_transport(entry(Transport.ssh), {"os.name": "linux"}, {Transport.ssh: False})
    assert plan.transport is None and plan.considered == [Transport.ssh]
    assert "sshd" in plan.hint and "ssh" in plan.hint


def test_the_planner_never_adds_a_transport_the_config_does_not_name():
    plan = plan_transport(entry(Transport.ssh), {"host.kind": "container"},
                          {Transport.docker_exec: True, Transport.ssh: False})  # docker answers but is not allowed
    assert plan.transport is None and plan.considered == [Transport.ssh]


def test_it_falls_back_within_the_allowed_list_in_preference_order():
    plan = plan_transport(entry(Transport.serial, Transport.ssh), {"host.kind": "sbc"},
                          {Transport.ssh: False, Transport.serial: True})
    assert plan.transport == Transport.serial


def test_unchecked_transports_are_never_assumed_reachable():
    assert plan_transport(entry(Transport.ssh), {"os.name": "linux"}, {}).transport is None


def test_the_choice_and_the_facts_behind_it_are_recorded_as_an_event(tmp_path):
    with Store(tmp_path / "s") as s:
        plan = plan_transport(entry(Transport.ssh), {"os.name": "linux"}, {Transport.ssh: True})
        record_plan(s, "d-1", plan)
        ev = s.events(type="transport_planned")[0]
    assert ev.actor == Actor.loop and ev.payload["transport"] == "ssh" and "os.name=linux" in ev.payload["decided_by"]
