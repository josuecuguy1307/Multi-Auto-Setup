"""Hands for this computer's own media session: connect a device macOS ALREADY has paired, read and set the audio output, read and control the Spotify desktop app.

Generic and fixed: the tools know no product and no person. The owner's devices are chosen by the model from FACTS (a fact key is the reference); code does the rest. Every effect is a
write-tier tool: the owner sees the exact action and its undo before anything happens (prepare_write). Volume is capped. Nothing here records audio or touches the microphone. The
Spotify library is read only by `spotify_find`, with the owner's own login (see envrec.media.spotify), and only the chosen track's id and title are kept."""

from __future__ import annotations

import json
import re
import subprocess

from envrec import helper
from envrec.discovery.types import Reason
from envrec.tools.ctx import ToolContext, ToolResult
from envrec.tools.registry import FactRef, tool

VOLUME_CAP = 40  # percent, for the Mac's output volume and for the Spotify app's own volume
_APP_NOT_ALLOWED = re.compile(r"-1743|not authori[sz]ed", re.I)


def run_osascript(script: str, timeout: float = 15.0) -> tuple[int, str, str]:
    """The one place AppleScript runs. `script` is always a fixed text of this module with at most a track id (validated) in it."""
    done = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    return done.returncode, done.stdout.strip(), done.stderr.strip()


def _automation_fix(err: str) -> str:
    return "macOS asks whether this program may control Spotify (System Settings > Privacy & Security > Automation); click Allow, then run this again" if _APP_NOT_ALLOWED.search(err) else err[:160]


def _entity_hash(fact) -> str | None:
    """The 8-hex hash at the end of a paired-device entity key (`ble.entity.bluetooth-3337ae5c`): the id connect_paired takes."""
    m = re.fullmatch(r"ble\.entity\.bluetooth-([0-9a-f]{8})", fact.key)
    return m.group(1) if m else None


def _describe(fact) -> str:
    attrs = (fact.value or {}).get("attrs", {}) if isinstance(fact.value, dict) else {}
    return f"{attrs.get('name') or 'a paired device'!r} ({attrs.get('type') or 'type unknown'}, {attrs.get('state') or 'state unknown'})"


# ---- Bluetooth: connect a device macOS already has paired -----------------------------------------------------------------------------------------

def _connect_text(ctx: ToolContext, device: str) -> str:
    f = ctx.ref_fact("device")
    return (f"connect the Bluetooth device {_describe(f) if f is not None else device} that macOS already has paired, through envrec Helper (macOS opens the connection to it; "
            "nothing is paired, nothing is written to the device)")


def _connect_undo(ctx: ToolContext, device: str) -> dict:
    return {"tool": "bt_disconnect", "args": {"device": device}, "text": "disconnect that device again (envrec Helper closes the connection)"}


@tool(tags=("bluetooth", "media", "active"), kinds=("media",), ring=1, approval=_connect_text, undo=_connect_undo, produces=("media.bt_connected", "media.bt_device"))
def bt_connect(ctx: ToolContext, device: FactRef) -> ToolResult:
    """Connect a Bluetooth device that macOS already has paired.

    Args:
        device: the fact key of the paired device in FACTS (ble.entity.bluetooth-...).
    Returns:
        Fact media.bt_connected: whether macOS reports the device connected afterwards.
    Tier: write
    Platforms: macos
    Notes: never pairs a new device; the device must be in macOS's own paired list; asks the owner with the exact action and its undo first.
    """
    return _bt(ctx, "connect")


def _bt(ctx: ToolContext, action: str) -> ToolResult:
    fact = ctx.ref_fact("device")
    h = _entity_hash(fact) if fact is not None else None
    if h is None:
        return ToolResult.fail(Reason.not_applicable, "device must be the key of a paired Bluetooth device in FACTS (ble.entity.bluetooth-...)")
    try:
        connected = helper.connect_paired(h, ctx.salts.stable, action, approve=ctx.recording_approve("launch_helper"))
    except helper.HelperError as exc:
        return ToolResult.fail(Reason.permission if exc.kind in ("permission", "missing") else Reason.unavailable, f"helper: {exc.detail}"[:200])
    return ToolResult(facts={"media.bt_connected": connected, "media.bt_device": h}, provider="native", limitations=["as macOS reports it right after the request; the check after the run reads it again"])


