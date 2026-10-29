"""The per-OS layer's contract, where state lives, the registry, the bind guard and the event log's refusal to hold a secret. SIMULATED-LOCAL / unit."""
from __future__ import annotations

import json
import os

import pytest

import envrec_os
from envrec_coordinator.registry import Registry
from envrec_coordinator.server import BindRefused, check_bind
from envrec_net import identity as ident
from envrec_net.log import EventLog


def test_the_default_state_folder_comes_from_the_skills_table_for_the_running_family_and_the_environment_can_override_it(tmp_path, monkeypatch):
    table = json.loads(envrec_os.STATE_PATHS_FILE.read_text())
    monkeypatch.delenv("MAS_STATE_DIR", raising=False)
    got = envrec_os.default_state_dir({"HOME": str(tmp_path), "LOCALAPPDATA": str(tmp_path / "local"), "XDG_DATA_HOME": str(tmp_path / "xdg")})
    assert str(got).endswith("mas") and table[envrec_os.family()]
    assert envrec_os.default_state_dir({"MAS_STATE_DIR": str(tmp_path / "x")}) == tmp_path / "x"


def test_a_family_with_no_row_in_the_table_has_no_default_and_says_which_file_to_edit(tmp_path, monkeypatch):
    f = tmp_path / "state-dirs.json"
    f.write_text(json.dumps({"darwin": "~/x"}))
    monkeypatch.setattr(envrec_os, "STATE_PATHS_FILE", f)
    monkeypatch.setattr(envrec_os, "family", lambda: "plan9")
    with pytest.raises(LookupError) as exc:
        envrec_os.default_state_dir({})
    assert "plan9" in str(exc.value) and "state-dirs.json" in str(exc.value)


@pytest.mark.parametrize("template,env,expected", [
    ("${XDG_DATA_HOME:-~/.local/share}/mas", {"XDG_DATA_HOME": "/data"}, "/data/mas"),
    ("${XDG_DATA_HOME:-/fallback}/mas", {}, "/fallback/mas"),
    ("%LOCALAPPDATA%\\mas", {"LOCALAPPDATA": "C:\\L"}, "C:\\L\\mas"),
])
def test_folder_templates_expand_the_environment(template, env, expected):
    assert str(envrec_os._expand(template, env)) == expected


def test_the_node_card_has_exactly_the_basics():
    c = envrec_os.card()
    assert set(c) == {"hostname", "os", "os_release", "arch"} and c["os"] in ("darwin", "linux", "windows")


def test_a_private_key_is_owner_only_from_the_first_byte_written_and_survives_a_reload(tmp_path):
    layer = envrec_os.current()
    me = ident.create()
    ident.save(me, tmp_path / "id", layer)
    assert layer.is_private(tmp_path / "id") and layer.is_private(tmp_path / "id" / "key.pem")
    again = ident.load(tmp_path / "id")
    assert again.node_id == me.node_id and again.cert_der == me.cert_der
    assert ident.load(tmp_path / "nothing") is None


def test_the_registry_persists_pins_and_forgets_on_removal_and_its_file_is_private(tmp_path):
    layer = envrec_os.current()
    a = ident.create()
    reg = Registry(tmp_path, layer)
    rec = reg.add(name="a", os_family="linux", arch="x86_64", cert_der=a.cert_der, clock=lambda: 5.0)
    assert Registry(tmp_path, layer).get(rec.node_id).fingerprint == a.fingerprint and layer.is_private(tmp_path / "nodes.json")
    assert reg.by_cert(a.cert_der).node_id == a.node_id and reg.public_key(a.node_id) == a.public_raw and reg.by_cert(ident.create().cert_der) is None
    reg.heartbeat(a.node_id, 9.0)
    assert Registry(tmp_path, layer).get(a.node_id).last_heartbeat == 9.0
    assert reg.remove(a.node_id) and Registry(tmp_path, layer).all() == [] and not reg.remove(a.node_id)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "8.8.8.8", "224.0.0.1", "example.com", ""])
def test_the_coordinator_refuses_to_listen_on_every_interface_a_public_address_or_a_name(host):
    with pytest.raises(BindRefused):
        check_bind(host)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "192.168.1.20", "10.0.0.5", "169.254.1.1"])
def test_the_coordinator_accepts_loopback_private_and_link_local_addresses(host):
    assert check_bind(host) == host


def test_the_event_log_refuses_a_payload_that_could_carry_a_secret(tmp_path):
    log = EventLog(tmp_path)
    with pytest.raises(ValueError):
        log.add("x", {"code": "123456"})
    with pytest.raises(ValueError):
        log.add("x", {"private_key": "..."})
    ev = log.add("pair_offer_created", {"offer": "o1"})
    assert ev.effective_tag == "GUARD" and log.add("owner_decision", {"accepted": True}, owner=True).effective_tag == "OWNER"
    log.close()
