import json
from pathlib import Path

import pytest

from envrec.contracts import Fact, FactStatus, Hypothesis, HypothesisOutcome, Target, TargetKind
from envrec.fingerprint import (
    BluetoothCompanies,
    BluetoothServices,
    IeeeOui,
    MdnsServiceTypes,
    UsbIds,
    load_available,
    match_serial,
    serial_protocol_result,
    single_lookup_result,
    usb_lookup_result,
)
from envrec.fingerprint.serial_signatures import crc16_modbus, modbus_rtu_ok, nmea_checksum_ok
from envrec.scoring import fingerprint_stats
from envrec.store import Store

ROOT = Path(__file__).resolve().parent.parent
DOWNLOADS = ROOT / "data" / "downloads"

USB_SAMPLE = """\
#	List of USB ID's
# Version: 2026.01.01
# Date:    2026-01-01 00:00:00

1a86  QinHeng Electronics
	55d4  CH9102 UART Bridge
	7523  CH340 serial converter
0403  Future Technology Devices International, Ltd
	6001  FT232 Serial (UART) IC
		00  an interface line, ignored
C 00  (Defined at Interface level)
	01  Looks like a product but belongs to a class section
"""

OUI_SAMPLE = '''Registry,Assignment,Organization Name,Organization Address
MA-L,286FB9,"Nokia Shanghai Bell Co., Ltd.","No.388 Ning Qiao Road"
MA-L,38E2CA,Katun Corporation,7760 France Ave S
MA-M,70B3D5A,Example MA-M Org,Somewhere
MA-S,70B3D5A1B,Tiny Org,Elsewhere
'''

BT_COMPANIES = json.dumps([{"code": 76, "name": "Apple, Inc."}, {"code": 6, "name": "Microsoft"}])
BT_SERVICES = json.dumps([{"name": "Battery Service", "identifier": "org.bluetooth.service.battery_service",
                           "uuid": "180F", "source": "gss"}])
IANA_SAMPLE = """\
Service Name,Port Number,Transport Protocol,Description,Assignee
ipp,631,tcp,Internet Printing Protocol,IESG
http,80,tcp,World Wide Web HTTP,IETF
http,80,udp,World Wide Web HTTP,IETF
,0,tcp,Reserved,
"""


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "s") as s:
        yield s


@pytest.fixture
def target(store):
    return store.put_target(Target(kind=TargetKind.usb, ring=1, handle="replay:t"))


# ---- usb.ids ----------------------------------------------------------------------------------

def test_usb_vendor_and_product_lookup():
    ids = UsbIds.from_text(USB_SAMPLE)
    assert ids.data_version == "2026.01.01"
    (v,) = ids.lookup("1a86")
    assert v.level == "vendor" and "QinHeng" in v.entry and v.source == "usb.ids"
    both = ids.lookup("0x1A86:0x7523")
    assert [c.level for c in both] == ["vendor", "product"] and "CH340" in both[1].entry


def test_usb_vendor_only_match_has_no_product_candidate():
    assert [c.level for c in UsbIds.from_text(USB_SAMPLE).lookup("1a86:9999")] == ["vendor"]


def test_usb_unlisted_vendor_and_class_sections():
    ids = UsbIds.from_text(USB_SAMPLE)
    assert ids.lookup("ffff") == []
    assert ids.vendor("0001") is None
    assert ids.product("C", "01") is None  # tabbed lines under a class section are not products


# ---- OUI, Bluetooth, mDNS -----------------------------------------------------------------------

def test_oui_longest_prefix_wins():
    oui = IeeeOui.from_text(OUI_SAMPLE)
    assert "Nokia" in oui.lookup("28:6F:B9:12:34:56")[0].entry
    assert "Tiny Org" in oui.lookup("70-B3-D5-A1-B2-C3")[0].entry  # MA-S beats MA-M
    assert "MA-M" in oui.lookup("70b3d5a0ffff")[0].entry
    assert oui.lookup("aa:bb:cc:dd:ee:ff") == []
    assert oui.lookup("28:6F") == []  # under 6 hex digits is not an assignment


