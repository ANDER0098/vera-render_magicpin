"""Provider selection, failover, retries, deadline bounds and circuit breakers."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

from ..config import Settings
from .cache import ResponseCache, content_key
from .providers import (AnthropicProvider, Composer, GeminiProvider,
                        NoopProvider, OpenAICompatProvider, ProviderUnavailable)

log = logging.getLogger("vera.llm")


@dataclass
class _Circuit:
    failures: int = 0
    opened_at: float = 0.0


class LLMAdapter:
    def __init__(self, providers: list[Composer] | None = None,
                 cache: ResponseCache | None = None, *, timeout: float = 5.5) -> None:
        self.providers = providers or [NoopProvider()]
        self.cache = cache or ResponseCache()
        self.timeout = min(7.0, max(0.05, float(timeout)))
        self._circuits = {provider.name: _Circuit() for provider in self.providers}

    @property
    def name(self) -> str:
        return self.providers[0].name if self.providers else "deterministic-fallback"

    @property
    def model_name(self) -> str:
        provider = self.providers[0]
        return str(getattr(provider, "model", "deterministic-fallback"))

    def complete(self, system: str, user: str, *, temperature: float = 0.0,
                 max_tokens: int = 900, timeout: float | None = None,
                 seed: int | None = 0, prompt_version: str = "v1") -> str:
        available = min(self.timeout, max(0.05, float(timeout or self.timeout)))
        errors: list[str] = []
        for provider in self.providers:
            circuit = self._circuits.setdefault(provider.name, _Circuit())
            now = time.monotonic()
            if circuit.failures >= 3 and now - circuit.opened_at < 60.0:
                errors.append("circuit_open")
                continue
            key = content_key(provider.name, str(getattr(provider, "model", "")),
                              prompt_version, system, user)
            cached = self.cache.get(key)
            if cached is not None:
                return cached
            for attempt in range(2):
                try:
                    value = provider.complete(system, user, temperature=temperature,
                                              max_tokens=max_tokens, timeout=available, seed=seed)
                    self.cache.put(key, value)
                    circuit.failures = 0
                    circuit.opened_at = 0.0
                    return value
                except Exception as exc:
                    # One retry only for a connection error; never retry HTTP status errors.
                    retryable = isinstance(exc, httpx.ConnectError)
                    if retryable and attempt == 0:
                        continue
                    circuit.failures += 1
                    if circuit.failures >= 3:
                        circuit.opened_at = time.monotonic()
                    errors.append(type(exc).__name__)
                    log.warning("provider_failed provider=%s error=%s", provider.name,
                                type(exc).__name__)
                    break
        raise ProviderUnavailable("No configured provider completed the request: " + ",".join(errors))


def _provider(name: str, settings: Settings, model: str = "") -> Composer:
    normalized = name.strip().lower()
    chosen_model = model or settings.llm_model
    if normalized in {"", "noop", "none", "deterministic-fallback"}:
        return NoopProvider()
    if normalized in {"openai", "openai_compat", "zai", "glm", "deepseek", "groq",
                      "openrouter", "together", "vllm", "ollama"}:
        base_url = settings.base_url
        default_model = "gpt-4o-mini"
        if normalized == "ollama":
            base_url = base_url or "http://localhost:11434/v1"
            default_model = "llama3.1"
        elif normalized == "vllm":
            base_url = base_url or "http://localhost:8000/v1"
        return OpenAICompatProvider(base_url, settings.api_key, chosen_model or default_model)
    if normalized == "anthropic":
        return AnthropicProvider(settings.api_key, chosen_model or "claude-3-5-haiku-latest")
    if normalized == "gemini":
        return GeminiProvider(settings.api_key, chosen_model or "gemini-2.0-flash")
    return NoopProvider()


def build_adapter(settings: Settings | None = None,
                  cache: ResponseCache | None = None) -> LLMAdapter:
    settings = settings or Settings.from_env()
    provider_names = [part.strip() for part in settings.provider.split(",") if part.strip()]
    local_names = {"ollama", "vllm"}
    if not provider_names or provider_names[0].lower() in {"noop", "none"}:
        return LLMAdapter([NoopProvider()], cache, timeout=settings.llm_timeout)
    fallback_names = [p.strip() for p in settings.fallback_provider.split(",") if p.strip()]
    if not settings.api_key:
        # Local OpenAI-compatible servers do not need credentials. Ignore remote
        # entries in a mixed failover chain when there is no API key configured.
        local_primary = [name for name in provider_names if name.lower() in local_names]
        local_fallback = [name for name in fallback_names if name.lower() in local_names]
        provider_names = local_primary or local_fallback
        fallback_names = local_fallback if local_primary else []
        if not provider_names:
            return LLMAdapter([NoopProvider()], cache, timeout=settings.llm_timeout)
    chain: list[Composer] = [_provider(name, settings) for name in provider_names]
    if fallback_names:
        chain.extend(_provider(name, settings, settings.fallback_model) for name in fallback_names)
    # Remove noop fallback entries when a real provider chain is present.
    chain = [item for item in chain if not isinstance(item, NoopProvider)] or [NoopProvider()]
    return LLMAdapter(chain, cache, timeout=settings.llm_timeout)
