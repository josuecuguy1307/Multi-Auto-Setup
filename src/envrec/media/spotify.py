"""Spotify's own door for the owner's library: OAuth 2.0 with PKCE (no client secret), the minimal scope `user-library-read`, tokens in the macOS Keychain.

The owner creates an app in their Spotify developer dashboard (their own gesture), allows the redirect http://127.0.0.1:8765/callback, and runs `envrec spotify-login`: a browser
opens, the owner logs in and clicks Agree. Nothing of the library is stored: `find_saved_tracks` reads pages in memory and returns only the matching tracks' ids and titles.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import subprocess
import threading
import time
import urllib.parse
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

SERVICE, ACCOUNT = "envrec-spotify", "envrec"
SCOPE = "user-library-read"
REDIRECT_PORT = 8765
REDIRECT = f"http://127.0.0.1:{REDIRECT_PORT}/callback"
AUTH_URL, TOKEN_URL, API = "https://accounts.spotify.com/authorize", "https://accounts.spotify.com/api/token", "https://api.spotify.com/v1"
MAX_PAGES = 12  # 600 saved tracks at most per search: a bound, not a crawl of the library


class SpotifyError(Exception):
    """kind: login | network | denied"""

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(f"{kind}: {detail}")
        self.kind, self.detail = kind, detail


# ---- the Keychain (the secret is never an argument of a process) ------------------------------------------------------------------------------------

def load_credentials(run: Callable = subprocess.run) -> dict | None:
    done = run(["security", "find-generic-password", "-s", SERVICE, "-a", ACCOUNT, "-w"], capture_output=True, text=True, timeout=15)
    if done.returncode != 0 or not done.stdout.strip():
        return None
    try:
        data = json.loads(done.stdout.strip())
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def save_credentials(creds: dict, run: Callable = subprocess.run) -> None:
    """`security -i` reads its command from stdin, so the JSON never appears in a process list."""
    payload = json.dumps(creds).replace("'", "")
    done = run(["security", "-i"], input=f"add-generic-password -U -s {SERVICE} -a {ACCOUNT} -w '{payload}'\n", capture_output=True, text=True, timeout=15)
    if done.returncode != 0:
        raise SpotifyError("login", "the Keychain did not accept the token")


# ---- PKCE ------------------------------------------------------------------------------------------------------------------------------------------

def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(client_id: str, challenge: str, state: str) -> str:
    q = {"response_type": "code", "client_id": client_id, "scope": SCOPE, "redirect_uri": REDIRECT, "code_challenge_method": "S256", "code_challenge": challenge, "state": state}
    return AUTH_URL + "?" + urllib.parse.urlencode(q)


def _token_request(form: dict, post: Callable = httpx.post) -> dict:
    try:
        r = post(TOKEN_URL, data=form, timeout=20)
    except httpx.HTTPError as exc:
        raise SpotifyError("network", type(exc).__name__) from exc
    if r.status_code != 200:
        raise SpotifyError("login", f"Spotify answered {r.status_code} to the token request")
    body = r.json()
    return {"access_token": body["access_token"], "refresh_token": body.get("refresh_token"), "expires_at": time.time() + int(body.get("expires_in", 3600)) - 60}


def login(client_id: str, open_url: Callable[[str], None], *, post: Callable = httpx.post, save: Callable[[dict], None] = save_credentials, wait_s: float = 180.0, port: int = REDIRECT_PORT) -> None:
    """The owner's login: open the authorization page, take the one redirect on 127.0.0.1, exchange the code, keep the tokens in the Keychain."""
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)
    got: dict = {}
    done = threading.Event()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):  # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            ok = q.get("state", [""])[0] == state and "code" in q
            got["code"] = q["code"][0] if ok else None
            got["error"] = q.get("error", [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"You can close this tab and go back to the terminal." if ok else b"Login was not completed.")
            done.set()

    httpd = HTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        open_url(authorize_url(client_id, challenge, state))
        if not done.wait(wait_s) or not got.get("code"):
            raise SpotifyError("login", "no login was completed" + (f" ({got['error']})" if got.get("error") else ""))
    finally:
        httpd.shutdown()
        httpd.server_close()
    tokens = _token_request({"grant_type": "authorization_code", "code": got["code"], "redirect_uri": REDIRECT, "client_id": client_id, "code_verifier": verifier}, post)
    save({"client_id": client_id, **tokens})


def access_token(*, load: Callable[[], dict | None] = load_credentials, save: Callable[[dict], None] = save_credentials, post: Callable = httpx.post, now: Callable[[], float] = time.time) -> str:
    creds = load()
    if not creds or not creds.get("access_token"):
        raise SpotifyError("login", "no Spotify login on this computer: run `envrec spotify-login` once")
    if now() < creds.get("expires_at", 0):
        return creds["access_token"]
    if not creds.get("refresh_token"):
        raise SpotifyError("login", "the Spotify login expired: run `envrec spotify-login` again")
    fresh = _token_request({"grant_type": "refresh_token", "refresh_token": creds["refresh_token"], "client_id": creds["client_id"]}, post)
    creds = {**creds, **{k: v for k, v in fresh.items() if v}}
    save(creds)
    return creds["access_token"]


# ---- reading the library: only what matches, only ids and titles --------------------------------------------------------------------------------------

def find_saved_tracks(artist: str, token: str, *, limit: int = 5, get: Callable = httpx.get) -> list[dict]:
    """The owner's saved tracks whose artist is `artist` (case-insensitive, whole name), newest saved first: [{id, title, artist}]. Pages are read in memory and dropped."""
    want = " ".join(artist.lower().split())
    found: list[dict] = []
    for page in range(MAX_PAGES):
        try:
            r = get(f"{API}/me/tracks", params={"limit": 50, "offset": page * 50}, headers={"Authorization": f"Bearer {token}"}, timeout=20)
        except httpx.HTTPError as exc:
            raise SpotifyError("network", type(exc).__name__) from exc
        if r.status_code in (401, 403):
            raise SpotifyError("login", f"Spotify refused the token ({r.status_code}): run `envrec spotify-login` again")
        if r.status_code != 200:
            raise SpotifyError("network", f"Spotify answered {r.status_code}")
        items = r.json().get("items", [])
        for it in items:
            tr = it.get("track") or {}
            names = [a.get("name", "") for a in tr.get("artists", [])]
            if tr.get("id") and any(" ".join(n.lower().split()) == want for n in names):
                found.append({"id": tr["id"], "title": tr.get("name", ""), "artist": next(n for n in names if " ".join(n.lower().split()) == want)})
                if len(found) >= limit:
                    return found
        if len(items) < 50:
            break
    return found