@tool(tags=("bluetooth", "media", "active"), kinds=("media",), ring=1, audience="undo", approval=lambda ctx, device: "disconnect the Bluetooth device macOS already has paired and that this run connected",
      undo=lambda ctx, device: {"tool": "bt_connect", "args": {"device": device}, "text": "connect it again"})
def bt_disconnect(ctx: ToolContext, device: FactRef) -> ToolResult:
    """Disconnect a paired Bluetooth device this run connected.

    Args:
        device: the fact key of the paired device in FACTS.
    Returns:
        Fact media.bt_connected: false when the device is disconnected afterwards.
    Tier: write
    Platforms: macos
    Notes: the undo of bt_connect; run only by the undo executor.
    """
    return _bt(ctx, "disconnect")


# ---- audio output --------------------------------------------------------------------------------------------------------------------------------

@tool(tags=("media", "audio", "passive"), kinds=("media",), ring=1, produces=("media.output_current", "media.outputs"))
def audio_state(ctx: ToolContext) -> ToolResult:
    """List audio output devices and which one is the default.

    Args:
        none
    Returns:
        Facts media.output_current (the id of the default output), media.output_current_transport, media.outputs (transport and id of each), no device names.
    Tier: observe
    Platforms: macos
    Notes: read-only, through envrec Helper (its launch asks the owner); ids are salted hashes.
    """
    try:
        outs = helper.audio_outputs(ctx.salts.stable, approve=ctx.recording_approve("launch_helper"))
    except helper.HelperError as exc:
        return ToolResult.fail(Reason.permission if exc.kind in ("permission", "missing") else Reason.unavailable, f"helper: {exc.detail}"[:200])
    cur = next((o for o in outs if o.get("current")), None)
    return ToolResult(facts={"media.output_current": cur["id"] if cur else None, "media.output_current_transport": cur["transport"] if cur else None,
                             "media.outputs": [f"{o['transport']}:{o['id']}" + (f" (bluetooth device {o['bt']})" if o.get("bt") else "") for o in outs]},
                      reasons={"media.output_current": "no output device reported as the default"}, provider="native")


def _output_text(ctx: ToolContext, device: str) -> str:
    f = ctx.ref_fact("device")
    return (f"make the audio output of {_describe(f) if f is not None else device} the default sound output of this computer, through envrec Helper, then lower the output volume to "
            f"{VOLUME_CAP}% if it is higher")


def _output_undo(ctx: ToolContext, device: str) -> dict:
    previous = ctx.known().get("media.output_current")
    if not previous:
        raise ValueError("the previous audio output is not on the record: read audio_state first, so it can be restored")
    return {"tool": "audio_restore", "args": {"output": str(previous)}, "text": f"make the previous output (id {previous}) the default again"}


@tool(tags=("media", "audio", "active"), kinds=("media",), ring=1, approval=_output_text, undo=_output_undo, produces=("media.output_is_device",))
def audio_output(ctx: ToolContext, device: FactRef) -> ToolResult:
    """Make a connected Bluetooth device the default sound output.

    Args:
        device: the fact key of the paired device in FACTS (ble.entity.bluetooth-...).
    Returns:
        Fact media.output_is_device: whether the default output is that device afterwards.
    Tier: write
    Platforms: macos
    Notes: the previous output must be on the record first (audio_state) so it can be restored; the output volume is capped; asks the owner first.
    """
    fact = ctx.ref_fact("device")
    h = _entity_hash(fact) if fact is not None else None
    if h is None:
        return ToolResult.fail(Reason.not_applicable, "device must be the key of a paired Bluetooth device in FACTS (ble.entity.bluetooth-...)")
    try:
        ok = helper.audio_set(h, ctx.salts.stable, approve=ctx.recording_approve("launch_helper"))
    except helper.HelperError as exc:
        return ToolResult.fail(Reason.permission if exc.kind in ("permission", "missing") else Reason.unavailable, f"helper: {exc.detail}"[:200])
    cap_volume()
    return ToolResult(facts={"media.output_is_device": ok}, provider="native", limitations=[f"the output volume is at most {VOLUME_CAP}%"])


def cap_volume(cap: int = VOLUME_CAP, run=None) -> None:
    run = run or run_osascript  # looked up when called, so a test that replaces run_osascript replaces it here too
    code, out, _ = run("output volume of (get volume settings)")
    if code == 0 and out.isdigit() and int(out) > cap:
        run(f"set volume output volume {cap}")


