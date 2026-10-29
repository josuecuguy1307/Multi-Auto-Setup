"""Mission 2's hands, with fakes for the helper, AppleScript, the Keychain and Spotify's servers: nothing real is touched."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json

import pytest

from envrec import helper
from envrec.contracts import CreatedBy, Evidence, Fact, FactStatus, Target, TargetKind
from envrec.discovery import Salts
from envrec.discovery.types import Capture, this_platform
from envrec.loop import Loop, RunConfig
from envrec.media import spotify
from envrec.model.scripted import ScriptedModel, scripted_profile
from envrec.privacy.gate import PrivacyGate
from envrec.protocols import ProtocolSet
from envrec.skills.lib import Library
from envrec.store import Store
from envrec.tools import ToolContext, get_tool
from envrec.tools.bridge import RegistryAdapter
from envrec.tools.builtin import media
from envrec.tools.ctx import resolve_refs, run_write_tool, run_tool

EARBUDS = "ble.entity.bluetooth-3337ae5c"
SALT = Salts()


@pytest.fixture
def st(tmp_path):
    with Store(tmp_path / "s") as s:
        yield s


def mac_target(store):
    return store.put_target(Target(kind=TargetKind.media, ring=1, handle="this-mac", authorized=True), from_config=True)


def put_fact(store, t, key, value, status=FactStatus.tested):
    store.write_fact(Fact(target_id=t.id, key=key, value=value, status=status, created_by=CreatedBy.adapter, evidence=[Evidence(provider="test", probe="p", confidence=0.9)]))


def paired(store, t, key=EARBUDS, name="My buds", typ="Headphones", state="paired"):
    put_fact(store, t, key, {"kind": "bluetooth", "ring": 1, "owned": "owned", "resolved": True, "attrs": {"name": name, "type": typ, "state": state}})


def ctx_for(store, t, answers, **kw):
    shown = []
    ctx = ToolContext(None, this_platform(), store, t, SALT, kw.pop("params", {}), lambda text: shown.append(text) or answers)
    return ctx, shown


def resolved(ctx, tool_name, args):
    tool = get_tool(tool_name)
    ctx.resolved, err = resolve_refs(tool, args, ctx.store, ctx.target)
    assert err is None, err
    return tool


class FakeHelper:
    def __init__(self):
        self.calls = []
        self.connected = False
        self.outputs = [{"id": "11111111", "transport": "builtin", "current": True}, {"id": "22222222", "transport": "bluetooth", "current": False, "bt": "3337ae5c"}]

    def connect_paired(self, device, salt, action, *, client=None, approve=None):
        self.calls.append(("connect_paired", device, action))
        self.connected = action == "connect"
        return self.connected

    def audio_outputs(self, salt, *, client=None, approve=None):
        self.calls.append(("audio_get",))
        return [dict(o) for o in self.outputs]

    def audio_set(self, device, salt, *, client=None, approve=None):
        self.calls.append(("audio_set", device))
        for o in self.outputs:
            o["current"] = device in (o["id"], o.get("bt"))
        return True


@pytest.fixture
def fake_helper(monkeypatch):
    f = FakeHelper()
    for name in ("connect_paired", "audio_outputs", "audio_set"):
        monkeypatch.setattr(helper, name, getattr(f, name))
    monkeypatch.setattr(media, "run_osascript", lambda script, timeout=15.0: (0, "50", ""))
    return f


# ---- Bluetooth -------------------------------------------------------------------------------------------------------------------------------------

def test_bt_connect_shows_the_device_and_the_undo_then_asks_the_helper_for_exactly_that_hash(st, fake_helper):
    t = mac_target(st)
    paired(st, t)
    ctx, shown = ctx_for(st, t, True)
    tool = resolved(ctx, "bt_connect", {"device": EARBUDS})
    res = run_write_tool(tool, ctx, {"device": EARBUDS})
    assert res.ok and res.facts == {"media.bt_connected": True, "media.bt_device": "3337ae5c"} and fake_helper.calls == [("connect_paired", "3337ae5c", "connect")]
    assert "My buds" in shown[0] and "already has paired" in shown[0] and "nothing is paired" in shown[0] and "disconnect that device again" in shown[0]
    assert [e.payload["undo"] for e in st.events(type="undo_entry")] == ["bt_disconnect"]


def test_bt_connect_refuses_what_is_not_a_paired_device_fact_and_never_pairs(st, fake_helper):
    t = mac_target(st)
    put_fact(st, t, "ble.nearby_count", 150)
    put_fact(st, t, "ble.entity.ble-aaaaaaaa", {"kind": "ble", "attrs": {}})  # an ambient advertiser is not a paired device
    for key in ("ble.nearby_count", "ble.entity.ble-aaaaaaaa"):
        ctx, _ = ctx_for(st, t, True)
        tool = resolved(ctx, "bt_connect", {"device": key})
        ctx.write_cleared = True
        res = run_tool(tool, ctx, {"device": key})
        assert not res.ok and "paired Bluetooth device" in res.detail
    assert fake_helper.calls == []


def test_a_refusal_connects_nothing(st, fake_helper):
    t = mac_target(st)
    paired(st, t)
    ctx, _ = ctx_for(st, t, False)
    tool = resolved(ctx, "bt_connect", {"device": EARBUDS})
    assert not run_write_tool(tool, ctx, {"device": EARBUDS}).ok and fake_helper.calls == [] and st.events(type="undo_entry") == []


# ---- audio output ----------------------------------------------------------------------------------------------------------------------------------

def test_the_previous_output_must_be_on_the_record_before_the_output_can_change(st, fake_helper):
    t = mac_target(st)
    paired(st, t)
    ctx, shown = ctx_for(st, t, True)
    tool = resolved(ctx, "audio_output", {"device": EARBUDS})
    res = run_write_tool(tool, ctx, {"device": EARBUDS})
    assert not res.ok and "read audio_state first" in res.detail and fake_helper.calls == [] and shown == [] and st.events(type="undo_entry") == []


def test_audio_output_records_the_previous_output_as_its_undo_sets_the_new_one_and_caps_the_volume(st, fake_helper, monkeypatch):
    t = mac_target(st)
    paired(st, t)
    put_fact(st, t, "media.output_current", "11111111")
    scripts = []
    monkeypatch.setattr(media, "run_osascript", lambda script, timeout=15.0: (scripts.append(script), (0, "85" if "output volume of" in script else "", ""))[1])
    ctx, shown = ctx_for(st, t, True)
    tool = resolved(ctx, "audio_output", {"device": EARBUDS})
    res = run_write_tool(tool, ctx, {"device": EARBUDS})
    assert res.ok and res.facts == {"media.output_is_device": True} and fake_helper.calls == [("audio_set", "3337ae5c")]
    undo = st.events(type="undo_entry")[0].payload
    assert undo["undo"] == "audio_restore" and undo["values"] == {"output": "11111111"} and "id 11111111" in shown[0]
    assert "set volume output volume 40" in scripts  # the volume was 85: it is lowered to the cap


def test_audio_state_reports_ids_and_transports_never_names(st, fake_helper):
    t = mac_target(st)
    ctx, _ = ctx_for(st, t, True)
    res = run_tool(get_tool("audio_state"), ctx, {})
    assert res.ok and res.facts["media.output_current"] == "11111111" and res.facts["media.output_current_transport"] == "builtin"
    assert res.facts["media.outputs"] == ["builtin:11111111", "bluetooth:22222222 (bluetooth device 3337ae5c)"]


# ---- Spotify desktop app ---------------------------------------------------------------------------------------------------------------------------

def test_spotify_state_reads_only_the_player_and_the_current_track(st, monkeypatch):
    seen = []
    monkeypatch.setattr(media, "run_osascript", lambda s, timeout=15.0: (seen.append(s), (0, "playing|spotify:track:4LRPiXqCikLlN15c3yImP7|Harry Styles|As It Was|35", ""))[1])
    t = mac_target(st)
    res = run_tool(get_tool("spotify_state"), ToolContext(None, this_platform(), st, t, SALT, {}, None), {})
    assert res.facts == {"media.player_state": "playing", "media.track_id": "4LRPiXqCikLlN15c3yImP7", "media.track_artist": "Harry Styles", "media.track_title": "As It Was"}
    assert all("playlist" not in s.lower() and "library" not in s.lower() and "account" not in s.lower() for s in seen)


def test_a_missing_automation_permission_is_typed_with_the_fix(st, monkeypatch):
    monkeypatch.setattr(media, "run_osascript", lambda s, timeout=15.0: (1, "", "execution error: Not authorized to send Apple events to Spotify. (-1743)"))
    t = mac_target(st)
    res = run_tool(get_tool("spotify_state"), ToolContext(None, this_platform(), st, t, SALT, {}, None), {})
    assert not res.ok and res.error.value == "permission" and "Automation" in res.detail and "Allow" in res.detail


def test_spotify_play_sends_only_a_validated_track_id_caps_the_volume_and_records_pause_as_its_undo(st, monkeypatch):
    scripts = []
    monkeypatch.setattr(media, "run_osascript", lambda s, timeout=15.0: (scripts.append(s), (0, "", ""))[1])
    t = mac_target(st)
    put_fact(st, t, "spotify.candidate_id", "4LRPiXqCikLlN15c3yImP7")
    put_fact(st, t, "spotify.candidate_title", "As It Was")
    ctx, shown = ctx_for(st, t, True)
    tool = resolved(ctx, "spotify_play", {"track": "spotify.candidate_id"})
    assert run_write_tool(tool, ctx, {"track": "spotify.candidate_id"}).ok
    assert 'play track "spotify:track:4LRPiXqCikLlN15c3yImP7"' in scripts[0] and "set sound volume to 40" in scripts[0]
    assert "As It Was" in shown[0] and "pause the Spotify app" in shown[0] and st.events(type="undo_entry")[0].payload["undo"] == "spotify_pause"
    for bad in ("4LRPi XqCik", 'x" & (do shell script "id") & "', "short"):
        put_fact(st, t, "spotify.bad", bad)
        ctx2, _ = ctx_for(st, t, True)
        tool2 = resolved(ctx2, "spotify_play", {"track": "spotify.bad"})
        ctx2.write_cleared = True
        res = run_tool(tool2, ctx2, {"track": "spotify.bad"})
        assert not res.ok and "Spotify track id" in res.detail
    assert len(scripts) == 1  # nothing else reached AppleScript


# ---- Spotify: the owner's login and the library ---------------------------------------------------------------------------------------------------

def test_pkce_challenge_is_the_sha256_of_the_verifier_and_the_authorize_url_asks_only_for_the_library_scope():
    v, c = spotify.pkce_pair()
    assert c == base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode() and 43 <= len(v) <= 128
    url = spotify.authorize_url("cid", c, "st")
    assert "scope=user-library-read" in url and "code_challenge_method=S256" in url and "client_secret" not in url and "redirect_uri=http%3A%2F%2F127.0.0.1%3A8765%2Fcallback" in url


def test_the_tokens_go_to_the_keychain_through_stdin_never_as_an_argument():
    calls = []

    class Done:
        returncode, stdout = 0, ""

    spotify.save_credentials({"access_token": "SECRET-TOKEN-VALUE"}, run=lambda argv, **kw: (calls.append((argv, kw)), Done())[1])
    argv, kw = calls[0]
    assert argv == ["security", "-i"] and "SECRET-TOKEN-VALUE" not in " ".join(argv) and "SECRET-TOKEN-VALUE" in kw["input"]


def test_a_fresh_token_is_used_an_expired_one_is_refreshed_and_no_login_is_a_typed_login_error():
    creds = {"client_id": "cid", "access_token": "old", "refresh_token": "rt", "expires_at": 100.0}
    saved = []

    class R:
        status_code = 200

        def json(self):
            return {"access_token": "new", "expires_in": 3600}

    assert spotify.access_token(load=lambda: dict(creds), now=lambda: 50.0) == "old"
    assert spotify.access_token(load=lambda: dict(creds), save=saved.append, post=lambda url, data, timeout: R(), now=lambda: 500.0) == "new" and saved[0]["refresh_token"] == "rt"
    with pytest.raises(spotify.SpotifyError) as exc:
        spotify.access_token(load=lambda: None)
    assert exc.value.kind == "login" and "spotify-login" in exc.value.detail


def test_find_saved_tracks_returns_only_matching_ids_and_titles_and_stops_early():
    page = [{"track": {"id": f"id{i:020d}", "name": f"Song {i}", "artists": [{"name": "Someone Else" if i % 3 else "Harry Styles"}]}} for i in range(50)]
    calls = []

    class R:
        status_code = 200

        def json(self):
            return {"items": page}

    found = spotify.find_saved_tracks("harry  styles", "tok", limit=3, get=lambda url, params, headers, timeout: (calls.append(params["offset"]), R())[1])
    assert [f["title"] for f in found] == ["Song 0", "Song 3", "Song 6"] and calls == [0] and set(found[0]) == {"id", "title", "artist"}


def test_spotify_find_keeps_only_the_chosen_track_and_never_the_library(st, monkeypatch):
    monkeypatch.setattr(spotify, "access_token", lambda **k: "tok")
    monkeypatch.setattr(spotify, "find_saved_tracks", lambda artist, token, **k: [{"id": "4LRPiXqCikLlN15c3yImP7", "title": "As It Was", "artist": "Harry Styles"}, {"id": "x" * 22, "title": "Other", "artist": "Harry Styles"}])
    t = mac_target(st)
    ctx = ToolContext(None, this_platform(), st, t, SALT, {}, lambda text: True)
    res = run_tool(get_tool("spotify_find"), ctx, {"artist": "Harry Styles"})
    assert res.facts["spotify.candidate_id"] == "4LRPiXqCikLlN15c3yImP7" and res.facts["spotify.candidates_found"] == 2
    assert json.loads(res.captures[0].data) == {"id": "4LRPiXqCikLlN15c3yImP7", "title": "As It Was", "artist": "Harry Styles"} and "Other" not in res.captures[0].data.decode()


def test_spotify_find_without_a_login_is_a_typed_permission_not_a_crash(st, monkeypatch):
    monkeypatch.setattr(spotify, "load_credentials", lambda: None)
    t = mac_target(st)
    res = run_tool(get_tool("spotify_find"), ToolContext(None, this_platform(), st, t, SALT, {}, lambda text: True), {"artist": "Harry Styles"})
    assert not res.ok and res.error.value == "permission" and "spotify-login" in res.detail


# ---- the whole protocol, the model choosing among the real menus, FIXTURE ------------------------------------------------------------------------

def test_the_media_protocol_end_to_end_with_a_scripted_model_and_fakes_asks_the_owner_for_every_effect_and_records_every_undo(st, fake_helper, monkeypatch):
    lib = Library().load()
    scripts = []
    monkeypatch.setattr(media, "run_osascript", lambda s, timeout=15.0: (scripts.append(s), (0, "playing|spotify:track:4LRPiXqCikLlN15c3yImP7|Harry Styles|As It Was|35" if "player state" in s else "", ""))[1])
    monkeypatch.setattr(spotify, "access_token", lambda **k: "tok")
    monkeypatch.setattr(spotify, "find_saved_tracks", lambda artist, token, **k: [{"id": "4LRPiXqCikLlN15c3yImP7", "title": "As It Was", "artist": "Harry Styles"}])
    asked = []
    t = mac_target(st)
    paired(st, t)  # what skill.ble_paired would have written; the skill itself reads system_profiler, which this test does not run
    put_fact(st, t, "ble.paired_count", 1)
    call = lambda tool, **args: json.dumps({"action": "call_tool", "tool": tool, "args": args})  # noqa: E731
    adv = lambda to: json.dumps({"action": "advance", "to": to})  # noqa: E731
    replies = [None, call("bt_connect", device=EARBUDS), adv("output"), call("audio_state"), call("audio_output", device=EARBUDS), adv("find"), call("spotify_find", artist="Harry Styles"),
               adv("play"), call("spotify_play", track="spotify.candidate_id"), adv("check"), call("spotify_state"), adv("done")]
    reg = RegistryAdapter(platform=this_platform(), library=lib, approve=lambda text: asked.append(text) or True)

    class StartsAfterPaired(ProtocolSet):
        """The paired list is a skill that reads system_profiler; this test does not run it (facts are written above), so the run starts at the step after it."""

        def start(self, kind):
            run = super().start(kind)
            run.state = "connect" if kind == "media" else run.state
            return run

    protocols = StartsAfterPaired.from_library(lib)
    loop = Loop(st, [reg], ScriptedModel(replies[1:], scripted_profile()), config=RunConfig(max_steps=16, protocols=protocols, library=lib))
    summary = asyncio.run(loop.run(t))
    if this_platform() != "macos":
        pytest.skip("the media tools are macOS tools")
    assert summary.stop_reason == "protocol_done", (summary.stop_reason, summary.tools_run, [e.payload for e in st.events(type="action_rejected")], [e.payload for e in st.events(type="protocol_refused")])
    assert summary.tools_run == ["bt_connect", "audio_state", "audio_output", "spotify_find", "spotify_play", "spotify_state"]
    writes = [a for a in asked if "undo:" in a]
    assert len(writes) == 3 and [e.payload["step"] for e in st.events(type="undo_entry")] == ["bt_connect", "audio_output", "spotify_play"]
    assert all(e.tag == "OWNER" for e in st.events(type="approval"))
    # a tool that reports facts without collecting a capture still leaves them in the store, with the report as their evidence (they were silently dropped before)
    assert st.current_fact(t.id, "media.bt_connected").value is True and st.current_fact(t.id, "media.output_is_device").value is True
    assert st.current_fact(t.id, "spotify.candidate_id").value == "4LRPiXqCikLlN15c3yImP7" and st.current_fact(t.id, "media.player_state").value == "playing"
