"""envrec Helper: the whitelist, the nonce, timeouts, typed unknowns, and the privacy gate still applied to what it returns. All with fakes: no app is launched."""
import hashlib
import json
import os
import stat

import pytest

from envrec import helper
from envrec.contracts import Target, TargetKind
from envrec.discovery import Salts
from envrec.discovery.types import Reason, this_platform
from envrec.helper import HelperClient, HelperError
from envrec.store import Store
from envrec.tools import ToolContext, get_tool, run_tool
from envrec.tools.builtin import hardware

RAW_ID = "9F2C4E10-AAAA-BBBB-CCCC-0123456789AB"


@pytest.fixture(autouse=True)
def private_helper_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ENVREC_HELPER_DIR", str(tmp_path))  # the approval log goes into the test's directory, never the real one


@pytest.fixture
def app(tmp_path):
    exe = tmp_path / "envrec-helper.app" / "Contents" / "MacOS" / "envrec-helper"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    return tmp_path / "envrec-helper.app"


class Fake:
    """A stand-in for the app: it checks the nonce and the whitelist the way main.swift does, and records what it was asked."""

    def __init__(self, reply=None, delay_error=None):
        self.seen, self.launched, self.reply, self.delay_error = [], [], reply, delay_error

    def launch(self, app, rundir):
        self.launched.append(rundir)
        self.nonce = open(os.path.join(rundir, "nonce")).read()
        open(os.path.join(rundir, "sock"), "w").close()  # the socket "appears"

    def roundtrip(self, sock, payload, timeout):
        if self.delay_error:
            raise self.delay_error
        msg = json.loads(payload)
        self.seen.append(msg)
        if msg.get("nonce") != self.nonce:
            return b'{"ok": false, "error": "refused", "detail": "bad nonce"}\n'
        return json.dumps(self.reply or {"ok": True, "data": []}).encode() + b"\n"


def client(app, fake, **kw):
    return HelperClient(app, launch=fake.launch, roundtrip=fake.roundtrip, start_wait_s=1, **{"approve": lambda text: True, **kw})


def test_only_the_four_whitelisted_requests_leave_the_client_and_nothing_is_launched_for_a_refused_one(app):
    fake = Fake()
    with client(app, fake) as c:
        for bad in (("run", {"command": "ls"}), ("ble_scan", {"path": "/etc/passwd"}), ("ble_scan", {"seconds": 999}), ("ble_scan", {"seconds": True}), ("ble_paired", {"seconds": 3})):
            with pytest.raises(HelperError) as exc:
                c.request(bad[0], **bad[1])
            assert exc.value.kind == "refused"
    assert fake.launched == [] and fake.seen == []
    assert set(helper.WHITELIST) == {"ble_scan", "ble_paired", "wifi_info", "mdns_browse"} and set(helper.TEXT_WHITELIST) == {"connect_paired", "audio_get", "audio_set"}


def test_the_swift_gate_and_the_python_gate_list_the_same_requests_and_ranges():
    src = helper.SOURCE.read_text()
    for name, args in helper.WHITELIST.items():
        assert f'"{name}"' in src
        for arg, (lo, hi) in args.items():
            assert f'"{arg}": ({lo:g}, {hi:g})' in src.replace(".0", "")
    for svc in helper.BONJOUR:
        assert f'"{svc}"' in src
    for forbidden in ("pair(", "IOBluetoothDevicePair", "connectPeripheral", "writeValue", "IOBluetoothSDPServiceRecord"):
        assert forbidden not in src
    # the only connect-capable call is `openConnection`, inside connectPaired, after the device was found among the ones macOS already has paired
    body = src[src.index("func connectPaired"):src.index("// ---- Audio output")]
    assert src.count("openConnection") == 1 and "openConnection" in body and body.index("pairedDevices()") < body.index("openConnection")
    for req, args in helper.TEXT_WHITELIST.items():
        assert f'"{req}"' in src
        for name, pattern in args.items():
            assert f'"{name}": "^{pattern}$"' in src or f'"{name}": "^({pattern})$"' in src, (req, name)


