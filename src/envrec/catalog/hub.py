"""Reading a model hub's own account of a repository at a PINNED revision, and the first bytes of a file by HTTP range request. Nothing is downloaded beyond a GGUF header (a few MiB, the weights are never
read). Only the hosts in ALLOWED_HOSTS are asked; a redirect is followed only inside the hub's own domains (the file servers behind it).

Every number this module returns is a fact read from the source (label: "verified-by-code"); the model card's own words come back separately under `declared`."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from envrec.catalog.gguf import GgufError

ALLOWED_HOSTS = ("huggingface.co",)
REDIRECT_SUFFIXES = (".huggingface.co", ".hf.co", "huggingface.co")
_REV = re.compile(r"^[0-9a-f]{40}$")
_INFO_FIELDS = ("gguf", "gated", "private", "tags", "pipeline_tag", "cardData", "author", "sha", "disabled", "library_name")


class HubError(RuntimeError):
    """The hub did not answer as asked (host refused, bad revision, HTTP error). Never hidden: the verification report names it."""


def _check_host(url: str) -> None:
    host = urlparse(url).hostname or ""
    if host not in ALLOWED_HOSTS:
        raise HubError(f"host {host!r} is not on the list of hosts this tool may ask ({', '.join(ALLOWED_HOSTS)})")


class HttpRange:
    """A ByteSource over an HTTP URL that supports range requests. `size` comes from the Content-Range of the first request."""

    def __init__(self, client: httpx.Client, url: str) -> None:
        _check_host(url)
        self._c, self._url = client, url
        self.size = self._probe()

    def _get(self, start: int, end: int) -> httpx.Response:
        url = self._url
        for _ in range(6):  # follow redirects by hand so every hop is checked against the hub's domains
            r = self._c.get(url, headers={"Range": f"bytes={start}-{end}"}, follow_redirects=False)
            if r.status_code in (301, 302, 303, 307, 308):
                nxt = str(httpx.URL(url).join(r.headers["location"]))
                host = urlparse(nxt).hostname or ""
                if not host.endswith(REDIRECT_SUFFIXES):
                    raise HubError(f"a redirect left the hub's domains: {host!r}")
                url = nxt
                continue
            return r
        raise HubError("too many redirects")

    def _probe(self) -> int:
        r = self._get(0, 0)
        if r.status_code not in (200, 206):
            raise HubError(f"HTTP {r.status_code} for {self._url}")
        cr = r.headers.get("content-range", "")
        m = re.match(r"bytes \d+-\d+/(\d+)$", cr)
        if m:
            return int(m.group(1))
        if r.status_code == 200:
            return int(r.headers.get("content-length", 0)) or 0
        raise GgufError(f"the server gave no usable Content-Range ({cr!r})")

    def read_at(self, offset: int, n: int) -> bytes:
        r = self._get(offset, offset + n - 1)
        if r.status_code not in (200, 206):
            raise HubError(f"HTTP {r.status_code} reading {n} bytes at {offset}")
        return r.content[:n]


class Hub:
    """The hub API at a pinned revision, with an on-disk cache so a re-run does not ask again (and a test can run with none)."""

    def __init__(self, client: httpx.Client | None = None, cache_dir: Path | None = None) -> None:
        self._c = client or httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0), headers={"user-agent": "mas-catalog-verify/1"})
        self._cache = cache_dir
        if cache_dir:
            cache_dir.mkdir(parents=True, exist_ok=True)

    def _cached(self, name: str) -> Any | None:
        if self._cache and (self._cache / name).is_file():
            return json.loads((self._cache / name).read_text())
        return None

    def _store(self, name: str, value: Any) -> Any:
        if self._cache:
            (self._cache / name).write_text(json.dumps(value))
        return value

    def _json(self, url: str) -> Any:
        _check_host(url)
        r = self._c.get(url, follow_redirects=False)
        if r.status_code != 200:
            raise HubError(f"HTTP {r.status_code} for {url}")
        return r.json()

    def repo_info(self, repo: str, revision: str) -> dict[str, Any]:
        """The hub's account of the repo at this revision: gated flag, tags, the license on the card, the pipeline tag, the author. `declared` is the card's words."""
        if not _REV.match(revision):
            raise HubError(f"{revision!r} is not a pinned (40 hex) revision")
        key = f"info__{repo.replace('/', '__')}__{revision}.json"
        hit = self._cached(key)
        if hit is not None:
            return hit
        q = "&".join(f"expand[]={f}" for f in _INFO_FIELDS)
        raw = self._json(f"https://huggingface.co/api/models/{repo}/revision/{revision}?{q}")
        card = raw.get("cardData") or {}
        tags = raw.get("tags") or []
        out = {"repo": repo, "revision": raw.get("sha"), "author": raw.get("author") or repo.split("/")[0], "gated": raw.get("gated", False), "private": bool(raw.get("private")),
               "disabled": bool(raw.get("disabled")), "pipeline_tag": raw.get("pipeline_tag"), "tags": tags, "library_name": raw.get("library_name"),
               "card_license": (card.get("license") if isinstance(card.get("license"), str) else None) or next((t.split(":", 1)[1] for t in tags if t.startswith("license:")), None),
               "base_models": [t for t in tags if t.startswith("base_model:")], "hub_gguf": raw.get("gguf") or {}}
        out["hub_gguf"].pop("chat_template", None)  # the header is read directly; the hub's copy is not kept
        return self._store(key, out)

    def tree(self, repo: str, revision: str) -> dict[str, dict[str, Any]]:
        """path -> {size, sha256 (the LFS oid: the file's SHA-256), oid} for every file at the pinned revision."""
        key = f"tree__{repo.replace('/', '__')}__{revision}.json"
        hit = self._cached(key)
        if hit is not None:
            return hit
        out: dict[str, dict[str, Any]] = {}
        url: str | None = f"https://huggingface.co/api/models/{repo}/tree/{revision}?recursive=1&limit=1000"
        while url:
            _check_host(url)
            r = self._c.get(url, follow_redirects=False)
            if r.status_code != 200:
                raise HubError(f"HTTP {r.status_code} listing {repo}@{revision[:8]}")
            for f in r.json():
                if f.get("type") == "file":
                    out[f["path"]] = {"size": f.get("size"), "sha256": (f.get("lfs") or {}).get("oid"), "oid": f.get("oid")}
            m = re.search(r'<([^>]+)>;\s*rel="next"', r.headers.get("link", ""))
            url = m.group(1) if m else None
        return self._store(key, out)

    def range_source(self, repo: str, revision: str, path: str) -> HttpRange:
        return HttpRange(self._c, f"https://huggingface.co/{repo}/resolve/{revision}/{path}")
