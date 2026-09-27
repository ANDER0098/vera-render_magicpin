"""LLM Provider abstraction for the local judge harness.

Supports OpenRouter, OpenAI, Anthropic, Gemini, Groq, DeepSeek, and
OpenAI-compatible endpoints via httpx.
Env-driven configuration with strict error handling on missing credentials.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

import httpx

logger = logging.getLogger("harness.providers")

DEFAULT_TEMPERATURE = 0.2
DEFAULT_MAX_TOKENS = 1500
DEFAULT_TIMEOUT = 45.0


class RateLimitError(Exception):
    """Raised when an HTTP 429 rate limit is encountered."""
    def __init__(self, message: str, retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after


class LLMProvider(ABC):
    """Abstract base class for all judge LLM providers."""

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.api_key = api_key
        self.model = model
        self.base_url = (base_url or self.default_base_url).rstrip("/")
        self.timeout = timeout
        self.call_count = 0

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Name of the provider service."""
        pass

    @property
    @abstractmethod
    def default_base_url(self) -> str:
        """Default base URL for this provider."""
        pass

    def name(self) -> str:
        return f"{self.provider_name}:{self.model}"

    @abstractmethod
    def _execute(self, prompt: str, system: Optional[str] = None) -> str:
        """Execute request to provider."""
        pass

    def complete(self, prompt: str, system: Optional[str] = None) -> str:
        """Complete prompt with rate limit retry & backoff."""
        self.call_count += 1
        max_retries = 5
        base_delay = 1.0

        for attempt in range(max_retries):
            try:
                return self._execute(prompt, system)
            except RateLimitError as e:
                if attempt == max_retries - 1:
                    raise
                delay = e.retry_after if (e.retry_after and e.retry_after > 0) else (base_delay * (2 ** attempt))
                logger.warning(
                    f"[{self.name()}] Rate limited (429). Retrying in {delay:.1f}s (attempt {attempt + 1}/{max_retries})"
                )
                time.sleep(delay)
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429:
                    if attempt == max_retries - 1:
                        raise RateLimitError(f"Rate limited after {max_retries} attempts: {e}") from e
                    retry_after = _parse_retry_after(e.response.headers)
                    delay = retry_after if (retry_after and retry_after > 0) else (base_delay * (2 ** attempt))
                    logger.warning(
                        f"[{self.name()}] HTTP 429 received. Backing off for {delay:.1f}s..."
                    )
                    time.sleep(delay)
                elif e.response.status_code in (500, 502, 503, 504):
                    if attempt == max_retries - 1:
                        raise
                    retry_after = _parse_retry_after(e.response.headers)
                    delay = retry_after if (retry_after and retry_after > 0) \
                        else min(base_delay * (2 ** attempt), 16.0)
                    logger.warning(
                        f"[{self.name()}] HTTP {e.response.status_code} received. "
                        f"Retrying in {delay:.1f}s (attempt {attempt + 1}/{max_retries})"
                    )
                    time.sleep(delay)
                else:
                    raise
        raise RuntimeError(f"Exceeded max retries for provider {self.name()}")


def _parse_retry_after(headers: httpx.Headers) -> Optional[float]:
    """Parse retry-after or ratelimit reset headers."""
    for h in ("retry-after", "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens", "x-ratelimit-reset"):
        val = headers.get(h)
        if val:
            try:
                return max(0.5, float(val))
            except ValueError:
                pass
    return None


class OpenAIProvider(LLMProvider):
    provider_name = "openai"
    default_base_url = "https://api.openai.com/v1"

    def _execute(self, prompt: str, system: Optional[str] = None) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": DEFAULT_TEMPERATURE,
            "max_tokens": DEFAULT_MAX_TOKENS,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(f"{self.base_url}/chat/completions", json=payload, headers=headers)
            if resp.status_code == 429:
                raise RateLimitError("OpenAI 429", _parse_retry_after(resp.headers))
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]


class OpenRouterProvider(LLMProvider):
    provider_name = "openrouter"
    default_base_url = "https://openrouter.ai/api/v1"

    def _execute(self, prompt: str, system: Optional[str] = None) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": DEFAULT_TEMPERATURE,
            "max_tokens": DEFAULT_MAX_TOKENS,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://magicpin.com",
            "X-Title": "magicpin-ai-challenge",
        }

        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(f"{self.base_url}/chat/completions", json=payload, headers=headers)
            if resp.status_code == 429:
                raise RateLimitError("OpenRouter 429", _parse_retry_after(resp.headers))
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]


class AnthropicProvider(LLMProvider):
    provider_name = "anthropic"
    default_base_url = "https://api.anthropic.com/v1"

    def _execute(self, prompt: str, system: Optional[str] = None) -> str:
        payload: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": DEFAULT_MAX_TOKENS,
            "temperature": DEFAULT_TEMPERATURE,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            payload["system"] = system

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(f"{self.base_url}/messages", json=payload, headers=headers)
            if resp.status_code == 429:
                raise RateLimitError("Anthropic 429", _parse_retry_after(resp.headers))
            resp.raise_for_status()
            data = resp.json()
            return data["content"][0]["text"]


class GeminiProvider(LLMProvider):
    provider_name = "gemini"
    default_base_url = "https://generativelanguage.googleapis.com/v1beta"

    def _execute(self, prompt: str, system: Optional[str] = None) -> str:
        payload: Dict[str, Any] = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": DEFAULT_TEMPERATURE,
                "maxOutputTokens": DEFAULT_MAX_TOKENS,
            },
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}

        # Keep credentials out of the request URL: httpx logs request URLs at
        # INFO level, which would otherwise expose the API key in console logs.
        # Google documents x-goog-api-key as the REST authentication header.
        url = f"{self.base_url}/models/{self.model}:generateContent"
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self.api_key,
        }

        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(url, json=payload, headers=headers)
            if resp.status_code == 429:
                raise RateLimitError("Gemini 429", _parse_retry_after(resp.headers))
            resp.raise_for_status()
            data = resp.json()
            return data["candidates"][0]["content"]["parts"][0]["text"]