def test_the_session_directory_is_private_and_the_nonce_is_fresh_and_sent(app):
    f1, f2 = Fake(), Fake()
    with client(app, f1) as c:
        c.request("ble_paired")
        mode = stat.S_IMODE(os.stat(c.rundir).st_mode)
        nonce_mode = stat.S_IMODE(os.stat(os.path.join(c.rundir, "nonce")).st_mode)
        assert (mode, nonce_mode) == (0o700, 0o600) and f1.seen[0]["nonce"] == f1.nonce and len(f1.nonce) == 32
    with client(app, f2) as c:
        c.request("ble_paired")
    assert f1.nonce != f2.nonce and not os.path.exists(f1.launched[0])  # closed: the directory is gone


def test_a_wrong_nonce_is_refused_by_the_helper_and_surfaces_as_refused(app):
    fake = Fake()
    c = client(app, fake)
    c._start()
    c.nonce = "0" * 32
    with pytest.raises(HelperError) as exc:
        c.request("ble_paired")
    c.close()
    assert exc.value.kind == "refused" and "nonce" in exc.value.detail


def test_a_helper_that_never_answers_is_a_timeout_not_a_hang(app):
    with client(app, Fake(delay_error=TimeoutError())) as c:
        with pytest.raises(HelperError) as exc:
            c.request("ble_scan", seconds=3)
    assert exc.value.kind == "timeout"


def test_a_helper_that_does_not_open_its_socket_is_no_start(app):
    class NoSocket(Fake):
        def launch(self, app, rundir):
            self.launched.append(rundir)

    with client(app, NoSocket()) as c:
        with pytest.raises(HelperError) as exc:
            c.request("ble_paired")
    assert exc.value.kind == "no_start"


def test_a_missing_helper_is_typed_and_names_the_fix_command(tmp_path):
    with HelperClient(tmp_path / "nope.app") as c:
        with pytest.raises(HelperError) as exc:
            c.request("ble_paired")
    assert exc.value.kind == "missing" and "envrec helper build" in exc.value.detail


def test_a_permission_answer_from_the_helper_is_typed_permission(app):
    reply = {"ok": False, "error": "permission", "detail": "bluetooth: not allowed"}
    with client(app, Fake(reply)) as c:
        with pytest.raises(HelperError) as exc:
            c.request("ble_scan", seconds=2)
    assert exc.value.kind == "permission" and "not allowed" in exc.value.detail


def test_ble_records_hash_the_identifier_with_the_run_salt_and_drop_names(app):
    raw = [{"id": RAW_ID, "rssi": -52, "company": [76], "services": ["FD6F", "180F"], "tx": 12, "name": "Owner's AirPods"}]
    with client(app, Fake({"ok": True, "data": raw})) as c:
        recs = helper.ble_records(5, "salt-1", c)
    assert recs == [{"id": hashlib.sha256(("salt-1\x1f" + RAW_ID).encode()).hexdigest()[:8], "rssi": -52, "company": [76], "services": ["180f", "fd6f"], "tx": 12}]
    assert RAW_ID not in json.dumps(recs) and "AirPods" not in json.dumps(recs)