@tool(tags=("media", "audio", "active"), kinds=("media",), ring=1, audience="undo", approval=lambda ctx, output: "restore the audio output this computer had before this run",
      undo=lambda ctx, output: {"tool": "audio_restore", "args": {"output": output}, "text": "nothing further"})
def audio_restore(ctx: ToolContext, output: str) -> ToolResult:
    """Make a previous audio output device the default again.

    Args:
        output: the id (salted hash) that audio_state reported for it.
    Returns:
        Fact media.output_current: the id of the default output afterwards.
    Tier: write
    Platforms: macos
    Notes: the undo of audio_output; run only by the undo executor.
    """
    if not re.fullmatch(r"[0-9a-f]{8}", output):
        return ToolResult.fail(Reason.denied, "output must be an 8-digit hexadecimal id")
    try:
        helper.audio_set(output, ctx.salts.stable, approve=ctx.recording_approve("launch_helper"))
        outs = helper.audio_outputs(ctx.salts.stable, approve=ctx.recording_approve("launch_helper"))
    except helper.HelperError as exc:
        return ToolResult.fail(Reason.unavailable, f"helper: {exc.detail}"[:200])
    cur = next((o for o in outs if o.get("current")), None)
    return ToolResult(facts={"media.output_current": cur["id"] if cur else None}, provider="native")


# ---- Spotify desktop app --------------------------------------------------------------------------------------------------------------------------

_STATE_SCRIPT = (
    'if application "Spotify" is not running then return "not_running|||||"\n'
    'tell application "Spotify"\n'
    '  set s to player state as string\n'
    '  if s is "stopped" then return s & "|||||"\n'
    '  return s & "|" & (id of current track) & "|" & (artist of current track) & "|" & (name of current track) & "|" & (sound volume as string)\n'
    'end tell')


def read_player(run=None) -> dict | None:
    """What the Spotify app reports now: {state, track_id, artist, title, volume}. None when AppleScript could not ask (the reason is the second value of read_player_checked)."""
    code, out, err = (run or run_osascript)(_STATE_SCRIPT)
    if code != 0:
        return None
    parts = (out.split("|") + [""] * 5)[:5]
    return {"state": parts[0], "track_id": parts[1].rsplit(":", 1)[-1], "artist": parts[2], "title": parts[3], "volume": int(parts[4]) if parts[4].isdigit() else None}


@tool(tags=("media", "spotify", "passive"), kinds=("media",), ring=1, produces=("media.player_state", "media.track_id"))
def spotify_state(ctx: ToolContext) -> ToolResult:
    """Read what the Spotify desktop app is playing now.

    Args:
        none
    Returns:
        Facts media.player_state (playing, paused, stopped, not_running), media.track_id, media.track_artist, media.track_title.
    Tier: observe
    Platforms: macos
    Notes: asks the app only for its player state and current track; reads nothing of the library, playlists or account.
    """
    code, out, err = run_osascript(_STATE_SCRIPT)
    if code != 0:
        return ToolResult.fail(Reason.permission if _APP_NOT_ALLOWED.search(err) else Reason.unavailable, _automation_fix(err))
    p = read_player()
    assert p is not None
    return ToolResult(facts={"media.player_state": p["state"], "media.track_id": p["track_id"] or None, "media.track_artist": p["artist"] or None,
                             "media.track_title": p["title"] or None}, reasons={"media.track_id": "nothing is loaded in the player"}, provider="native")


_TRACK_ID = re.compile(r"[0-9A-Za-z]{22}")


def _play_text(ctx: ToolContext, track: str) -> str:
    f = ctx.ref_fact("track")
    title = ctx.known().get("spotify.candidate_title")
    return (f"play the Spotify track {title!r} (id {f.value if f is not None else track}) in the Spotify desktop app (opened if it is not running) and set the app's volume to "
            f"at most {VOLUME_CAP}")


def _play_undo(ctx: ToolContext, track: str) -> dict:
    return {"tool": "spotify_pause", "args": {}, "text": "pause the Spotify app"}


