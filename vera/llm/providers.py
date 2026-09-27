"""Minimal httpx implementations for common chat-completion APIs."""

from __future__ import annotations

from typing import Protocol

import httpx


class Composer(Protocol):
    name: str

    def complete(self, system: str, user: str, *, temperature: float = 0.0,
                 max_tokens: int = 900, timeout: float = 6.0,
                 seed: int | None = 0) -> str: ...


class ProviderUnavailable(RuntimeError):
    pass


class NoopProvider:
    name = "deterministic-fallback"

    def complete(self, system: str, user: str, *, temperature: float = 0.0,
                 max_tokens: int = 900, timeout: float = 6.0,
                 seed: int | None = 0) -> str:
        raise ProviderUnavailable("No LLM provider is configured")


class _HTTPProvider:
    def __init__(self, name: str, endpoint: str, api_key: str, model: str) -> None:
        self.name, self.endpoint, self.api_key, self.model = name, endpoint.rstrip("/"), api_key, model

    def _post(self, path: str, headers: dict[str, str], payload: dict,
              timeout: float) -> dict:
        try:
            response = httpx.post(self.endpoint + path, headers=headers, json=payload,
                                  timeout=max(0.05, timeout))
            if response.status_code >= 400:
                # Do not include response text or headers; they can echo credentials.
                raise ProviderUnavailable(f"{self.name} returned HTTP {response.status_code}")
            return response.json()
        except ProviderUnavailable:
            raise
        except httpx.TimeoutException as exc:
            raise ProviderUnavailable(f"{self.name} timed out") from exc
        except httpx.ConnectError:
            # Preserve this typed failure so the adapter can perform its one
            # permitted connection-only retry.
            raise
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"{self.name} connection failed") from exc
        except (ValueError, TypeError) as exc:
            raise ProviderUnavailable(f"{self.name} returned invalid JSON") from exc


class OpenAICompatProvider(_HTTPProvider):
    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        super().__init__("openai_compat", base_url or "https://api.openai.com/v1", api_key, model)

    def complete(self, system: str, user: str, *, temperature: float = 0.0,
                 max_tokens: int = 900, timeout: float = 6.0,
                 seed: int | None = 0) -> str:
        payload = {"model": self.model, "messages": [
            {"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": temperature, "max_tokens": max_tokens}
        if seed is not None:
            payload["seed"] = seed
        data = self._post("/chat/completions", {"Authorization": f"Bearer {self.api_key}",
                                                "Content-Type": "application/json"}, payload, timeout)
        return str(data["choices"][0]["message"]["content"])


class AnthropicProvider(_HTTPProvider):
    def __init__(self, api_key: str, model: str) -> None:
        super().__init__("anthropic", "https://api.anthropic.com/v1", api_key, model)

    def complete(self, system: str, user: str, *, temperature: float = 0.0,
                 max_tokens: int = 900, timeout: float = 6.0,
                 seed: int | None = 0) -> str:
        data = self._post("/messages", {"x-api-key": self.api_key,
                                        "anthropic-version": "2023-06-01",
                                        "Content-Type": "application/json"},
                          {"model": self.model, "system": system,
                           "messages": [{"role": "user", "content": user}],
                           "temperature": temperature, "max_tokens": max_tokens}, timeout)
        return str(data["content"][0]["text"])


class GeminiProvider(_HTTPProvider):
    def __init__(self, api_key: str, model: str) -> None:
        super().__init__("gemini", "https://generativelanguage.googleapis.com/v1beta", api_key, model)

    def complete(self, system: str, user: str, *, temperature: float = 0.0,
                 max_tokens: int = 900, timeout: float = 6.0,
                 seed: int | None = 0) -> str:
        path = f"/models/{self.model}:generateContent?key={self.api_key}"
        data = self._post(path, {"Content-Type": "application/json"}, {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
        }, timeout)
        return str(data["candidates"][0]["content"]["parts"][0]["text"])