def test_the_scan_tool_routes_through_the_helper_after_an_os_abort_and_stores_no_raw_identifier(app, monkeypatch, tmp_path):
    class Done:
        returncode, stdout, stderr = 134, b"", b"abort: privacy violation"

    monkeypatch.setattr("subprocess.run", lambda *a, **k: Done())
    monkeypatch.setattr("importlib.util.find_spec", lambda name: object())
    monkeypatch.setattr("sys.platform", "darwin")
    fake = Fake({"ok": True, "data": [{"id": RAW_ID, "rssi": -60, "company": [76], "services": [], "tx": None}]})
    real = helper.HelperClient
    monkeypatch.setattr(helper, "HelperClient", lambda *a, **k: real(app, launch=fake.launch, roundtrip=fake.roundtrip, start_wait_s=1, approve=k.get("approve")))
    salts = Salts()
    with Store(tmp_path / "s") as st:
        t = st.put_target(Target(kind=TargetKind.host, ring=1, handle="this-machine"))
        ctx = ToolContext(__import__("envrec.discovery", fromlist=["LocalCollector"]).LocalCollector(), this_platform(), st, t, salts, {"seconds": 2}, lambda plan: True)
        res = run_tool(get_tool("ble_scan_passive"), ctx, {})
    assert res.ok and len(fake.seen) == 1 and fake.seen[0]["req"] == "ble_scan"
    body = res.captures[0].data.decode()
    assert RAW_ID not in body and json.loads(body)[0]["id"] == hashlib.sha256((salts.run + "\x1f" + RAW_ID).encode()).hexdigest()[:8]


def test_without_a_built_helper_an_os_abort_is_a_typed_unknown_with_the_fix_command(monkeypatch, tmp_path):
    class Done:
        returncode, stdout, stderr = 134, b"", b""

    monkeypatch.setattr("subprocess.run", lambda *a, **k: Done())
    monkeypatch.setattr("importlib.util.find_spec", lambda name: object())
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setenv("ENVREC_HELPER_DIR", str(tmp_path))
    cap = hardware.ble_collect(2, "s")
    assert cap.error == Reason.permission and "envrec helper build" in cap.detail and cap.data == b""


def test_on_linux_the_direct_probe_is_used_and_the_helper_is_never_touched(monkeypatch):
    class Done:
        returncode, stdout, stderr = 0, b"[]", b""

    monkeypatch.setattr("subprocess.run", lambda *a, **k: Done())
    monkeypatch.setattr("importlib.util.find_spec", lambda name: object())
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(helper, "HelperClient", lambda *a, **k: pytest.fail("the helper must not be used here"))
    cap = hardware.ble_collect(2, "s")
    assert cap.ok and cap.data == b"[]"


def test_build_without_swiftc_prints_the_xcode_command_and_runs_nothing():
    ran = []
    with pytest.raises(HelperError) as exc:
        helper.build(run=lambda *a, **k: ran.append(a), which=lambda n: None)
    assert exc.value.kind == "missing" and "xcode-select --install" in exc.value.detail and ran == []


def test_the_plist_declares_the_three_usage_texts_background_mode_and_the_bonjour_list():
    p = helper.info_plist()
    for k in ("NSBluetoothAlwaysUsageDescription", "NSLocalNetworkUsageDescription", "NSLocationWhenInUseUsageDescription"):
        assert p[k].count(".") == 1 and len(p[k]) > 40  # one sentence each
    assert p["LSUIElement"] is True and p["NSBonjourServices"] == helper.BONJOUR and p["CFBundleIdentifier"] == "com.envrec.helper"


def test_remove_deletes_the_app_and_clears_only_its_own_bundle_id(app, monkeypatch):
    monkeypatch.setenv("ENVREC_HELPER_DIR", str(app.parent))
    calls = []

    class Done:
        returncode = 0

    res = helper.remove(run=lambda argv, **k: calls.append(list(argv)) or Done())
    assert res == {"removed": True, "path": str(app), "privacy_entries_cleared": True} and not app.exists()
    assert ["tccutil", "reset", "All", "com.envrec.helper"] in calls
    assert all(c[0] in ("pkill", "tccutil") and (c[0] != "tccutil" or c[-1] == "com.envrec.helper") for c in calls)


def _build(tmp_path, approve):
    calls = []

    class Done:
        returncode, stdout, stderr = 0, "", ""

    def run(argv, **k):
        calls.append(list(argv))
        if argv[0] == "swiftc":
            open(argv[argv.index("-o") + 1], "w").write("bin")
        return Done()

    dest = tmp_path / "built" / "envrec-helper.app"
    helper.build(run=run, which=lambda n: "swiftc", dest=dest, approve=approve)
    return calls, dest