@tool(tags=("media", "spotify", "active"), kinds=("media",), ring=1, approval=_play_text, undo=_play_undo, produces=("media.play_requested",))
def spotify_play(ctx: ToolContext, track: FactRef) -> ToolResult:
    """Play one Spotify track in the desktop app.

    Args:
        track: the fact key whose value is the Spotify track id (spotify.candidate_id).
    Returns:
        Fact media.play_requested: whether the app accepted the request.
    Tier: write
    Platforms: macos
    Notes: controls the Spotify desktop app through AppleScript with only a track id; caps the app's volume; asks the owner first.
    """
    fact = ctx.ref_fact("track")
    tid = str(fact.value) if fact is not None else ""
    if not _TRACK_ID.fullmatch(tid):
        return ToolResult.fail(Reason.not_applicable, "track must be the key of a fact whose value is a Spotify track id (spotify.candidate_id)")
    script = (f'tell application "Spotify"\n  play track "spotify:track:{tid}"\n  if sound volume > {VOLUME_CAP} then set sound volume to {VOLUME_CAP}\nend tell')
    code, _, err = run_osascript(script, timeout=30)
    if code != 0:
        return ToolResult.fail(Reason.permission if _APP_NOT_ALLOWED.search(err) else Reason.unavailable, _automation_fix(err))
    return ToolResult(facts={"media.play_requested": True}, provider="native", limitations=["the app accepted the request; whether it is playing is read again by the check"])


@tool(tags=("media", "spotify", "active"), kinds=("media",), ring=1, audience="undo", approval=lambda ctx: "pause the Spotify app",
      undo=lambda ctx: {"tool": "spotify_pause", "args": {}, "text": "nothing further"})
def spotify_pause(ctx: ToolContext) -> ToolResult:
    """Pause the Spotify desktop app.

    Args:
        none
    Returns:
        Fact media.player_state afterwards.
    Tier: write
    Platforms: macos
    Notes: the undo of spotify_play; run only by the undo executor.
    """
    code, _, err = run_osascript('tell application "Spotify" to pause')
    if code != 0:
        return ToolResult.fail(Reason.unavailable, _automation_fix(err))
    p = read_player()
    return ToolResult(facts={"media.player_state": p["state"] if p else None}, provider="native")


# ---- finding the track in the owner's own library -------------------------------------------------------------------------------------------------

def _find_text(ctx: ToolContext, artist: str) -> str:
    return (f"read the saved tracks of YOUR Spotify library (Spotify Web API, scope user-library-read, your own login) to find one by {artist!r}; only the id and title of the chosen "
            "track are kept, nothing else of the library is stored")


@tool(tags=("media", "spotify", "active"), kinds=("media",), ring=1, approval=_find_text, produces=("spotify.candidate_id", "spotify.candidate_title", "spotify.candidate_artist"))
def spotify_find(ctx: ToolContext, artist: str) -> ToolResult:
    """Find a saved Spotify track by an artist in the owner's library.

    Args:
        artist: the artist's name as the goal gives it.
    Returns:
        Facts spotify.candidate_id, spotify.candidate_title, spotify.candidate_artist (the most recently saved match) and spotify.candidates_found (how many up to 5).
    Tier: read
    Platforms: macos
    Notes: read-only; the owner's own login (token in the Keychain); keeps only the chosen track's id and title; asks the owner first.
    """
    from envrec.discovery.types import Capture
    from envrec.media import spotify
    name = " ".join(artist.split())[:80]
    if not name:
        return ToolResult.fail(Reason.denied, "artist is empty")
    try:
        tracks = spotify.find_saved_tracks(name, spotify.access_token())
    except spotify.SpotifyError as exc:
        return ToolResult.fail(Reason.permission if exc.kind == "login" else Reason.unavailable, exc.detail[:200])
    if not tracks:
        cap = Capture("candidate", "spotify saved tracks", b"[]", 0, "remote", ctx.platform)
        return ToolResult(captures=[cap], facts={"spotify.candidate_id": None, "spotify.candidate_title": None, "spotify.candidate_artist": None, "spotify.candidates_found": 0},
                          reasons={k: f"none of the saved tracks is by {name!r}" for k in ("spotify.candidate_id", "spotify.candidate_title", "spotify.candidate_artist")}, provider="spotify")
    first = tracks[0]
    cap = Capture("candidate", "spotify saved tracks", json.dumps({k: first[k] for k in ("id", "title", "artist")}).encode(), 0, "remote", ctx.platform)
    return ToolResult(captures=[cap], facts={"spotify.candidate_id": first["id"], "spotify.candidate_title": first["title"], "spotify.candidate_artist": first["artist"],
                                            "spotify.candidates_found": len(tracks)}, provider="spotify", limitations=["from the owner's saved tracks; the newest saved match"])
