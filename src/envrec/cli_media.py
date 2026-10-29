"""`envrec enroll-local` and `envrec spotify-login`: the owner's two gestures for Mission 2. Both are interactive and human-only."""

from __future__ import annotations

import argparse
import subprocess
import sys


def cmd_enroll_local(args) -> int:
    """Enroll THIS computer for effects on its own Bluetooth (already-paired devices), audio output and media apps. The owner types the name and yes, as for any device."""
    from envrec.enroll.enroll import EnrollmentRefused, enroll
    from envrec.inventory.report import Candidate
    from envrec.mission.models import Transport
    from envrec.store import Store
    name = "This Mac door" if args.door else "This Mac"
    cand = Candidate(name=name, kind="host", seen_via="this is the computer you are typing on", host="127.0.0.1")
    try:
        with Store(args.store) as store:
            res = enroll(cand, targets_path=args.targets, store=store, check=lambda e, t: True, transport=Transport.local, prefill={"scope": "door" if args.door else "bluetooth_audio_media"})
    except EnrollmentRefused as exc:
        print(f"Refused: {exc}")
        return 2
    print(res.reason)
    return 0 if res.enrolled else 1


def cmd_enroll_proposed(args) -> int:
    """Enroll the computer a door found. Address, user, computer name and host key come from what the computer itself reported through the one-time link; the owner types the name and yes."""
    from envrec.doors.proposal import read_target_proposal
    from envrec.enroll.enroll import EnrollmentRefused, enroll
    from envrec.inventory.report import Candidate
    from envrec.mission.models import Transport
    from envrec.store import Store
    p = read_target_proposal(args.proposal)
    cand = Candidate(name=p["name"], kind="host", seen_via=f"the door (it reported SSH host key {p['host_key_fingerprint']})", host=p["host"],
                     facts={"os.name": (p["os"], "tested", 0.9)} if p.get("os") else {})
    try:
        with Store(args.store) as store:
            res = enroll(cand, targets_path=args.targets, store=store, check=lambda e, t: True, transport=Transport.ssh, prefill={"ssh_user": p["ssh_user"], "ssh_port": 22})
    except EnrollmentRefused as exc:
        print(f"Refused: {exc}")
        return 2
    print(res.reason)
    return 0 if res.enrolled else 1


def cmd_consent_network(args) -> int:
    """Record that the owner said this network is theirs. Read-only discovery on it then needs no prompt. Bound to the network's fingerprint: another network is not covered."""
    from envrec.consent import NetworkConsent, current_network
    from envrec.contracts import Actor, Event
    from envrec.store import Store
    net = current_network()
    if net is None:
        print("Cannot tell which network this is (no default route or gateway address): nothing recorded.")
        return 2
    row = NetworkConsent().record(net, said=args.said)
    with Store(args.store) as st:
        st.append_event(Event(actor=Actor.human, type="network_consent", payload={"subnet": net["subnet"], "fingerprint": net["fingerprint"], "said": args.said[:200]}))
    print(f"Recorded: the owner said {row['subnet']} is theirs ({row['at']}). Other networks are not covered.")
    return 0


def cmd_spotify_login(args) -> int:
    """Log in to YOUR Spotify (OAuth with PKCE, scope user-library-read). A browser opens; you log in and click Agree. The token goes to the macOS Keychain."""
    from envrec.guard import LOOP_ACTIVE
    from envrec.media import spotify
    if LOOP_ACTIVE.get() or not sys.stdin.isatty():
        print("Refused: logging in is a human gesture and needs a terminal.")
        return 2
    print("Opening the Spotify login page in your browser. Redirect URI this app must allow: " + spotify.REDIRECT)
    try:
        spotify.login(args.client_id, lambda url: subprocess.run(["open", url], check=False))
    except spotify.SpotifyError as exc:
        print(f"Not logged in: {exc.detail}")
        return 1
    print("Logged in. The token is in your Keychain (service envrec-spotify); nothing was written to this folder.")
    return 0


def add_media_commands(sub) -> None:
    e = sub.add_parser("enroll-local", help="enroll THIS computer for effects on its own paired Bluetooth devices, audio output and media apps (needs a terminal)")
    e.add_argument("--targets", default="targets.yaml")
    e.add_argument("--store", default="runs/fleet")
    e.add_argument("--door", action="store_true", help="enroll this computer as the door to a computer that is not enrolled yet (a key, a one-time link, a pairing, a proposal)")
    e.set_defaults(func=cmd_enroll_local)
    cn = sub.add_parser("consent-network", help="record that you told the harness this network is yours (read-only discovery on it then needs no prompt)")
    cn.add_argument("--said", required=True, help="the owner's words, for the record")
    cn.add_argument("--store", default="runs/fleet")
    cn.set_defaults(func=cmd_consent_network)
    pr = sub.add_parser("enroll-proposed", help="enroll the computer the door found: you see what it reported and type its name and yes (needs a terminal)")
    pr.add_argument("--proposal", default="proposals/targets.proposed.yaml")
    pr.add_argument("--targets", default="targets.yaml")
    pr.add_argument("--store", default="runs/fleet")
    pr.set_defaults(func=cmd_enroll_proposed)
    s = sub.add_parser("spotify-login", help="log in to your own Spotify (library read-only); the token goes to the Keychain")
    s.add_argument("--client-id", required=True, help="the Client ID of the app you created in your Spotify developer dashboard (no secret is needed)")
    s.set_defaults(func=cmd_spotify_login)