def test_build_compiles_and_signs_and_clears_the_privacy_entries_of_its_own_id_only_when_the_owner_approves(tmp_path):
    shown = []
    calls, dest = _build(tmp_path, lambda text: shown.append(text) or True)
    assert [c[0] for c in calls] == ["swiftc", "codesign", "tccutil"]
    assert calls[1][:5] == ["codesign", "--force", "--sign", "-", "--identifier"] and calls[2] == ["tccutil", "reset", "All", "com.envrec.helper"]
    assert "tccutil reset All com.envrec.helper" in shown[0] and "cannot be undone" in shown[0] and helper.last_reset_skipped is False
    assert (dest / "Contents" / "Info.plist").is_file() and (dest / "Contents" / "MacOS" / "envrec-helper").is_file()
    entry = json.loads(helper.approval_log_path().read_text().splitlines()[-1])
    assert (entry["action"], entry["granted"], entry["command"]) == ("tccutil_reset", True, "tccutil reset All com.envrec.helper") and "do it yourself" in entry["undo"]


def test_build_without_the_owners_yes_builds_but_does_not_touch_the_privacy_database(tmp_path):
    calls, dest = _build(tmp_path, lambda text: False)
    assert [c[0] for c in calls] == ["swiftc", "codesign"] and helper.last_reset_skipped is True and (dest / "Contents" / "MacOS" / "envrec-helper").is_file()
    assert json.loads(helper.approval_log_path().read_text().splitlines()[-1])["granted"] is False
    calls_no_approver, _ = _build(tmp_path, None)  # no approver at all means no
    assert "tccutil" not in [c[0] for c in calls_no_approver]


def test_the_app_is_not_launched_without_the_owners_approval_and_the_refusal_is_logged(app):
    fake = Fake()
    for approver in (None, lambda text: False):
        with HelperClient(app, launch=fake.launch, roundtrip=fake.roundtrip, start_wait_s=1, approve=approver) as c:
            with pytest.raises(HelperError) as exc:
                c.request("ble_paired")
        assert exc.value.kind == "approval"
    assert fake.launched == [] and [json.loads(ln)["granted"] for ln in helper.approval_log_path().read_text().splitlines()] == [False, False]


def test_launching_the_app_shows_the_exact_command_and_logs_how_to_undo_it(app):
    fake, shown = Fake(), []
    with client(app, fake, approve=lambda text: shown.append(text) or True) as c:
        c.request("ble_paired")
    assert len(fake.launched) == 1 and f"open -n -a {app} --args {fake.launched[0]}" in shown[0]
    entry = json.loads(helper.approval_log_path().read_text().splitlines()[-1])
    assert entry["action"] == "launch_helper" and entry["granted"] is True and entry["undo"].startswith("pkill -f ")


# ---- A5: the owner's approval to start the helper is an event in the store, not only a line in the helper's own log ---------------------------------------

def _scan_through_the_helper(app, monkeypatch, tmp_path, approver):
    class Done:
        returncode, stdout, stderr = 134, b"", b"abort: privacy violation"

    monkeypatch.setattr("subprocess.run", lambda *a, **k: Done())
    monkeypatch.setattr("importlib.util.find_spec", lambda name: object())
    monkeypatch.setattr("sys.platform", "darwin")
    fake = Fake({"ok": True, "data": [{"id": RAW_ID, "rssi": -60, "company": [76], "services": [], "tx": None}]})
    real = helper.HelperClient
    monkeypatch.setattr(helper, "HelperClient", lambda *a, **k: real(app, launch=fake.launch, roundtrip=fake.roundtrip, start_wait_s=1, approve=k.get("approve")))
    st = Store(tmp_path / "s")
    t = st.put_target(Target(kind=TargetKind.host, ring=1, handle="this-machine"))
    ctx = ToolContext(__import__("envrec.discovery", fromlist=["LocalCollector"]).LocalCollector(), this_platform(), st, t, Salts(), {"seconds": 2}, approver)
    res = run_tool(get_tool("ble_scan_passive"), ctx, {})
    return st, ctx, res, fake


