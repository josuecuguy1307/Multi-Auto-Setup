"""Mission 2's verification and undo, with fakes: the checks read the world through readers that are not the tools, and the undo replays newest first, asking each time."""
from __future__ import annotations

from envrec.contracts import CreatedBy, Evidence, Fact, FactStatus, Target, TargetKind
from envrec.discovery import Salts
from envrec.discovery.types import short_hash, this_platform
from envrec.store import Store
from envrec.tools import ToolContext
from envrec.tools.builtin import media
from envrec.undo import pending_tool_undos, run_undo
from envrec.verify import media_check as mc

SALT = Salts().stable
ADDR = "AA:BB:CC:DD:EE:FF"
H = short_hash(ADDR, salt=SALT)


def bt(connected: bool):
    return lambda: {"SPBluetoothDataType": [{("device_connected" if connected else "device_not_connected"): [{"Buds": {"device_address": ADDR}}], "device_not_connected": [{"Other": {"device_address": "11:22:33:44:55:66"}}]}]}


def audio(name):
    return lambda: {"SPAudioDataType": [{"_items": [{"_name": "Speakers", "coreaudio_default_audio_output_device": "spaudio_no" if name != "Speakers" else "spaudio_yes"},
                                                     {"_name": "Buds", "coreaudio_default_audio_output_device": "spaudio_yes" if name == "Buds" else "spaudio_no"}]}]}


PLAYING = {"state": "playing", "track_id": "TRACK", "artist": "Harry Styles", "title": "x"}


def test_check_one_needs_the_system_to_say_the_device_with_that_hash_is_connected():
    assert mc.check_connected(H, SALT, bt(True)).passed
    assert not mc.check_connected(H, SALT, bt(False)).passed
    assert not mc.check_connected("00000000", SALT, bt(True)).passed  # a different device is connected, not this one


def test_check_two_needs_the_output_the_player_the_track_and_the_artist_all_at_once():
    ok = lambda **kw: mc.check_playing_through(H, SALT, "TRACK", "Harry Styles", read_bt=bt(True), read_audio=audio(kw.get("out", "Buds")), read_player=lambda: kw.get("player", PLAYING))  # noqa: E731
    assert ok().passed
    assert not ok(out="Speakers").passed and not ok(out="Speakers").evidence["default_output_is_the_device"]
    assert not ok(player={**PLAYING, "state": "paused"}).passed
    assert not ok(player={**PLAYING, "track_id": "OTHER"}).passed
    assert not ok(player={**PLAYING, "artist": "Someone Else"}).passed
    assert not ok(player=None).passed
    assert not mc.check_playing_through(H, SALT, "TRACK", "", read_bt=bt(True), read_audio=audio("Buds"), read_player=lambda: PLAYING).passed  # no artist asked for: nothing to confirm


def test_the_restore_check_compares_the_ids_and_an_empty_record_never_passes():
    assert mc.restore_confirmed("11111111", "11111111").passed and not mc.restore_confirmed("11111111", "22222222").passed and not mc.restore_confirmed("", "").passed


def test_the_undo_replays_newest_first_asks_each_time_and_marks_what_it_did(tmp_path, monkeypatch):
    calls = []

    def fake_osascript(script, timeout=15.0):
        calls.append(script)
        return 0, "paused|||||", ""

    monkeypatch.setattr(media, "run_osascript", fake_osascript)
    with Store(tmp_path / "s") as st:
        t = st.put_target(Target(kind=TargetKind.media, ring=1, handle="this-mac", authorized=True), from_config=True)
        from envrec.contracts import Actor, Event
        st.append_event(Event(actor=Actor.loop, type="undo_entry", payload={"target": t.id, "step": "spotify_play", "undo": "spotify_pause", "values": {}, "text": "pause the Spotify app"}))
        asked = []
        results = run_undo(st, t, lambda: ToolContext(None, this_platform(), st, t, Salts(), {}, lambda text: asked.append(text) or True))
        assert [(r.step, r.ok) for r in results] == [("spotify_play", True)] and len(asked) == 1 and "pause the Spotify app" in asked[0]
        assert pending_tool_undos(st, t.id) == []  # applied once, not again
        assert run_undo(st, t, lambda: ToolContext(None, this_platform(), st, t, Salts(), {}, lambda text: True)) == []


def test_a_refused_undo_step_is_recorded_as_not_done_and_stays_pending_for_the_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "run_osascript", lambda s, timeout=15.0: (0, "", ""))
    with Store(tmp_path / "s") as st:
        t = st.put_target(Target(kind=TargetKind.media, ring=1, handle="this-mac", authorized=True), from_config=True)
        from envrec.contracts import Actor, Event
        st.append_event(Event(actor=Actor.loop, type="undo_entry", payload={"target": t.id, "step": "spotify_play", "undo": "spotify_pause", "values": {}, "text": "pause"}))
        results = run_undo(st, t, lambda: ToolContext(None, this_platform(), st, t, Salts(), {}, lambda text: False))
        assert [r.ok for r in results] == [False] and [e.payload["ok"] for e in st.events(type="undo_applied")] == [False]


def test_this_computer_is_enrolled_with_the_local_transport_by_the_owner_typing_its_name_and_yes(tmp_path):
    import yaml

    from envrec.enroll.enroll import enroll
    from envrec.inventory.report import Candidate
    from envrec.mission import Transport, load_targets
    answers = iter(["This Mac", "yes"])
    with Store(tmp_path / "s") as st:
        res = enroll(Candidate(name="This Mac", kind="host", seen_via="here", host="127.0.0.1"), targets_path=tmp_path / "targets.yaml", store=st, check=lambda e, t: True, transport=Transport.local,
                     input_fn=lambda q: next(answers), output_fn=lambda s: None, is_tty=lambda: True)
        assert res.enrolled and res.entry.allowed_transports == [Transport.local]
    cfg = load_targets(tmp_path / "targets.yaml")
    text = (tmp_path / "targets.yaml").read_text()
    assert cfg.targets[0].local.scope == "bluetooth_audio_media" and "key" not in text.lower().replace("key_env", "") and yaml.safe_load(text)["targets"][0]["id"] == "this-mac"
    from envrec.remote.provision import sync_targets
    with Store(tmp_path / "s2") as st2:
        t = sync_targets(st2, cfg)["this-mac"]
        assert t.kind.value == "media" and t.authorized and t.ring == 1
