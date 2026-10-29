"""Claude through the `claude` CLI in print mode: uses the user's own subscription login.

The call is a bare completion: no tools, no slash commands, no settings or project files
(it runs from an empty temp directory), no saved session.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import time

from envrec.contracts import ModelProfile
from envrec.model.base import Completion, ModelAdapter, ModelError


def build_argv(binary: str, model_id: str, system: str) -> list[str]:
    return [
        binary, "-p",
        "--model", model_id,
        "--system-prompt", system,
        "--tools", "",
        "--no-session-persistence",
        "--disable-slash-commands",
        "--setting-sources", "",
        "--output-format", "json",
    ]


def parse_cli_output(stdout: str) -> Completion:
    """The CLI may print a warning line before the JSON result; take the last JSON line."""
    line = next((ln for ln in reversed(stdout.splitlines()) if ln.lstrip().startswith("{")), None)
    if line is None:
        raise ModelError("claude CLI produced no JSON result")
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        raise ModelError("claude CLI result is not valid JSON") from None
    if data.get("is_error"):
        raise ModelError(f"claude CLI error: {str(data.get('result'))[:200]}")
    usage = data.get("usage") or {}
    prompt = sum(int(usage.get(k) or 0) for k in
                 ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    return Completion(
        text=str(data.get("result") or ""),
        prompt_tokens=prompt,
        completion_tokens=int(usage.get("output_tokens") or 0),
        cost_usd=data.get("total_cost_usd"),
    )


class ClaudeCliModel(ModelAdapter):
    def __init__(self, profile: ModelProfile, *, binary: str = "claude", timeout_s: float = 120.0) -> None:
        super().__init__(profile)
        self.binary = binary
        self.timeout_s = timeout_s

    async def complete(self, system: str, user: str) -> Completion:
        argv = build_argv(self.binary, self.profile.model_id, system)
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="envrec-claude-") as cwd:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, cwd=cwd,
                )
            except OSError as exc:
                raise ModelError(f"cannot start {self.binary!r}: {exc.strerror or type(exc).__name__}") from None
            try:
                out, err = await asyncio.wait_for(proc.communicate(user.encode()), self.timeout_s)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise ModelError(f"claude CLI timed out after {self.timeout_s:.0f}s") from None
        if proc.returncode != 0 and not out.strip():
            raise ModelError(f"claude CLI exited {proc.returncode}: {err.decode(errors='replace')[:200]}")
        done = parse_cli_output(out.decode(errors="replace"))
        return Completion(done.text, done.prompt_tokens, done.completion_tokens,
                          time.monotonic() - started, done.cost_usd)