def test_bluetooth_company_and_service_lookups():
    companies = BluetoothCompanies.from_text(BT_COMPANIES)
    assert "Apple" in companies.lookup("0x004C")[0].entry and "Apple" in companies.lookup("004c")[0].entry
    assert companies.lookup("ffff") == [] and companies.lookup("") == []
    services = BluetoothServices.from_text(BT_SERVICES)
    for ident in ("180f", "0x180F", "0000180f-0000-1000-8000-00805f9b34fb"):
        assert "Battery" in services.lookup(ident)[0].entry
    assert services.lookup("1234") == []


def test_mdns_service_types():
    mdns = MdnsServiceTypes.from_text(IANA_SAMPLE)
    assert "Internet Printing" in mdns.lookup("_ipp._tcp.local.")[0].entry
    assert len(mdns.lookup("_http._tcp")) == 1 and len(mdns.lookup("_http._udp")) == 1
    assert mdns.lookup("_nothing._tcp") == [] and mdns.lookup("garbage") == []


# ---- serial signatures --------------------------------------------------------------------------

def nmea(body):
    x = 0
    for ch in body:
        x ^= ord(ch)
    return f"${body}*{x:02X}"


def test_nmea_checksum_is_verified_not_just_pattern_matched():
    good = nmea("GPGLL,4916.45,N,12311.12,W,225444,A")
    assert nmea_checksum_ok(good)
    top = match_serial(good + "\r\n")[0]
    assert top.protocol == "nmea" and top.confidence == 0.95
    bad = good[:-2] + ("00" if not good.endswith("00") else "01")
    assert match_serial(bad)[0].confidence == 0.5 and "BAD" in match_serial(bad)[0].evidence


def test_scpi_idn_reply_needs_the_command_for_high_confidence():
    reply = "KEYSIGHT TECHNOLOGIES,34461A,MY53200000,A.03.01\r\n"
    assert match_serial(reply, sent="*IDN?")[0].confidence == 0.9
    assert match_serial(reply)[0].confidence == 0.4
    assert match_serial(reply)[0].protocol == "scpi"


def test_at_replies():
    assert match_serial("+CSQ: 20,0\r\nOK\r\n", sent="AT+CSQ")[0].protocol == "at"
    assert match_serial("OK")[0].confidence == 0.2  # a bare OK is weak evidence


def test_modbus_rtu_crc_and_ascii_lrc():
    payload = bytes([0x01, 0x03, 0x00, 0x00, 0x00, 0x02])
    frame = payload + crc16_modbus(payload).to_bytes(2, "little")
    assert modbus_rtu_ok(frame) and match_serial(frame)[0].protocol == "modbus"
    assert not modbus_rtu_ok(frame[:-1] + bytes([frame[-1] ^ 0xFF]))
    raw = bytes([0x01, 0x03, 0x00, 0x00, 0x00, 0x02])
    ascii_frame = ":" + (raw + bytes([(-sum(raw)) & 0xFF])).hex().upper() + "\r\n"
    assert match_serial(ascii_frame)[0].protocol == "modbus"


def test_gcode_banners_and_ok_replies():
    assert match_serial("Grbl 1.1h ['$' for help]")[0].protocol == "gcode"
    assert match_serial("ok", sent="G28")[0].protocol == "gcode"
    assert match_serial("ok") == []  # without a G-code command, 'ok' proves nothing


def test_unrecognized_bytes_match_nothing():
    assert match_serial(b"\x02\x07zz??\xff") == []
    assert match_serial("") == []


# ---- facts: vendor is not model ---------------------------------------------------------------

def test_vendor_only_match_leaves_the_model_unknown(store, target):
    result = usb_lookup_result(store, target, UsbIds.from_text(USB_SAMPLE), "1a86", "9999", "evt_1")
    facts = {f.key: f for f in result.facts}
    assert facts["usb.vendor_name"].status == FactStatus.hypothesis
    assert facts["usb.vendor_name"].value == "QinHeng Electronics"
    assert facts["usb.product_name"].status == FactStatus.unknown
    assert "only the vendor matched" in result.unknowns[0].question
    assert result.coverage.status == "partial"


