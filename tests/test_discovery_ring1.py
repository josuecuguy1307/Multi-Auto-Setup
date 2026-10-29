"""D4: attached hardware. USB resolved through usb.ids, serial enumeration, the serial probe against pty fake devices, Bluetooth."""

from __future__ import annotations

import asyncio
import fcntl
import io
import json
import os
import pty
import sys
import threading
import time
import tty
import types
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from envrec.contracts import FactStatus, Target, TargetKind
from envrec.discovery import LocalCollector, Reason, ReplayCollector, Salts, tables
from envrec.fingerprint import UsbIds
from envrec.skills.discover import get_adapter, run_adapter
from envrec.store import Store
from envrec.tools import ToolContext, get_tool
from envrec.tools.builtin import hardware

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "fixtures" / "discovery"


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "s") as s:
        yield s


@pytest.fixture(autouse=True)
def sample_usb_ids():
    tables.override("usb_ids", UsbIds.from_file(ROOT / "fixtures" / "samples" / "usb_ids_sample.txt"))
    yield
    tables.override("usb_ids", None)


def go(store, adapter, d, plat, name="h"):
    host = store.put_target(Target(kind=TargetKind.host, ring=1, handle=name))
    r = run_adapter(get_adapter(adapter), ReplayCollector(d, plat), store, host, salts=Salts(), platform=plat)
    facts = {f.key: f for f in store.current_facts(host.id)}
    return r, facts, [f for k, f in facts.items() if ".entity." in k]


# ---- USB --------------------------------------------------------------------------------------------------------------------

def test_real_mac_with_nothing_plugged_in_reports_zero_devices_not_an_error(store):
    r, f, ents = go(store, "usb_devices", FIX / "usb_devices" / "macos", "macos")
    assert r.status == "ran" and f["usb.device_count"].value == 0 and ents == []


def test_attached_devices_are_resolved_through_usb_ids_and_unknowns_stay_unknown(store):
    r, f, ents = go(store, "usb_devices", FIX / "usb_devices" / "macos_synthetic", "macos")
    assert f["usb.device_count"].value == 4
    by_vid = {e.value["attrs"]["vid"]: e for e in ents}
    ch340 = by_vid["1a86"]
    assert ch340.value["resolved"] is True and "QinHeng" in (ch340.value["attrs"]["vendor"] or "") and ch340.status == FactStatus.tested
    mystery = by_vid["ffff"]
    assert mystery.value["resolved"] is False and mystery.status == FactStatus.hypothesis and r.unknowns >= 1
    assert "SYN-0001" not in json.dumps([e.value for e in ents])  # serial numbers are identifiers: hashed, never stored


def test_linux_lsusb_synthetic_skips_the_root_hub(store):
    _, f, ents = go(store, "usb_devices", FIX / "usb_devices" / "linux", "linux")
    assert f["usb.device_count"].value == 2 and {e.value["attrs"]["vid"] for e in ents} == {"1a86", "ffff"}


def test_without_the_real_table_lookups_are_unavailable_not_guessed(store):
    tables.override("usb_ids", None)
    tables._load.cache_clear()
    from envrec.discovery.commands import data_dir
    if (data_dir() / "downloads" / "usb.ids").is_file():
        pytest.skip("the real usb.ids is present on this machine")
    r, f, ents = go(store, "usb_devices", FIX / "usb_devices" / "macos_synthetic", "macos")
    assert f["usb.ids_table"].status == FactStatus.unknown and all(not e.value["resolved"] for e in ents)


# ---- serial enumeration -------------------------------------------------------------------------------------------------------

def test_bluetooth_audio_ports_and_debug_consoles_are_counted_not_listed(store):
    _, f, ents = go(store, "serial_ports", FIX / "serial_ports" / "macos", "macos")
    assert f["serial.port_count"].value == 0 and f["serial.excluded_count"].value >= 4 and ents == []


