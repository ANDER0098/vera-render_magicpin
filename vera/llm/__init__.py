"""Provider-agnostic HTTP LLM adapter; no provider SDKs are required."""

from .adapter import LLMAdapter, ProviderUnavailable, build_adapter

__all__ = ["LLMAdapter", "ProviderUnavailable", "build_adapter"]
