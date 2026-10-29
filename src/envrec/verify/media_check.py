"""Mission 2's verification, by code, from the world, independent of the model and of the tools it used.

Check 1: the system says the paired device is connected (system_profiler, not the helper that connected it).
Check 2: the default sound output is that device AND the Spotify app is playing AND the current track is the one asked for (its id is the chosen one, its artist the requested artist).
Then the previous output is put back and that is confirmed. Every reader is a parameter, so tests use fakes; the real ones read system_profiler and the Spotify app.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field

from envrec.discovery.types import short_hash


@dataclass
class Check:
    name: str
    passed: bool
    evidence: dict = field(default_factory=dict)


def _profiler(datatype: str) -> dict:
    done = subprocess.run(["system_profiler", datatype, "-json"], capture_output=True, text=True, timeout=40)
    return json.loads(done.stdout or "{}")


def paired_devices(read: Callable[[], dict] | None = None) -> list[dict]:
    """[{address, name, connected}] from the system's own Bluetooth report."""
    data = (read or (lambda: _profiler("SPBluetoothDataType")))()
    blk = (data.get("SPBluetoothDataType") or [{}])[0]
    out = []
    for key, connected in (("device_connected", True), ("device_not_connected", False)):
        for item in blk.get(key, []):
            for name, props in item.items():
                out.append({"address": props.get("device_address", ""), "name": name, "connected": connected})
    return out


def default_output_name(read: Callable[[], dict] | None = None) -> str | None:
    data = (read or (lambda: _profiler("SPAudioDataType")))()
    for grp in data.get("SPAudioDataType", []):
        for dev in grp.get("_items", []):
            if dev.get("coreaudio_default_audio_output_device") == "spaudio_yes":
                return dev.get("_name")
    return None


def check_connected(device_hash: str, salt: str, read_bt: Callable[[], dict] | None = None) -> Check:
    match = [d for d in paired_devices(read_bt) if short_hash(d["address"], salt=salt) == device_hash]
    return Check("earbuds_connected", bool(match and match[0]["connected"]),
                 {"device": device_hash, "paired_entries_with_that_hash": len(match), "connected": bool(match and match[0]["connected"]), "source": "system_profiler SPBluetoothDataType"})


def check_playing_through(device_hash: str, salt: str, expected_track_id: str, expected_artist: str, *, read_bt: Callable[[], dict] | None = None,
                          read_audio: Callable[[], dict] | None = None, read_player: Callable[[], dict | None]) -> Check:
    match = [d for d in paired_devices(read_bt) if short_hash(d["address"], salt=salt) == device_hash]
    out_name = default_output_name(read_audio)
    player = read_player() or {}
    output_is_device = bool(match and out_name and out_name == match[0]["name"])
    playing = player.get("state") == "playing"
    right_track = bool(expected_track_id and player.get("track_id") == expected_track_id)
    right_artist = bool(expected_artist) and expected_artist.strip().lower() in [n.strip().lower() for n in (player.get("artist") or "").split(",")]
    return Check("plays_through_earbuds", output_is_device and playing and right_track and right_artist,
                 {"default_output_is_the_device": output_is_device, "player_state": player.get("state"), "track_is_the_chosen_one": right_track, "artist_is_the_requested_one": right_artist,
                  "source": "system_profiler SPAudioDataType + the Spotify app's own state"})


def restore_confirmed(previous_output_id: str, current_output_id: str | None) -> Check:
    return Check("previous_output_restored", bool(previous_output_id) and current_output_id == previous_output_id, {"previous": previous_output_id, "now": current_output_id})