def test_usb_serial_ports_are_listed_and_linked(store):
    _, f, ents = go(store, "serial_ports", FIX / "serial_ports" / "linux", "linux")
    names = {e.value["attrs"]["name"] for e in ents}
    assert names == {"ttyACM0", "ttyUSB0"} and "ttyS0" not in names  # the built-in UART is not a device we list


# ---- serial probe against pty fake devices -------------------------------------------------------------------------------------

class FakeDevice:
    """A pty whose master side behaves like a serial device and records every byte written to it."""

    def __init__(self, kind: str):
        self.kind, self.master, self.slave = kind, *pty.openpty()
        tty.setraw(self.master)
        self.port = os.ttyname(self.slave)
        self.received = b""
        self.stop = False
        self.t = threading.Thread(target=self.loop, daemon=True)
        self.t.start()

    def loop(self):
        import select
        last = 0.0
        while not self.stop:
            r, _, _ = select.select([self.master], [], [], 0.05)
            if r:
                data = os.read(self.master, 256)
                self.received += data
                if self.kind == "modem" and data.endswith(b"AT\r\n"):
                    os.write(self.master, b"OK\r\n")
                if self.kind == "scpi" and b"*IDN?" in data:
                    os.write(self.master, b"ACME,Probe-9,SN0001,1.0\r\n")
            now = time.monotonic()
            if now - last > 0.15:
                last = now
                if self.kind == "nmea":
                    os.write(self.master, b"$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47\r\n")
                elif self.kind == "banner":
                    os.write(self.master, b"ACME Gizmo firmware 1.2 ready\r\n")

    def close(self):
        self.stop = True
        self.t.join(timeout=1)
        os.close(self.master)
        os.close(self.slave)


@pytest.fixture
def fast_plan(monkeypatch):
    plan = {"listen_s": 0.4, "reply_wait_s": 0.4, "bauds": [9600],
            "probes": [{"name": "newline", "send": "\r\n"}, {"name": "at", "send": "AT\r\n"}, {"name": "scpi_idn", "send": "*IDN?\r\n"}, {"name": "grbl_info", "send": "$I\r\n"}]}
    monkeypatch.setattr(hardware, "serial_plan", lambda: plan)
    return plan


def probe(store, dev, plan=None):
    host = store.put_target(Target(kind=TargetKind.host, ring=1, handle="probe-host"))
    r = run_adapter(get_adapter("serial_probe"), LocalCollector(), store, host, salts=Salts(), params={"port": dev.port}, approve=lambda text: True)
    ents = [f for f in store.current_facts(host.id) if ".entity." in f.key]
    return r, ents


@pytest.mark.skipif(sys.platform == "win32", reason="pty fake devices are POSIX only")
def test_a_modem_like_device_answers_the_at_query(store, fast_plan):
    dev = FakeDevice("modem")
    try:
        r, ents = probe(store, dev)
    finally:
        dev.close()
    assert r.status == "ran" and len(ents) == 1
    a = ents[0].value["attrs"]
    assert a["responded"] and a["sent"][-1] == "at" and a["protocol_guess"] == "at" and a["banner"].strip() == "OK"


@pytest.mark.skipif(sys.platform == "win32", reason="pty fake devices are POSIX only")
def test_a_streaming_device_is_identified_by_listening_and_nothing_is_sent(store, fast_plan):
    dev = FakeDevice("nmea")
    try:
        _, ents = probe(store, dev)
    finally:
        dev.close()
    a = ents[0].value["attrs"]
    assert a["heard_unprompted"] and a["protocol_guess"] == "nmea" and a["sent"] == []
    assert dev.received == b""  # it never wrote a byte to a device that was already talking


@pytest.mark.skipif(sys.platform == "win32", reason="pty fake devices are POSIX only")
def test_a_banner_printing_device_is_recorded_without_a_protocol_guess_being_invented(store, fast_plan):
    dev = FakeDevice("banner")
    try:
        _, ents = probe(store, dev)
    finally:
        dev.close()
    a = ents[0].value["attrs"]
    assert "Gizmo" in a["banner"] and ents[0].value["resolved"] is False and a["protocol_guess"] is None


