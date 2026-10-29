"""The computer route's fixed code: the single-use setup link, the door file, the pairing code, the pinning, the key. No device is contacted."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from envrec.doors.pair_code import host_key_blob, pairing_code
from envrec.doors.pin import PairingRefused, pin_if_code_matches
from envrec.doors.setup_server import SetupLink, private_lan_address
from envrec.doors.ssh_keys import create_keypair, key_path
from envrec.doors.windows_door import MARKER, render_door

BLOB = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes(range(32))
HOST_PUB = "ssh-ed25519 " + base64.b64encode(BLOB).decode()
MAC_PUB = "ssh-ed25519 " + base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes(range(100, 132))).decode() + " envrec-owner-name"
GOLDEN = Path(__file__).parent / "golden" / "doors" / "windows_door.cmd"


def get(url, timeout=3):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


# ---- the pairing code -------------------------------------------------------------------------------------------------------------------------

def test_the_code_is_six_digits_from_the_sha256_of_the_host_key_blob():
    expected = f"{int.from_bytes(hashlib.sha256(BLOB).digest()[:4], 'big') % 1_000_000:06d}"
    assert pairing_code(HOST_PUB) == expected and len(expected) == 6 and expected.isdigit()
    assert pairing_code(HOST_PUB + " some comment") == expected  # the comment does not change it
    other = "ssh-ed25519 " + base64.b64encode(BLOB[:-1] + b"\xff").decode()
    assert pairing_code(other) != expected


@pytest.mark.parametrize("bad", ["", "ssh-rsa AAAA", "ssh-ed25519", "ssh-ed25519 not*base64!"])
def test_a_line_that_is_not_an_ed25519_public_key_gives_no_code(bad):
    with pytest.raises(Exception):
        pairing_code(bad)


def test_the_windows_door_computes_the_same_code_the_mac_does():
    """The PowerShell in the door reads the first four bytes of the SHA-256 big-endian modulo 1e6; this pins the text of that formula next to the Python one."""
    door = render_door("192.168.1.20", MAC_PUB)
    assert "$h[3],$h[2],$h[1],$h[0]" in door and "$n %% 1000000" in door and "ToString('000000')" in door and "ComputeHash($b)" in door
    h = hashlib.sha256(BLOB).digest()
    assert int.from_bytes(h[:4], "big") == int.from_bytes(bytes([h[0], h[1], h[2], h[3]]), "big")  # the order the PowerShell rebuilds


# ---- the door file ----------------------------------------------------------------------------------------------------------------------------

def test_the_door_file_is_pinned_by_a_golden_and_only_the_key_and_address_vary():
    door = render_door("192.168.1.20", MAC_PUB)
    if not GOLDEN.exists():
        GOLDEN.write_bytes(door.encode())
    assert door.encode() == GOLDEN.read_bytes()
    other = render_door("10.0.0.7", "ssh-ed25519 " + base64.b64encode(b"x" * 51).decode())
    assert other.replace("10.0.0.7", "192.168.1.20").replace(other.split("ssh-ed25519 ")[1].split()[0], door.split("ssh-ed25519 ")[1].split()[0]) == door


def test_the_door_file_says_what_it_will_do_before_it_does_anything_and_restricts_everything():
    door = render_door("192.168.1.20", MAC_PUB)
    first_action = min(door.index(w) for w in ("Start-Process", "sc config", "netsh", "Add-WindowsCapability"))
    assert door.index("This file will do exactly the following") < first_action and door.index("pause") < first_action
    for must in ("sc config sshd start= demand", "profile=private", 'from="192.168.1.20"', 'permitopen="127.0.0.1:8089"', "PasswordAuthentication no", "-Verb RunAs",
                 "administrators_authorized_keys", "authorized_keys", "icacls", "envrec-undo.cmd"):
        assert must in door, must
    assert door.count(MARKER) >= 8  # every line it adds to a system file is tagged: that is what the undo removes
    assert "\r\n" in door and "\n" not in door.replace("\r\n", "")


def test_the_door_embeds_only_the_public_key_and_not_its_comment():
    door = render_door("192.168.1.20", MAC_PUB)
    assert MAC_PUB.split()[1] in door and "envrec-owner-name" not in door
    for bad in ("-----BEGIN OPENSSH PRIVATE KEY-----", "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQC", MAC_PUB + "\nssh-ed25519 " + MAC_PUB.split()[1]):
        with pytest.raises(ValueError):
            render_door("192.168.1.20", bad)
    for ip in ("example.com", "999.1.1.1", "1.2.3", "192.168.1.20; calc"):
        with pytest.raises(ValueError):
            render_door(ip, MAC_PUB)


# ---- the setup link ---------------------------------------------------------------------------------------------------------------------------

def _link(**kw):
    import ipaddress
    events = []
    link = SetupLink("127.0.0.1", b"DOOR-BYTES", emit=lambda k, i: events.append((k, i)), check=ipaddress.IPv4Address, **kw)
    return link, events


def test_the_link_serves_the_file_once_and_then_stops():
    link, events = _link()
    url = link.start()
    assert get(url) == (200, b"DOOR-BYTES") and link.outcome == "served" and link.fetched_by == "127.0.0.1"
    assert get(url)[0] == 410  # a second download is refused
    link.stop()
    assert [k for k, _ in events][:2] == ["setup_link_started", "setup_link_served"]


def test_a_second_request_in_the_same_instant_is_refused_so_the_file_is_never_served_twice():
    link, events = _link()
    url = link.start()
    link.served = True  # as if another request had just taken it
    assert get(url)[0] == 410 and ("setup_link_refused", {"reason": "already used", "client": "127.0.0.1"}) in events
    link.stop()


def test_a_wrong_token_or_path_gets_nothing_and_is_recorded():
    link, events = _link()
    url = link.start()
    base = url.rsplit("/", 2)[0]
    assert get(base + "/" + "x" * 32 + "/d.cmd")[0] == 404 and get(url.replace("d.cmd", "other.cmd"))[0] == 404
    assert not link.served and [i["reason"] for k, i in events if k == "setup_link_refused"] == ["wrong token or path", "wrong token or path"]
    link.stop()


def test_a_client_outside_the_private_network_is_refused():
    import ipaddress
    link, events = _link()
    link.net = ipaddress.ip_network("192.168.77.0/24")  # the Mac's network is not the client's
    url = link.start()
    assert get(url)[0] == 403 and not link.served and events[-1][1]["reason"] == "client outside the private network"
    link.stop()


def test_the_link_expires_and_stops_serving():
    link, events = _link(ttl_s=0.4)
    url = link.start()
    assert link.done.wait(3) and link.outcome == "expired"
    with pytest.raises(OSError):
        get(url, timeout=1)
    assert [k for k, _ in events] == ["setup_link_started", "setup_link_expired"]


def test_the_token_is_at_least_128_bits_differs_per_link_and_is_not_in_any_event():
    a, ev_a = _link()
    b, _ = _link()
    assert len(base64.urlsafe_b64decode(a.token + "=" * (-len(a.token) % 4))) >= 16 and a.token != b.token
    a.start()
    assert a.token not in json.dumps(ev_a)
    a.stop()


def test_a_public_loopback_or_wildcard_address_is_never_bound():
    for bad in ("8.8.8.8", "127.0.0.1", "0.0.0.0", "::1"):
        with pytest.raises(ValueError):
            private_lan_address(bad)
    assert str(private_lan_address("192.168.1.20")) == "192.168.1.20" and str(private_lan_address("10.1.2.3")) == "10.1.2.3"


# ---- pinning ----------------------------------------------------------------------------------------------------------------------------------

def test_a_matching_code_pins_the_host_key_and_a_mismatch_writes_nothing(tmp_path):
    kh = tmp_path / "known"
    scan = lambda host, port: HOST_PUB.split()[0] + " " + HOST_PUB.split()[1]  # noqa: E731
    with pytest.raises(PairingRefused):
        pin_if_code_matches("192.168.1.30", "000000" if pairing_code(HOST_PUB) != "000000" else "111111", known_hosts=kh, scan=scan)
    assert not kh.exists()
    for typed in ("12345", "abcdef", "", "1234567"):
        with pytest.raises(PairingRefused):
            pin_if_code_matches("192.168.1.30", typed, known_hosts=kh, scan=scan)
    assert not kh.exists()
    assert pin_if_code_matches("192.168.1.30", pairing_code(HOST_PUB), known_hosts=kh, scan=scan) == pairing_code(HOST_PUB)
    assert kh.read_text() == f"192.168.1.30 {HOST_PUB}\n" and stat.S_IMODE(kh.stat().st_mode) == 0o600
    pin_if_code_matches("192.168.1.30", pairing_code(HOST_PUB), known_hosts=kh, scan=scan)
    assert kh.read_text().count("\n") == 1  # pinning twice does not duplicate the line


# ---- the key ----------------------------------------------------------------------------------------------------------------------------------

def test_the_key_is_made_with_mode_600_and_only_the_public_half_comes_back(tmp_path):
    pub = create_keypair("laptop", tmp_path / "ssh")
    path = key_path("laptop", tmp_path / "ssh")
    assert pub.startswith("ssh-ed25519 ") and "PRIVATE" not in pub and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text().startswith("-----BEGIN OPENSSH PRIVATE KEY-----") and path.read_text() not in pub
    with pytest.raises(FileExistsError):
        create_keypair("laptop", tmp_path / "ssh")  # never overwritten
    with pytest.raises(ValueError):
        key_path("../../etc/passwd", tmp_path)


def test_every_change_the_door_makes_has_a_line_in_the_undo_file_it_writes():
    door = render_door("192.168.1.20", MAC_PUB)
    undo = door[door.index("echo @echo off"):door.index("> \"%HERE%envrec-undo.cmd\"")]
    for added, removed in (("add rule name=\"envrec-door-sshd\"", "delete rule name=\"envrec-door-sshd\""), ("sc config sshd start= demand", "sc config sshd start= disabled"),
                           ("net start sshd", "net stop sshd"), ('set rule name="OpenSSH-SSH-In-TCP" new enable=no', 'set rule name="OpenSSH-SSH-In-TCP" new enable=yes')):
        assert added in door and removed in undo, (added, removed)
    assert "administrators_authorized_keys" in undo and "authorized_keys" in undo and "sshd_config" in undo and "notmatch 'envrec-door'" in undo  # the tagged lines go from all three files



def _post(url, body):
    import urllib.request
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status
    except urllib.error.HTTPError as exc:
        return exc.code


def test_a_report_is_taken_once_only_from_the_machine_that_downloaded_the_file_and_its_address_is_the_connections():
    link, events = _link()
    url = link.start()
    report_url = url.rsplit("/", 1)[0] + "/report"
    assert _post(report_url, {"user": "x"}) == 403 and link.report is None  # nothing was downloaded yet: no report is accepted
    assert get(url)[0] == 200
    assert _post(report_url.replace(link.token, "w" * 32), {"user": "x"}) == 403
    assert _post(report_url, {"user": "owner<script>", "computer": "BOX-1", "os": "Some OS 1.0", "host_key": "ssh-ed25519 AAAA", "address": "10.9.9.9"}) == 200
    assert link.report == {"address": "127.0.0.1", "user": "ownerscript", "computer": "BOX-1", "os": "Some OS 1.0", "host_key": "ssh-ed25519 AAAA"}  # the address field the file sent is ignored; characters outside a plain set are dropped
    assert link.reported.is_set() and link.done.is_set() and link.outcome == "reported"
    assert [k for k, _ in events if k == "setup_link_reported"] and link.token not in json.dumps(events)



def test_the_door_file_has_no_bare_parenthesis_inside_its_opening_block_which_would_end_it_before_it_starts():
    """In a .cmd file a `)` inside an `if (...)` block closes the block. The first version had "(nothing else)" in an echo line and the window closed at once."""
    door = render_door("192.168.1.20", MAC_PUB).replace("\r\n", "\n")
    block = door[door.index('if "%~1"=="" ('):door.index("set \"TARGET_USER=")].splitlines()
    inner = block[1:-1]  # between `if ... (` and the closing `)`
    for line in inner:
        if line.strip().startswith("echo"):
            text = line.replace("^(", "").replace("^)", "")
            assert "(" not in text and ")" not in text, line



def test_the_progress_beacons_of_the_file_are_recorded_in_order_only_after_the_download_and_with_the_token():
    link, events = _link()
    url = link.start()
    base = url.rsplit("/", 1)[0]
    assert get(base + "/step/s0")[0] == 404 and link.steps == []  # nothing downloaded yet
    get(url)
    assert get(base + "/step/s0")[0] == 200 and get(base + "/step/s1")[0] == 200 and get(base + "/step/failed")[0] == 200
    assert get(base.replace(link.token, "z" * 32) + "/step/s2")[0] == 404 and get(base + "/step/BAD!")[0] == 404
    assert link.steps == ["s0", "s1", "failed"] and [i["step"] for k, i in events if k == "setup_link_step"] == ["s0", "s1", "failed"] and link.token not in json.dumps(events)
    link.stop()


# ---- the door's events that happen after the last tool call are in the log --------------------------------------------------------------------

def _serve_ctx(tmp_path, ttl_s):
    """A real store, a real target, and the real `ssh_door_serve` tool: the link is served and NO other tool runs afterwards."""
    import ipaddress

    from envrec.contracts import Target, TargetKind
    from envrec.discovery import Salts
    from envrec.discovery.types import this_platform
    from envrec.store import Store
    from envrec.tools import ToolContext
    ssh_dir = tmp_path / "ssh"
    pair = create_keypair("lan-peer", ssh_dir)  # the key the door file will carry (public half only)
    assert pair.startswith("ssh-ed25519")
    store = Store(tmp_path / "store")
    target = store.put_target(Target(kind=TargetKind.host, ring=1, handle="door-test"))
    params = {"ssh_dir": ssh_dir, "lan_address": "127.0.0.1", "door_name": "lan-peer", "link_options": {"check": ipaddress.IPv4Address, "ttl_s": ttl_s}, "show": lambda m: None}
    return ToolContext(None, this_platform(), store, target, Salts(), params, lambda text: True), store


def _link_kinds(store):
    return [e.type for e in store.events() if e.type.startswith("setup_link_")]


def test_a_link_that_expires_with_no_later_tool_call_leaves_the_expired_event_in_the_log(tmp_path):
    import time

    from envrec.tools.ctx import run_write_tool
    from envrec.tools.registry import get_tool
    ctx, store = _serve_ctx(tmp_path, ttl_s=0.6)
    with store:
        res = run_write_tool(get_tool("ssh_door_serve"), ctx, {})
        assert res.ok, res.limitations
        assert _link_kinds(store) == ["setup_link_started"]  # nothing else has happened yet
        end = time.monotonic() + 8
        while "setup_link_expired" not in _link_kinds(store) and time.monotonic() < end:  # no tool runs in this window; only the link's own thread can write
            time.sleep(0.1)
        kinds = _link_kinds(store)
        assert kinds == ["setup_link_started", "setup_link_expired"], kinds
        expired = next(e for e in store.events() if e.type == "setup_link_expired")
        assert expired.effective_tag == "GUARD"
        ctx.params["run_state"]["door"]["link"].stop()


def test_a_link_still_being_served_when_the_run_ends_is_shut_down_and_the_shutdown_is_recorded(tmp_path):
    from envrec.tools.bridge import RegistryAdapter
    from envrec.tools.ctx import run_write_tool
    from envrec.tools.registry import get_tool
    ctx, store = _serve_ctx(tmp_path, ttl_s=60)
    adapter = RegistryAdapter(params={})
    adapter.run_state = ctx.params.setdefault("run_state", {})
    with store:
        assert run_write_tool(get_tool("ssh_door_serve"), ctx, {}).ok
        link = ctx.params["run_state"]["door"]["link"]
        adapter.close()  # what the loop does when the run ends
        assert _link_kinds(store) == ["setup_link_started", "setup_link_shutdown"], _link_kinds(store)
        shut = next(e for e in store.events() if e.type == "setup_link_shutdown")
        assert shut.payload["reason"] == "the run ended" and shut.payload["served"] is False and shut.payload["reported"] is False
        assert link.done.is_set() and link._httpd is None  # the port is closed
        adapter.close()  # closing twice records nothing more
        assert _link_kinds(store) == ["setup_link_started", "setup_link_shutdown"]