def test_the_helper_launch_approval_is_an_owner_event_in_the_store_with_the_exact_command(app, monkeypatch, tmp_path):
    shown = []
    st, ctx, res, fake = _scan_through_the_helper(app, monkeypatch, tmp_path, lambda text: shown.append(text) or True)
    with st:
        from envrec.tools.ctx import flush_approvals
        flush_approvals(ctx)
        events = [e for e in st.events(type="approval") if e.payload.get("action") == "launch_helper"]
    assert res.ok and len(events) == 1
    ev = events[0]
    assert ev.tag == "OWNER" and ev.payload["granted"] is True and ev.payload["target"] == ctx.target.id
    assert f"open -n -a {app} --args {fake.launched[0]}" in ev.payload["plan"]  # the exact action the owner was shown
    assert any("open -n -a" in s for s in shown)
    assert json.loads(helper.approval_log_path().read_text().splitlines()[-1])["action"] == "launch_helper"  # the helper keeps its own log too


def test_a_refused_helper_launch_is_also_an_owner_event_and_nothing_started(app, monkeypatch, tmp_path):
    st, ctx, res, fake = _scan_through_the_helper(app, monkeypatch, tmp_path, lambda text: False)
    with st:
        from envrec.tools.ctx import flush_approvals
        flush_approvals(ctx)
        events = [e for e in st.events(type="approval") if e.payload.get("action") == "launch_helper"]
    assert not res.ok and fake.launched == [] and [e.payload["granted"] for e in events] == [False] and events[0].tag == "OWNER"


# ---- Mission 2 requests: connect a device macOS already has paired, read and set the output device ---------------------------------------------------

SALT = "ab" * 16


@pytest.mark.parametrize("req,args", [
    ("connect_paired", {"device": "zzzzzzzz", "salt": SALT, "action": "connect"}),                  # not a hash
    ("connect_paired", {"device": "0123abcd", "salt": SALT, "action": "pair"}),                     # not an allowed action
    ("connect_paired", {"device": "0123abcd", "salt": "short", "action": "connect"}),
    ("connect_paired", {"device": "0123abcd", "salt": SALT}),                                       # missing action
    ("connect_paired", {"device": "0123abcd", "salt": SALT, "action": "connect", "address": "AA:BB"}),  # an extra argument
    ("connect_paired", {"device": "0123abcd; ls", "salt": SALT, "action": "connect"}),
    ("audio_set", {"device": "0123abcd"}),
    ("audio_get", {"salt": 5}),
])
def test_the_new_requests_refuse_anything_but_their_exact_shapes_and_launch_nothing(app, req, args):
    fake = Fake()
    with client(app, fake) as c:
        with pytest.raises(HelperError) as exc:
            c.request(req, **args)
    assert exc.value.kind == "refused" and fake.launched == [] and fake.seen == []


def test_connect_paired_sends_only_a_hash_a_salt_and_an_action_and_reports_the_state(app):
    fake = Fake({"ok": True, "data": {"connected": True}})
    with client(app, fake) as c:
        assert helper.connect_paired("0123abcd", SALT, "connect", client=c) is True
    assert fake.seen[0]["req"] == "connect_paired" and set(fake.seen[0]) == {"nonce", "req", "device", "salt", "action"}


def test_the_audio_outputs_come_back_as_hashes_and_transports_only(app):
    fake = Fake({"ok": True, "data": [{"id": "0a1b2c3d", "transport": "builtin", "current": True}, {"id": "4e5f6a7b", "transport": "bluetooth", "current": False}]})
    with client(app, fake) as c:
        outs = helper.audio_outputs(SALT, client=c)
        assert outs[1] == {"id": "4e5f6a7b", "transport": "bluetooth", "current": False}
    src = helper.SOURCE.read_text()
    assert 'kAudioDevicePropertyDeviceName' not in src and 'kAudioObjectPropertyName' not in src  # the helper never reads a device name for audio
