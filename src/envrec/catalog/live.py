"""Live metadata reader: GitHub release listings and Hugging Face model listings, nothing else.

Only constructed by the generator command after the manifest is approved. Every request goes through one
host allowlist; it lists, it never downloads artifacts. Checksums come from the sources themselves: the GitHub
asset `digest` field ("sha256:...") and the Hugging Face LFS object id (a sha256).
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

ALLOWED_HOSTS = {"api.github.com", "huggingface.co"}


class HostNotAllowed(PermissionError):
    pass


class LiveMetadata:
    def __init__(self, client: httpx.Client | None = None, github_token: str | None = None, *, pace_s: float = 0.0, cache_dir: Path | None = None, max_retries: int = 6) -> None:
        self.client = client or httpx.Client(timeout=30, follow_redirects=False)
        self.pace_s, self.cache_dir, self.max_retries = pace_s, cache_dir, max_retries  # a pause between requests, answers kept on disk so a re-run asks nothing again, and how often a 429 is waited out
        if cache_dir:
            cache_dir.mkdir(parents=True, exist_ok=True)
        self.headers = {"accept": "application/json", "user-agent": "envrec-catalog"}
        if github_token:
            self.headers["authorization"] = f"Bearer {github_token}"
        self.requests: list[str] = []

    def _get(self, url: str, **params: Any) -> Any:
        host = urlparse(url).hostname
        if host not in ALLOWED_HOSTS:
            raise HostNotAllowed(f"{host} is not a metadata host the catalog may read")
        key = self.cache_dir / (hashlib.sha256(json.dumps([url, params], sort_keys=True).encode()).hexdigest() + ".json") if self.cache_dir and host == "huggingface.co" else None
        if key and key.is_file():
            return json.loads(key.read_text())
        # the token only goes to GitHub, never to the other host
        headers = {k: v for k, v in self.headers.items() if k != "authorization" or host == "api.github.com"}
        for attempt in range(self.max_retries + 1):
            self.requests.append(url)
            if self.pace_s:
                time.sleep(self.pace_s)
            r = self.client.get(url, params=params or None, headers=headers)
            if (r.status_code == 429 or r.status_code in (500, 502, 503, 504)) and attempt < self.max_retries:  # rate limited or a passing server error: wait as long as the source says (or back off), then ask again
                time.sleep(min(float(r.headers.get("retry-after", 0)) or 2.0 * 2 ** attempt, 120.0))
                continue
            r.raise_for_status()
            out = r.json()
            if key:
                key.write_text(json.dumps(out))
            return out
        raise RuntimeError("unreachable")

    def releases(self, repo_url: str) -> list[dict[str, Any]]:
        path = urlparse(repo_url).path.strip("/")
        out = []
        for page in (1, 2, 3):
            data = self._get(f"https://api.github.com/repos/{path}/releases", per_page=100, page=page)
            if not data:
                break
            for r in data:
                out.append({"tag": r["tag_name"], "commit": r.get("target_commitish") if len(r.get("target_commitish", "")) == 40 else None,
                            "prerelease": bool(r.get("prerelease")),
                            "assets": [{"name": a["name"], "url": a["browser_download_url"], "bytes": a["size"],
                                        "sha256": (a.get("digest") or "").removeprefix("sha256:") or None} for a in r.get("assets", [])]})
        return out

    def model_repo(self, repo: str) -> dict[str, Any]:
        """One repository the manifest names (a source approved for specific repos): its license from its own page, without listing everything its author publishes."""
        info = self._get(f"https://huggingface.co/api/models/{repo}")
        lic = next((t.split(":", 1)[1] for t in info.get("tags", []) if t.startswith("license:")), None)
        return {"repo": info.get("id", repo), "license": lic}

    def model_repos(self, author: str) -> list[dict[str, Any]]:
        data = self._get("https://huggingface.co/api/models", author=author, filter="gguf", limit=100, full="false")
        out = []
        for m in data:
            info = self._get(f"https://huggingface.co/api/models/{m['id']}")
            lic = next((t.split(":", 1)[1] for t in info.get("tags", []) if t.startswith("license:")), None)
            out.append({"repo": m["id"], "license": lic})
        return out

    def repo_files(self, repo: str) -> dict[str, Any]:
        info = self._get(f"https://huggingface.co/api/models/{repo}", blobs="true")
        tree = self._get(f"https://huggingface.co/api/models/{repo}/tree/{info['sha']}")
        files = [{"path": f["path"], "bytes": (f.get("lfs") or {}).get("size", f.get("size", 0)),
                  "sha256": (f.get("lfs") or {}).get("oid")} for f in tree if f.get("type") == "file"]
        return {"revision": info["sha"], "files": files}
