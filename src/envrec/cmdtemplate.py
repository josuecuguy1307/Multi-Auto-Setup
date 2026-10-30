"""The shape of a fixed command template: an argv with named, strictly checked placeholders. Neutral on purpose: the discovery code and the (retired) remote library both use it, and the
MAS commands must not have to import the retired `envrec.remote` package to get it."""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field

from envrec.contracts import Tier


class TemplateError(ValueError):
    """A placeholder value or a template name was rejected."""


Schema = Callable[[object], str]


@dataclass(frozen=True)
class Template:
    name: str
    argv: tuple[str, ...]
    tier: Tier
    placeholders: dict[str, Schema] = field(default_factory=dict)
    description: str = ""
    sends_stdin: bool = False  # a file transfer: the bytes go on stdin, never in argv

    def render(self, values: dict[str, object] | None = None) -> list[str]:
        values = dict(values or {})
        unknown = set(values) - set(self.placeholders)
        if unknown:
            raise TemplateError(f"template {self.name!r} has no placeholder(s) {sorted(unknown)}")
        missing = set(self.placeholders) - set(values)
        if missing:
            raise TemplateError(f"template {self.name!r} needs {sorted(missing)}")
        clean = {k: self.placeholders[k](v) for k, v in values.items()}
        out = []
        for part in self.argv:
            for n in re.findall(r"\{(\w+)\}", part):
                if n in clean:  # only declared placeholders; braces in fixed script text (e.g. ${10}) are left alone
                    part = part.replace("{" + n + "}", clean[n])
            out.append(part)
        return out

    def remote_command(self, values: dict[str, object] | None = None) -> str:
        """The single string ssh sends to the remote shell: every argv element shell-quoted."""
        return " ".join(shlex.quote(p) for p in self.render(values))
