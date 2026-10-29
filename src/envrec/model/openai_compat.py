"""OpenAI-compatible chat client. Works with ollama (`http://localhost:11434/v1`) and any similar server."""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

from envrec.contracts import ModelProfile
from envrec.model.base import Completion, ModelAdapter, ModelError


class OpenAICompatModel(ModelAdapter):
    def __init__(
        self,
        profile: ModelProfile,
        *,
        timeout_s: float = 120.0,
        max_tokens: int = 600,
        temperature: float = 0.0,
        json_mode: bool = False,
        extra_body: dict[str, Any] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(profile)
        self.timeout_s = timeout_s
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.json_mode = json_mode
        self.extra_body = dict(extra_body or {})
        self._client = client

    async def complete(self, system: str, user: str) -> Completion:
        body: dict[str, Any] = {
            "model": self.profile.model_id,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
            **self.extra_body,
        }
        if self.json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {}
        if self.profile.api_key_env:
            key = os.environ.get(self.profile.api_key_env)
            if key:
                headers["Authorization"] = f"Bearer {key}"
        url = self.profile.endpoint.rstrip("/") + "/chat/completions"

        started = time.monotonic()
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self.timeout_s)
        try:
            resp = await client.post(url, json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            raise ModelError(f"{type(exc).__name__}: {str(exc)[:200]}") from None
        except ValueError:
            raise ModelError("backend returned a body that is not JSON") from None
        finally:
            if owns_client:
                await client.aclose()

        try:
            text = data["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError, TypeError, AttributeError):
            raise ModelError("backend reply has no choices[0].message") from None
        usage = data.get("usage") or {}
        return Completion(
            text=text,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            latency_s=time.monotonic() - started,
        )
