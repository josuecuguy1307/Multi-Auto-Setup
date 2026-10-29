from pathlib import Path

from envrec.mission import Transport, load_mission, load_targets

ROOT = Path(__file__).resolve().parent.parent


def test_the_shipped_examples_are_valid_and_say_what_the_docs_say():
    mission = load_mission(ROOT / "config" / "mission.example.yaml")
    assert mission.limits.max_workers == 3 and mission.limits.deploy_requires_approval is True
    cfg = load_targets(ROOT / "config" / "targets.example.yaml")
    assert [t.id for t in cfg.targets] == ["laptop-b", "lab-box"]
    assert cfg.get("laptop-b").limits.params_b == 9.0
    assert cfg.get("lab-box").allowed_transports == [Transport.docker_exec, Transport.ssh]
