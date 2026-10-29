"""D6: the adb, serial and TCP routes. Same rules as ssh: only entries of targets.yaml, only their allowed transports, nothing opens before the check; serial and TCP send
only what the entry's `allow_send` permits."""

from __future__ import annotations

import pytest

from envrec.mission import AdbBlock, EntryLimits, SerialBlock, TargetConfig, TargetEntry, TcpBlock, Transport
from envrec.remote.runner import RemoteRunner, TargetNotAllowed
from envrec.remote.templates import TemplateError


def entry(id="phone", transports=(Transport.adb_usb,), **kw):
    base = dict(id=id, name=id, host="127.0.0.1", allowed_transports=list(transports), limits=EntryLimits(max_disk_gb=4, max_model_params="1B"), owner_confirmed=True)
    return TargetEntry(**{**base, **kw})


class Rec:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, stdin, timeout):
        self.calls.append(argv)
        return 0, b"ok", b""


def test_adb_runs_only_a_fixed_template_through_adb_shell_for_the_listed_serial():
    rec = Rec()
    r = RemoteRunner(TargetConfig(targets=[entry(adb_usb=AdbBlock(serial="R5CT1234ABC"))]), exec_fn=rec)
    res = r.run("phone", Transport.adb_usb, "uname_m")
    assert res.ok and rec.calls == [["adb", "-s", "R5CT1234ABC", "shell", "uname -m"]]
    with pytest.raises(TemplateError):
        r.run("phone", Transport.adb_usb, "no_such_template")
    with pytest.raises(TargetNotAllowed):
        r.run("phone", Transport.ssh, "uname_m")  # ssh is not allowed for this entry
    with pytest.raises(TargetNotAllowed):
        r.run("tablet", Transport.adb_usb, "uname_m")  # not in targets.yaml
    assert len(rec.calls) == 1


def test_a_serial_entry_without_allow_send_only_listens():
    seen = []
    r = RemoteRunner(TargetConfig(targets=[entry("board", [Transport.serial], serial=SerialBlock(path="/dev/ttyUSB0", baud=9600))]),
                     serial_fn=lambda path, baud, plan, t: (seen.append((path, baud, plan)), (0, b'{"passive": "hello"}'))[1])
    res = r.serial_listen("board")
    path, baud, plan = seen[0]
    assert (path, baud) == ("/dev/ttyUSB0", 9600) and plan["probes"] == [] and plan["bauds"] == [9600]  # nothing to send
    assert res.ok and "send=nothing" in res.argv_shown


def test_allow_send_permits_only_the_fixed_identification_table():
    seen = []
    r = RemoteRunner(TargetConfig(targets=[entry("board", [Transport.serial], serial=SerialBlock(path="/dev/ttyUSB0", allow_send=True))]),
                     serial_fn=lambda path, baud, plan, t: (seen.append(plan), (0, b"{}"))[1])
    r.serial_listen("board")
    assert [p["send"] for p in seen[0]["probes"]] == ["\r\n", "AT\r\n", "*IDN?\r\n", "$I\r\n"]


def test_serial_and_tcp_refuse_before_anything_opens_when_the_entry_does_not_allow_them():
    calls = []
    r = RemoteRunner(TargetConfig(targets=[entry("box", [Transport.ssh], ssh=None) if False else entry("phone", [Transport.adb_usb], adb_usb=AdbBlock(serial="ABC12345"))]),
                     serial_fn=lambda *a: (calls.append(a), (0, b""))[1], tcp_fn=lambda *a: (calls.append(a), (0, b""))[1])
    with pytest.raises(TargetNotAllowed):
        r.serial_listen("phone")
    with pytest.raises(TargetNotAllowed):
        r.tcp_read("phone")
    with pytest.raises(TargetNotAllowed):
        r.serial_listen("ghost")
    assert calls == [] and r.attempts == []


def test_tcp_reads_what_the_endpoint_volunteers_and_sends_idn_only_if_allowed():
    got = []
    cfg = lambda allow: TargetConfig(targets=[entry("scope", [Transport.tcp], tcp=TcpBlock(host="192.168.1.50", port=5025, allow_send=allow))])  # noqa: E731
    quiet = RemoteRunner(cfg(False), tcp_fn=lambda h, p, send, t: (got.append((h, p, send)), (0, b"banner"))[1])
    quiet.tcp_read("scope")
    loud = RemoteRunner(cfg(True), tcp_fn=lambda h, p, send, t: (got.append((h, p, send)), (0, b"ACME,SCOPE,1,2"))[1])
    res = loud.tcp_read("scope")
    assert got == [("192.168.1.50", 5025, None), ("192.168.1.50", 5025, b"*IDN?\r\n")] and res.stdout.startswith("ACME")