def test_full_match_gives_two_separate_hypotheses_with_hash_checked_evidence(store, target):
    result = usb_lookup_result(store, target, UsbIds.from_text(USB_SAMPLE), "1a86", "7523")
    facts = {f.key: f for f in result.facts}
    assert facts["usb.product_name"].value == "CH340 serial converter"
    for f in facts.values():
        assert f.status == FactStatus.hypothesis  # a list match never exceeds hypothesis
        assert "QinHeng" in store.read_raw(f.evidence[0].raw_ref).decode()
        assert f.evidence[0].provider == "usb.ids"
        assert any("guess" in lim for lim in f.evidence[0].limitations)


def test_unlisted_vendor_is_unknown_for_both(store, target):
    result = usb_lookup_result(store, target, UsbIds.from_text(USB_SAMPLE), "ffff", "0001")
    assert all(f.status == FactStatus.unknown for f in result.facts) and len(result.unknowns) == 2


def test_single_lookup_and_serial_results(store, target):
    oui = IeeeOui.from_text(OUI_SAMPLE)
    r = single_lookup_result(store, target, "net.vendor_hint", oui.lookup("28:6f:b9:00:00:01"), "lookup_oui")
    assert r.facts[0].value.startswith("Nokia") and r.facts[0].status == FactStatus.hypothesis
    miss = single_lookup_result(store, target, "net.vendor_hint", [], "lookup_oui")
    assert miss.facts[0].status == FactStatus.unknown

    hit = serial_protocol_result(store, target, "Grbl 1.1h ['$' for help]")
    assert hit.facts[0].value == "gcode" and hit.facts[0].status == FactStatus.hypothesis
    none = serial_protocol_result(store, target, b"\x01\x02\x03")
    assert none.facts[0].status == FactStatus.unknown and none.unknowns


# ---- stats: matched / confirmed / wrong per source -------------------------------------------

def test_fingerprint_stats_count_per_source(store):
    for source, outcome in [("usb.ids", HypothesisOutcome.confirmed), ("usb.ids", HypothesisOutcome.contradicted),
                            ("usb.ids", HypothesisOutcome.pending), ("serial.signatures", HypothesisOutcome.confirmed)]:
        store.put_hypothesis(Hypothesis(target_id="t", claim="c", rung=3, source=source, outcome=outcome))
    store.put_hypothesis(Hypothesis(target_id="t", claim="no source", rung=1))
    stats = fingerprint_stats(store)
    assert (stats["usb.ids"].matched, stats["usb.ids"].confirmed, stats["usb.ids"].wrong,
            stats["usb.ids"].pending) == (3, 1, 1, 1)
    assert stats["serial.signatures"].confirmed == 1 and set(stats) == {"usb.ids", "serial.signatures"}


# ---- real downloaded files (skipped when absent) ----------------------------------------------

def _need(name):
    if not (DOWNLOADS / name).is_file():
        pytest.skip(f"{name} not downloaded; run scripts/fetch_data.py")


@pytest.mark.realdata
def test_real_usb_ids():
    _need("usb.ids")
    ids = UsbIds.from_file(DOWNLOADS / "usb.ids")
    assert "linux foundation" in ids.vendor("1d6b").lower()
    assert "qinheng" in ids.vendor("1a86").lower() and ids.data_version[:2] == "20"


@pytest.mark.realdata
def test_real_oui():
    _need("oui.csv")
    assert "cisco" in IeeeOui.from_file(DOWNLOADS / "oui.csv").lookup("00:00:0c:11:22:33")[0].entry.lower()


@pytest.mark.realdata
def test_real_bluetooth_numbers():
    _need("bt_company_ids.json")
    _need("bt_service_uuids.json")
    assert "apple" in BluetoothCompanies.from_file(DOWNLOADS / "bt_company_ids.json").lookup("004c")[0].entry.lower()
    assert "battery" in BluetoothServices.from_file(DOWNLOADS / "bt_service_uuids.json").lookup("180f")[0].entry.lower()


@pytest.mark.realdata
def test_real_iana_services():
    _need("iana_services.csv")
    hits = MdnsServiceTypes.from_file(DOWNLOADS / "iana_services.csv").lookup("_http._tcp")
    assert hits and "http" in hits[0].entry.lower()


@pytest.mark.realdata
def test_load_available_finds_whatever_was_downloaded():
    assert set(load_available(DOWNLOADS)) <= {"usb", "oui", "ble_company", "ble_service", "mdns"}
    assert load_available(ROOT / "nothing-here") == {}
