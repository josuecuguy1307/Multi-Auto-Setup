"""Small fixture matrix for the catalog generator: fake sources, no network."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

H = lambda c: c * 64  # noqa: E731  a valid-looking sha256


def manifest_raw(approved: bool = True) -> dict[str, Any]:
    return {
        "approved": approved, "disk_reserve_gib": 4, "max_artifact_gb": 3.0, "max_model_params_b": 8,
        "license_allowlist": ["apache-2.0", "mit"],
        "quantizations": ["Q4_K_M", "Q8_0"],
        "runtime_sources": [
            {"id": "llama.cpp", "status": "approve", "repo": "https://example.test/llama.cpp", "license": "MIT",
             "license_source": "https://example.test/LICENSE",
             "channel": {"kind": "github_releases", "include_prerelease": True, "tag_pattern": r"^b\d+$"},
             "formats": ["gguf"], "server_binary": "llama-server",
             "assets": [{"os": "linux", "arch": "arm64", "backend": "cpu", "pattern": r"^llama-b\d+-bin-ubuntu-arm64\.tar\.gz$"},
                        {"os": "linux", "arch": "amd64", "backend": "cpu", "pattern": r"^llama-b\d+-bin-ubuntu-x64\.tar\.gz$"},
                        {"os": "linux", "arch": "amd64", "backend": "cuda", "pattern": r"^llama-b\d+-bin-ubuntu-cuda-x64\.tar\.gz$"}]},
            {"id": "rustlm", "status": "approve", "repo": "https://example.test/rustlm", "license": "MIT",
             "license_source": "https://example.test/LICENSE2",
             "channel": {"kind": "github_releases", "include_prerelease": False, "tag_pattern": r"^v\d+\.\d+\.\d+$"},
             "formats": ["gguf", "safetensors"], "server_binary": "rustlm",
             "assets": [{"os": "linux", "arch": "arm64", "backend": "cpu", "pattern": r"^rustlm-arm64\.tar\.gz$"}]},
            {"id": "offrt", "status": "propose-off", "license": "MIT", "reason_off": "not wanted"},
            {"id": "badrt", "status": "exclude", "license": "MIT", "reason": "fetches outside the manifest"},
        ],
        "model_sources": [
            {"id": "acme", "status": "approve", "hub": "huggingface", "author": "Acme", "formats": ["gguf"]},
            {"id": "hub", "status": "propose-off", "author": "somehub", "formats": ["gguf"]},
            {"id": "vendorx", "status": "exclude", "seen": "restricted-use license"},
        ],
    }


def write_manifest(tmp: Path, approved: bool = True, mutate=None) -> Path:
    raw = manifest_raw(approved)
    if mutate:
        mutate(raw)
    p = tmp / "runtime_manifest.yaml"
    p.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return p


class FakeMeta:
    """Implements MetadataSource from dicts; records every call so tests can assert on what was read."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.rel = {
            "https://example.test/llama.cpp": [
                {"tag": "b200", "commit": "c" * 40, "prerelease": True, "assets": [
                    {"name": "llama-b200-bin-ubuntu-arm64.tar.gz", "url": "https://example.test/dl/a", "sha256": H("a"), "bytes": 18_000_000},
                    {"name": "llama-b200-bin-ubuntu-x64.tar.gz", "url": "https://example.test/dl/b", "sha256": None, "bytes": 22_000_000},
                    {"name": "llama-b200-bin-ubuntu-cuda-x64.tar.gz", "url": "https://example.test/dl/c", "sha256": H("c"), "bytes": 3_500_000_000}]},
                {"tag": "b199", "commit": "d" * 40, "prerelease": True, "assets": []},
                {"tag": "nightly", "commit": "e" * 40, "prerelease": True, "assets": []},
            ],
            "https://example.test/rustlm": [
                {"tag": "v1.0.0", "commit": "f" * 40, "prerelease": False, "assets": [
                    {"name": "rustlm-arm64.tar.gz", "url": "https://example.test/dl/r", "sha256": H("e"), "bytes": 36_000_000}]},
                {"tag": "v1.1.0-rc1", "commit": "9" * 40, "prerelease": True, "assets": []},
            ],
        }
        self.repos = {"Acme": [
            {"repo": "Acme/Model-0.5B-Instruct-GGUF", "license": "apache-2.0"},
            {"repo": "Acme/Model-1.5B-Instruct-GGUF", "license": "apache-2.0"},
            {"repo": "Acme/Research-3B-GGUF", "license": "vendor-research"},
            {"repo": "Acme/NoLicense-1B-GGUF", "license": None},
            {"repo": "Acme/Big-32B-GGUF", "license": "apache-2.0"},
        ]}
        self.files = {
            "Acme/Model-0.5B-Instruct-GGUF": {"revision": "r" * 40, "files": [
                {"path": "model-0.5b-instruct-q4_k_m.gguf", "bytes": 491_000_000, "sha256": H("1")},
                {"path": "model-0.5b-instruct-q8_0.gguf", "bytes": 676_000_000, "sha256": H("2")},
                {"path": "model-0.5b-instruct-q2_k.gguf", "bytes": 400_000_000, "sha256": H("3")},  # quant not wanted
                {"path": "README.md", "bytes": 1000, "sha256": None}]},
            "Acme/Model-1.5B-Instruct-GGUF": {"revision": "s" * 40, "files": [
                {"path": "model-1.5b-instruct-q4_k_m.gguf", "bytes": 1_117_000_000, "sha256": H("4")},
                {"path": "model-1.5b-instruct-q8_0.gguf", "bytes": 1_900_000_000, "sha256": None},  # no checksum
                {"path": "huge-1.5b-q8_0.gguf", "bytes": 3_500_000_000, "sha256": H("5")}]},          # over cap
            "Acme/Research-3B-GGUF": {"revision": "t" * 40, "files": [{"path": "r-3b-q4_k_m.gguf", "bytes": 1_000_000_000, "sha256": H("6")}]},
            "Acme/NoLicense-1B-GGUF": {"revision": "u" * 40, "files": [{"path": "n-1b-q4_k_m.gguf", "bytes": 500_000_000, "sha256": H("7")}]},
            "Acme/Big-32B-GGUF": {"revision": "v" * 40, "files": [
                {"path": "big-32b-q4_k_m-00001-of-00004.gguf", "bytes": 2_000_000_000, "sha256": H("8")},
                {"path": "big-32b-q4_k_m.gguf", "bytes": 2_000_000_000, "sha256": H("9")}]},
        }

    def releases(self, repo_url):
        self.calls.append(("releases", repo_url))
        return copy.deepcopy(self.rel[repo_url])

    def model_repos(self, author):
        self.calls.append(("model_repos", author))
        return copy.deepcopy(self.repos[author])

    def repo_files(self, repo):
        self.calls.append(("repo_files", repo))
        return copy.deepcopy(self.files[repo])