@pytest.mark.skipif(sys.platform == "win32", reason="pty fake devices are POSIX only")
def test_a_silent_device_gets_only_table_entries_and_stays_unknown(store, fast_plan):
    dev = FakeDevice("silent")
    try:
        _, ents = probe(store, dev)
    finally:
        dev.close()
    assert dev.received == b"".join(p["send"].encode() for p in fast_plan["probes"])  # every table entry, once each, in order
    a = ents[0].value["attrs"]
    assert a["responded"] is False and ents[0].value["resolved"] is False and set(a["sent"]) == {"newline", "at", "scpi_idn", "grbl_info"}


@pytest.mark.skipif(sys.platform == "win32", reason="pty fake devices are POSIX only")
def test_the_probe_stops_at_the_first_reply(store, fast_plan):
    dev = FakeDevice("scpi")
    try:
        _, ents = probe(store, dev)
    finally:
        dev.close()
    assert ents[0].value["attrs"]["sent"] == ["newline", "at", "scpi_idn"]
    assert dev.received.endswith(b"*IDN?\r\n") and b"$I" not in dev.received


@pytest.mark.skipif(sys.platform == "win32", reason="pty fake devices are POSIX only")
def test_a_port_in_use_is_refused_not_stolen(store, fast_plan):
    dev = FakeDevice("silent")
    holder = os.open(dev.port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        host = store.put_target(Target(kind=TargetKind.host, ring=1, handle="busy"))
        r = run_adapter(get_adapter("serial_probe"), LocalCollector(), store, host, salts=Salts(), params={"port": dev.port}, approve=lambda text: True)
    finally:
        os.close(holder)
        dev.close()
    assert r.status == "denied" and "in use" in r.reason and dev.received == b""


def test_only_serial_device_paths_may_be_opened(store, fast_plan):
    host = store.put_target(Target(kind=TargetKind.host, ring=1, handle="x"))
    for bad in ("/etc/passwd", "/dev/disk0", "/dev/null", "relative", "/dev/cu.usbserial-1/../../etc/passwd"):
        r = run_adapter(get_adapter("serial_probe"), LocalCollector(), store, host, salts=Salts(), params={"port": bad}, approve=lambda text: True)
        assert r.status == "denied", bad


def test_the_probe_table_is_the_only_source_of_bytes_and_needs_an_approval_text():
    plan = hardware.serial_plan()
    assert [p["send"] for p in plan["probes"]] == ["\r\n", "AT\r\n", "*IDN?\r\n", "$I\r\n"]
    t = get_tool("serial_identify")
    txt = t.approval(ToolContext(collector=LocalCollector(), platform="linux"), port="/dev/cu.usbserial-1")
    assert "/dev/cu.usbserial-1" in txt and "*IDN?" in txt and "exclusively" in txt
    assert t.tier == "read" and get_adapter("serial_probe").tier == "read" and get_adapter("serial_probe").mode == "menu"


def test_probe_replay_fixture_parses(store):
    r, f, ents = go(store, "serial_probe", FIX / "serial_probe" / "macos", "macos")
    assert ents and ents[0].value["attrs"]["protocol_guess"] == "nmea" and ents[0].status == FactStatus.hypothesis


# ---- Bluetooth ------------------------------------------------------------------------------------------------------------------

def test_paired_devices_from_real_mac_output_are_owned_and_addresses_are_not_stored(store):
    r, f, ents = go(store, "ble_paired", FIX / "ble_paired" / "macos", "macos")
    assert f["ble.paired_count"].value == len(ents) >= 1 and f["ble.controller_state"].value in ("on", "off")
    assert all(e.value["owned"] == "owned" for e in ents)
    blob = json.dumps([e.value for e in ents])
    import re
    assert not re.search(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", blob)


def test_ambient_advertisers_are_counted_and_classified_with_no_names_or_addresses(store):
    from envrec.fingerprint import BluetoothCompanies
    tables.override("bt_companies", BluetoothCompanies({76: "Apple, Inc.", 117: "Samsung Electronics Co. Ltd."}, "t"))
    try:
        r, f, ents = go(store, "ble_scan", FIX / "ble_scan" / "macos", "macos")
    finally:
        tables.override("bt_companies", None)
    assert f["ble.nearby_count"].value == 3 and len(ents) == 3 and all(e.value["owned"] == "ambient" for e in ents)
    assert f["ble.nearby_by_class"].value["Apple, Inc."] == 1
    unresolved = [e for e in ents if not e.value["resolved"]]
    assert len(unresolved) == 1 and unresolved[0].status == FactStatus.hypothesis
    assert "identifiers are hashed" in f["ble.nearby_count"].evidence[0].limitations[0]


def test_ble_scan_hashes_addresses_inside_the_scanning_process(monkeypatch):
    """The scanner process prints only salted hashes: the raw address never leaves it."""
    from envrec.discovery import _ble_scan

    class Dev:
        address = "AA:BB:CC:DD:EE:FF"
        name = "Example AirPods"

    class Adv:
        rssi, tx_power, manufacturer_data, service_uuids = -50, 4, {76: b"\x01"}, ["0000180f-0000-1000-8000-00805f9b34fb"]

    class Scanner:
        @staticmethod
        async def discover(timeout, return_adv):
            return {"x": (Dev(), Adv())}
    monkeypatch.setitem(sys.modules, "bleak", types.SimpleNamespace(BleakScanner=Scanner))
    buf = io.StringIO()
    with redirect_stdout(buf):
        asyncio.run(_ble_scan.main(0.1, "salt-1"))
    out = buf.getvalue()
    assert "AA:BB:CC" not in out and "AirPods" not in out and "Example" not in out
    rec = json.loads(out)[0]
    assert rec["id"] == _ble_scan.short_hash("AA:BB:CC:DD:EE:FF", "salt-1") and rec["company"] == [76]
    other = io.StringIO()
    with redirect_stdout(other):
        asyncio.run(_ble_scan.main(0.1, "salt-2"))
    assert json.loads(other.getvalue())[0]["id"] != rec["id"]  # a new salt per run: a stranger's device cannot be followed between runs


def test_a_denied_bluetooth_scan_is_recorded_with_the_settings_step(store, monkeypatch):
    from envrec.discovery import Capture
    monkeypatch.setattr(hardware, "ble_collect", lambda seconds, salt, approve=None: Capture("scan", "x", b"", 134, "local", "macos", Reason.permission, "System Settings > Privacy & Security > Bluetooth"))

    class Denied:
        origin, platform = "local", "macos"

        def run(self, cmd):
            return Capture(cmd.name, "x", b"", 1, "local", "macos", Reason.unavailable, "x")

        def read_file(self, name, path):
            return Capture(name, "x", b"", 134, "local", "macos", Reason.permission, "System Settings > Privacy & Security > Bluetooth")
    host = store.put_target(Target(kind=TargetKind.host, ring=1, handle="bt"))

    r = run_adapter(get_adapter("ble_scan"), Denied(), store, host, salts=Salts(), platform="macos")
    assert r.status == "permission" and "Privacy & Security > Bluetooth" in r.reason


def test_thunderbolt_real_output(store):
    _, f, _ = go(store, "hw_buses", FIX / "hw_buses" / "macos", "macos")
    assert f["hw.thunderbolt_ports"].value >= 1 and f["hw.thunderbolt_devices_connected"].value == 0


def test_serial_identify_also_collects_what_the_device_said_as_plain_text(tmp_path):
    from envrec.tools import ToolContext as TC, get_tool, run_tool
    d = tmp_path / "rec"
    d.mkdir()
    (d / "probe.out").write_text(json.dumps({"port": "/dev/x", "passive": "", "sent": ["scpi_idn"], "reply": "KRL3 R4.2 b=87 t=342 m=2\r\n", "baud": 9600}))
    res = run_tool(get_tool("serial_identify"), TC(collector=ReplayCollector(d, "linux"), platform="linux"), {})
    assert [c.name for c in res.captures] == ["probe", "heard"] and res.captures[1].text == "KRL3 R4.2 b=87 t=342 m=2\r\n"