class GroqProvider(LLMProvider):
    provider_name = "groq"
    default_base_url = "https://api.groq.com/openai/v1"

    def _execute(self, prompt: str, system: Optional[str] = None) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": DEFAULT_TEMPERATURE,
            "max_tokens": DEFAULT_MAX_TOKENS,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(f"{self.base_url}/chat/completions", json=payload, headers=headers)
            if resp.status_code == 429:
                raise RateLimitError("Groq 429", _parse_retry_after(resp.headers))
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]


class DeepSeekProvider(LLMProvider):
    provider_name = "deepseek"
    default_base_url = "https://api.deepseek.com/v1"

    def _execute(self, prompt: str, system: Optional[str] = None) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": DEFAULT_TEMPERATURE,
            "max_tokens": DEFAULT_MAX_TOKENS,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(f"{self.base_url}/chat/completions", json=payload, headers=headers)
            if resp.status_code == 429:
                raise RateLimitError("DeepSeek 429", _parse_retry_after(resp.headers))
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]


class FakeProvider(LLMProvider):
    """Mock provider for unit tests without network or keys."""
    provider_name = "fake"
    default_base_url = "http://fake"

    def __init__(self, response_text: str = "", responses: Optional[List[str]] = None, raise_exception: Optional[Exception] = None):
        super().__init__(api_key="fake-key", model="fake-model")
        self.response_text = response_text
        self.responses = list(responses) if responses else []
        self.raise_exception = raise_exception

    def _execute(self, prompt: str, system: Optional[str] = None) -> str:
        if self.raise_exception:
            raise self.raise_exception
        if self.responses:
            return self.responses.pop(0)
        return self.response_text


PROVIDER_CLASSES = {
    "openrouter": OpenRouterProvider,
    "openai": OpenAIProvider,
    "anthropic": AnthropicProvider,
    "gemini": GeminiProvider,
    "groq": GroqProvider,
    "deepseek": DeepSeekProvider,
    "fake": FakeProvider,
}

ENV_KEY_MAP = {
    "openrouter": ["OPENROUTER_API_KEY", "JUDGE_API_KEY"],
    "openai": ["OPENAI_API_KEY", "JUDGE_API_KEY"],
    "anthropic": ["ANTHROPIC_API_KEY", "JUDGE_API_KEY"],
    "gemini": ["GEMINI_API_KEY", "JUDGE_API_KEY"],
    "groq": ["GROQ_API_KEY", "JUDGE_API_KEY"],
    "deepseek": ["DEEPSEEK_API_KEY", "JUDGE_API_KEY"],
}

DEFAULT_MODELS = {
    "openrouter": "anthropic/claude-sonnet-4.6",
    "openai": "gpt-4o-mini",
    "anthropic": "claude-3-5-sonnet-20241022",
    "gemini": "gemini-3.8-flash",
    "groq": "llama-3.3-70b-versatile",
    "deepseek": "deepseek-chat",
}


def resolve_api_key(provider_name: str) -> str:
    """Find API key for provider or raise descriptive error."""
    provider_norm = provider_name.lower()
    keys_to_check = ENV_KEY_MAP.get(provider_norm, ["JUDGE_API_KEY"])
    for k in keys_to_check:
        val = os.environ.get(k)
        if val and val.strip():
            return val.strip()

    missing_names = " or ".join(keys_to_check)
    raise ValueError(
        f"Missing API key for provider '{provider_name}'. Please set {missing_names} in the environment."
    )


def get_provider(spec: Optional[str] = None, base_url_override: Optional[str] = None) -> LLMProvider:
    """Create provider from string spec or environment variables.

    Spec formats:
      - 'gemini:gemini-3.8-flash'
      - 'openrouter:anthropic/claude-sonnet-4.6'
      - 'openai'
    """
    base_url = base_url_override or os.environ.get("JUDGE_BASE_URL")

    if not spec:
        provider_name = os.environ.get("JUDGE_PROVIDER", "gemini").lower()
        model_name = os.environ.get("JUDGE_MODEL", DEFAULT_MODELS.get(provider_name, "gemini-3.8-flash"))
    elif ":" in spec:
        provider_name, model_name = spec.split(":", 1)
        provider_name = provider_name.lower()
    else:
        provider_name = spec.lower()
        model_name = os.environ.get("JUDGE_MODEL", DEFAULT_MODELS.get(provider_name, "gemini-3.8-flash"))

    if provider_name not in PROVIDER_CLASSES:
        if base_url:
            # Fallback to OpenAI-compatible provider
            api_key = os.environ.get("JUDGE_API_KEY", "placeholder")
            return OpenAIProvider(api_key=api_key, model=model_name, base_url=base_url)
        raise ValueError(
            f"Unknown provider '{provider_name}'. Supported: {', '.join(PROVIDER_CLASSES.keys())}"
        )

    if provider_name == "fake":
        return FakeProvider()

    api_key = resolve_api_key(provider_name)
    cls = PROVIDER_CLASSES[provider_name]
    return cls(api_key=api_key, model=model_name, base_url=base_url)


def get_providers_from_env() -> List[LLMProvider]:
    """Parse JUDGE_MODEL_LIST or single JUDGE_PROVIDER into a list of providers."""
    model_list_env = os.environ.get("JUDGE_MODEL_LIST")
    if model_list_env:
        specs = [s.strip() for s in model_list_env.split(",") if s.strip()]
        return [get_provider(s) for s in specs]
    return [get_provider()]
