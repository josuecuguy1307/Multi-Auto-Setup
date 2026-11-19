"""P0-SECRETS (MISSION 4.2: zero credentials in the repo, logs or prompts): a secret scan over the whole tree and the whole history.

What is scanned: every tracked file, every untracked file git does not ignore, and `git log --all -p` (every commit on every ref, with
its message). What counts as a secret: a private-key block WITH a key body (a bare "BEGIN ... PRIVATE KEY" header is a test fixture,
not a key), cloud and service tokens of their real length and shape with enough distinct characters (so "AKIA" + "A" * 16 fixtures
do not count), and key-store files (.pem/.key/.p12/.pfx/.kdbx/id_rsa...) tracked at any point. Every rule has a positive control,
assembled at run time so this file never matches itself.

Limits (declared): no generic "password = ..." heuristic (too noisy for a repo full of secret-handling tests); a secret in a format
not listed here is not found; files git ignores (runs/, .venv/) are not scanned because they are never published.
REAL: runs over the real repository.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

TOKENS = {
    "aws access key": r"\b(AKIA|ASIA)[0-9A-Z]{16}\b",
    "github token": r"\bgh[pousr]_[A-Za-z0-9]{36}\b",
    "github fine-grained token": r"\bgithub_pat_[A-Za-z0-9_]{60,}\b",
    "gitlab token": r"\bglpat-[A-Za-z0-9_-]{20}\b",
    "slack token": r"\bxox[abprs]-[A-Za-z0-9-]{20,}\b",
    "anthropic key": r"\bsk-ant-[A-Za-z0-9_-]{60,}\b",
    "openai key": r"\bsk-(proj-)?[A-Za-z0-9_-]{40,}\b",
    "google api key": r"\bAIza[0-9A-Za-z_-]{35}\b",
    "hugging face token": r"\bhf_[A-Za-z0-9]{34,}\b",
    "stripe live key": r"\b[rs]k_live_[A-Za-z0-9]{24,}\b",
}
KEY_BLOCK = re.compile(r"-----BEGIN ([A-Z0-9 ]*PRIVATE KEY|PGP PRIVATE KEY BLOCK)-----\s*(?:[A-Za-z0-9-]+: [^\n]*\n\s*)*([A-Za-z0-9+/=\s]{80,})")
KEY_FILES = re.compile(r"(^|/)(id_(rsa|dsa|ecdsa|ed25519)|[^/]+\.(pem|key|p12|pfx|kdbx|jks|keystore|ppk))$", re.IGNORECASE)
RANDOM_BODY = {"anthropic key", "openai key"}
MIN_DISTINCT = 10  # a real token's body has many distinct characters; fixtures like "A" * 16 or "abc" * 12 do not


def findings(text: str) -> list[str]:
    out = []
    for name, pat in TOKENS.items():
        for m in re.finditer(pat, text):
            tok = m.group(0)
            if len(set(tok)) < MIN_DISTINCT:
                continue
            if name in RANDOM_BODY and not (re.search(r"[A-Z]", tok) and re.search(r"[a-z]", tok) and re.search(r"[0-9]", tok)):
                continue  # these keys are random base64: an all-lowercase phrase like "sk-should-never-appear-..." is a fixture
            out.append(f"{name}: {tok[:12]}...")
    for m in KEY_BLOCK.finditer(text):
        body = re.sub(r"\s", "", m.group(2))
        if len(body) >= 64 and len(set(body)) >= 20:
            out.append(f"private key block ({m.group(1)})")
    return out


def git(*args) -> str:
    r = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, timeout=600)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")[-500:]
    return r.stdout.decode("utf-8", "replace")


def test_the_repository_is_a_git_repository_with_history():
    assert int(git("rev-list", "--all", "--count").strip()) > 0


def test_no_secret_in_any_tracked_or_untracked_file():
    files = [f for f in git("ls-files", "-z", "--cached", "--others", "--exclude-standard").split("\0") if f]
    assert len(files) > 100
    bad = {}
    for rel in files:
        p = ROOT / rel
        if not p.is_file() or p.stat().st_size > 20 << 20:
            continue
        if v := findings(p.read_bytes().decode("utf-8", "replace")):
            bad[rel] = v
    assert bad == {}


def test_no_secret_anywhere_in_the_history():
    log = git("log", "--all", "-p", "--no-color", "--no-ext-diff", "--text")
    assert len(log) > 1000
    assert findings(log) == []


def test_no_key_store_file_was_ever_tracked():
    names = set(git("log", "--all", "--name-only", "--format=").split("\n")) | set(git("ls-files").split("\n"))
    assert sorted(n for n in names if n and KEY_FILES.search(n)) == []


# ---- positive controls: each rule fires on a realistic secret built here, and stays quiet on the fixtures the repo really has --------

def _real(prefix: str, n: int, alphabet: str = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789") -> str:
    return prefix + (alphabet * 4)[:n]


@pytest.mark.parametrize("secret", [
    "AK" + "IA" + "QWERTYUIOP234567", "gh" + "p_" + _real("", 36), "github" + "_pat_" + _real("", 70), "gl" + "pat-" + _real("", 20),
    "xo" + "xb-" + _real("", 30), "sk-" + "ant-" + _real("api03-", 90), "sk-" + "proj-" + _real("", 48), "AI" + "za" + _real("", 35),
    "hf" + "_" + _real("", 36), "sk" + "_live_" + _real("", 30)])
def test_positive_control_every_token_rule_fires(secret):
    assert findings(f"config = {{'key': '{secret}'}}\n"), secret


def test_positive_control_a_private_key_block_with_a_body_fires():
    body = "\n".join([_real("", 64, "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7")] * 4)
    block = "-----BEGIN " + "PRIVATE KEY-----\n" + body + "\n-----END " + "PRIVATE KEY-----\n"
    assert findings(block)
    assert findings(block.replace("BEGIN PRIVATE", "BEGIN OPENSSH PRIVATE"))


def test_the_rules_ignore_the_fixtures_this_repo_uses():
    for fixture in ["-----BEGIN " + "OPENSSH PRIVATE KEY-----", "sk-" + "ant-TOP-SECRET-TOKEN-123456", "AK" + "IA" + "A" * 16,
                    "gh" + "p_" + "a" * 36, "sk-" + "abcdefghijklmnop12345", "STAND-IN-CREDENTIAL-0f3a",
                    "sk-" + "should-never-appear-in-any-result-1234567890"]:
        assert findings(f"x = '{fixture}'") == [], fixture


def test_positive_control_key_store_names():
    for n in ["keys/id_rsa", "deploy/server.pem", "certs/client.p12", "vault.kdbx", "a/b/id_ed25519"]:
        assert KEY_FILES.search(n), n
    for n in ["src/envrec_node/credentials.py", "skills/secret-entry/SKILL.md", "docs/keys.md", "id_rsa.pub"]:
        assert not KEY_FILES.search(n), n
