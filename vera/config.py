"""Environment-backed configuration with safe, keyless defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _integer(name: str, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(os.environ.get(name, default))))
    except (TypeError, ValueError):
        return default


def _number(name: str, default: float, low: float, high: float) -> float:
    try:
        return max(low, min(high, float(os.environ.get(name, default))))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    team_name: str = "Magicpin Vera Challenge"
    team_members: tuple[str, ...] = ()
    model: str = "deterministic-fallback"
    approach: str = (
        "deterministic derived-signal planner + provider-agnostic composer with "
        "grounding validation, repair, and keyless fallback"
    )
    contact_email: str = "not-provided@example.invalid"
    version: str = "0.1.0"
    submitted_at: str = "not submitted"
    state_dir: Path = Path("vera_state")
    provider: str = "noop"
    base_url: str = ""
    api_key: str = ""
    llm_model: str = ""
    fallback_provider: str = ""
    fallback_model: str = ""
    llm_timeout: float = 5.5
    compose_budget: float = 6.0
    action_limit: int = 5
    cooldown_seconds: int = 86400
    score_urgency: float = 1.0
    score_fact_density: float = 0.7
    score_deadline: float = 1.0
    score_peer_gap: float = 0.8
    score_category_fit: float = 0.6
    score_novelty: float = 0.5
    score_recency_penalty: float = 0.7

    @classmethod
    def from_env(cls) -> "Settings":
        root = Path(__file__).resolve().parents[1]
        state_dir = Path(os.environ.get("VERA_STATE_DIR", str(root / "vera_state")))
        provider = os.environ.get("VERA_LLM_PROVIDER", "noop").strip() or "noop"
        llm_model = os.environ.get("VERA_LLM_MODEL", "").strip()
        members = tuple(
            part.strip() for part in os.environ.get("VERA_TEAM_MEMBERS", "").split(",")
            if part.strip()
        )
        return cls(
            team_name=os.environ.get("VERA_TEAM_NAME", "Magicpin Vera Challenge"),
            team_members=members,
            model=llm_model if provider.lower() not in {"", "noop", "none"} and llm_model else "deterministic-fallback",
            approach=os.environ.get(
                "VERA_APPROACH",
                "deterministic derived-signal planner + per-kind provider-agnostic composer with "
                "grounding validation, repair, and keyless fallback",
            ),
            contact_email=os.environ.get("VERA_CONTACT_EMAIL", "not-provided@example.invalid"),
            version=os.environ.get("VERA_VERSION", "0.1.0"),
            submitted_at=os.environ.get("VERA_SUBMITTED_AT", "not submitted"),
            state_dir=state_dir,
            provider=provider,
            base_url=os.environ.get("VERA_LLM_BASE_URL", "").strip(),
            api_key=os.environ.get("VERA_LLM_API_KEY", ""),
            llm_model=llm_model,
            fallback_provider=os.environ.get("VERA_LLM_FALLBACK_PROVIDER", "").strip(),
            fallback_model=os.environ.get("VERA_LLM_FALLBACK_MODEL", "").strip(),
            llm_timeout=_number("VERA_LLM_TIMEOUT", 5.5, 0.05, 7.0),
            compose_budget=_number("VERA_COMPOSE_BUDGET", 6.0, 0.05, 7.0),
            action_limit=_integer("VERA_ACTION_LIMIT", 5, 1, 20),
            cooldown_seconds=_integer("VERA_COOLDOWN_SECONDS", 86400, 0, 30 * 86400),
            score_urgency=_number("VERA_SCORE_URGENCY", 1.0, 0, 100),
            score_fact_density=_number("VERA_SCORE_FACT_DENSITY", 0.7, 0, 100),
            score_deadline=_number("VERA_SCORE_DEADLINE", 1.0, 0, 100),
            score_peer_gap=_number("VERA_SCORE_PEER_GAP", 0.8, 0, 100),
            score_category_fit=_number("VERA_SCORE_CATEGORY_FIT", 0.6, 0, 100),
            score_novelty=_number("VERA_SCORE_NOVELTY", 0.5, 0, 100),
            score_recency_penalty=_number("VERA_SCORE_RECENCY_PENALTY", 0.7, 0, 100),
        )
