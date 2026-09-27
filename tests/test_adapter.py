from __future__ import annotations

import pytest

from vera.config import Settings
from vera.llm.adapter import LLMAdapter, ProviderUnavailable, build_adapter
from vera.llm.cache import ResponseCache
from vera.llm.providers import NoopProvider


class FakeProvider:
    model = "test"

    def __init__(self, name="fake", error=None):
        self.name, self.error, self.calls, self.timeouts = name, error, 0, []

    def complete(self, system, user, *, temperature=0, max_tokens=900, timeout=6, seed=0):
        self.calls += 1
        self.timeouts.append(timeout)
        if self.error:
            raise self.error
        return "done"


def test_keyless_and_env_provider_selection(tmp_path):
    keyless = build_adapter(Settings(provider="noop", api_key="", llm_timeout=2),
                            ResponseCache(tmp_path / "cache.sqlite"))
    assert keyless.name == "deterministic-fallback"
    configured = build_adapter(Settings(provider="openai_compat", api_key="secret",
                                        base_url="https://example.invalid/v1", llm_model="m", llm_timeout=2),
                               ResponseCache(tmp_path / "configured.sqlite"))
    assert configured.name == "openai_compat"
    assert configured.model_name == "m"
    local = build_adapter(Settings(provider="ollama", api_key="", llm_timeout=2),
                          ResponseCache(tmp_path / "ollama.sqlite"))
    assert local.name == "openai_compat"
    assert local.providers[0].endpoint == "http://localhost:11434/v1"
    assert local.model_name == "llama3.1"
    local_fallback = build_adapter(Settings(provider="anthropic", api_key="",
                                            fallback_provider="ollama"),
                                   ResponseCache(tmp_path / "ollama-fallback.sqlite"))
    assert local_fallback.providers[0].endpoint == "http://localhost:11434/v1"
    no_key_remote = build_adapter(Settings(provider="openai_compat", api_key=""),
                                  ResponseCache(tmp_path / "remote.sqlite"))
    assert no_key_remote.name == "deterministic-fallback"


def test_failover_chain_and_timeout_is_bounded(tmp_path):
    failing = FakeProvider("first", RuntimeError("down"))
    fallback = FakeProvider("second")
    adapter = LLMAdapter([failing, fallback], ResponseCache(tmp_path / "cache.sqlite"), timeout=2)
    assert adapter.complete("s", "u", timeout=10) == "done"
    assert failing.calls == 1 and fallback.calls == 1
    assert fallback.timeouts == [2]


def test_circuit_opens_after_three_failures(tmp_path):
    failing = FakeProvider("bad", RuntimeError("unavailable"))
    adapter = LLMAdapter([failing], ResponseCache(tmp_path / "cache.sqlite"), timeout=1)
    for _ in range(3):
        with pytest.raises(ProviderUnavailable):
            adapter.complete("system", f"request {_}")
    assert failing.calls == 3
    with pytest.raises(ProviderUnavailable):
        adapter.complete("system", "fourth")
    assert failing.calls == 3


def test_provider_key_is_never_in_failure_logs(tmp_path, caplog):
    failing = FakeProvider("openai_compat", RuntimeError("secret-key-should-not-leak"))
    adapter = LLMAdapter([failing], ResponseCache(tmp_path / "cache.sqlite"), timeout=.4)
    with pytest.raises(ProviderUnavailable):
        adapter.complete("system", "user")
    assert "secret-key-should-not-leak" not in caplog.text
    assert not isinstance(NoopProvider(), type(None))
