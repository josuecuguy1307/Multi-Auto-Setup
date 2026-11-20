"""Withhold anything that looks like a credential before an inspection stores a single byte (MISSION 4.1 rule 8; D-040).

The inspector never opens a credential store (its allowlist names none). This is the second line: a careless or hostile host can put
a token inside a field the inspector is allowed to read (os-release, a package database, a device model). Every string the collectors
return passes redact(); a value that matches any rule below is replaced WHOLE by WITHHELD (no prefix or suffix is kept, so nothing of
the secret survives), and the count goes into the owner's text. Rules are shapes, not guesses: a word like "password" or a package
named base-passwd is left alone; `password=<something>` is not.
"""

from __future__ import annotations

import re

WITHHELD = "[withheld: credential-like]"

RULES = [
    ("private_key_block", r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    ("aws_access_key_id", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("github_token", r"\bgh[pousr]_[A-Za-z0-9]{36,}"),
    ("github_pat", r"\bgithub_pat_[A-Za-z0-9_]{22,}"),
    ("sk_api_key", r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}"),
    ("slack_token", r"\bxox[abposr]-[0-9A-Za-z-]{10,}"),
    ("google_api_key", r"\bAIza[0-9A-Za-z_-]{35}"),
    ("jwt", r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    ("url_with_password", r"[a-zA-Z][a-zA-Z0-9+.-]*://[^/\s:@]+:[^/\s@]+@"),
    ("assignment", r"(?i)\b(?:pass(?:word)?|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|client[_-]?secret)"
                   r"\s*[:=]\s*\S+"),
]
_COMPILED = [(name, re.compile(rx)) for name, rx in RULES]


def looks_like_credential(value: str) -> bool:
    return any(rx.search(value) for _, rx in _COMPILED)


def redact_value(value):
    if isinstance(value, str) and looks_like_credential(value):
        return WITHHELD
    return value


def redact(tree):
    """(a redacted copy of tree, how many strings were withheld). Dict keys are the device's own and are left as they are."""
    count = 0

    def walk(x):
        nonlocal count
        if isinstance(x, dict):
            return {k: walk(v) for k, v in x.items()}
        if isinstance(x, list):
            return [walk(v) for v in x]
        y = redact_value(x)
        if y is not x and y == WITHHELD:
            count += 1
        return y

    return walk(tree), count


def count_withheld(tree) -> int:
    if isinstance(tree, dict):
        return sum(count_withheld(v) for v in tree.values())
    if isinstance(tree, list):
        return sum(count_withheld(v) for v in tree)
    return 1 if tree == WITHHELD else 0
