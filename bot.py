"""Official starter-compatible import path.

Two roles:
1. ``uvicorn bot:app`` keeps working (re-exports the FastAPI app from app.py).
2. ``compose(category, merchant, trigger, customer)`` is the static entry
   required by challenge-brief.md section 7.1. It drives the same production
   planner + composer the HTTP server uses, in-process, with a disposable
   store per call so every pair composes from a clean, deterministic slate.
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app import app  # noqa: F401  (uvicorn bot:app entrypoint)
from vera.composer import compose as _compose_action
from vera.config import Settings
from vera.llm.adapter import build_adapter
from vera.planner import plan
from vera.prompts.registry import PromptRegistry
from vera.store import ContextStore

# Fixed canonical clock: compose() must be deterministic given the same inputs,
# so the static entry never reads the wall clock. Matches the runner's E1 instant.
_CANONICAL_NOW = datetime(2026, 5, 1, 10, 0, 0, tzinfo=timezone.utc)


class _FreshConversations:
    """No-op conversation state: each static compose() starts from a clean slate."""

    last_sent_at: dict[str, str] = {}

    def is_suppressed(self, suppression_key: str) -> bool:
        return False

    def is_opted_out(self, merchant_id: str) -> bool:
        return False

    def is_closed(self, conversation_id: str) -> bool:
        return False

    def closed_ids_for_merchant(self, merchant_id: str) -> list[str]:
        return []

    def all_conversation_ids(self) -> set[str]:
        return set()


def _stamp() -> str:
    return _CANONICAL_NOW.isoformat().replace("+00:00", "Z")


def compose(category: dict, merchant: dict, trigger: dict,
            customer: dict | None) -> dict:
    """
    Inputs are the dicts loaded from the dataset JSON.
    Returns a dict with keys: body, cta, send_as, suppression_key, rationale.
    Deterministic given the same inputs; no LLM call, no network I/O.
    """
    merchant = merchant or {}
    trigger = dict(trigger or {})
    category = category or {}
    trigger_id = str(trigger.get("id") or trigger.get("trigger_id") or "")
    merchant_id = str(trigger.get("merchant_id") or merchant.get("merchant_id") or "")
    if not trigger_id or not merchant_id:
        raise ValueError("compose() needs a trigger id and a merchant_id")
    customer_id = (trigger.get("customer_id") or (customer or {}).get("customer_id")
                   or (customer or {}).get("id"))

    async def _run() -> dict[str, Any] | None:
        with tempfile.TemporaryDirectory(prefix="vera_compose_") as tmp:
            store = ContextStore(Path(tmp) / "journal.jsonl")
            slug = str(merchant.get("category_slug") or category.get("slug") or "")
            if slug and category:
                await store.put("category", slug, 1, category,
                                received_at=_stamp(), delivered_at="")
            await store.put("merchant", merchant_id, 1, merchant,
                            received_at=_stamp(), delivered_at="")
            if customer_id and customer:
                await store.put("customer", str(customer_id), 1, customer,
                                received_at=_stamp(), delivered_at="")
            await store.put("trigger", trigger_id, 1, trigger,
                            received_at=_stamp(), delivered_at="")
            settings = Settings.from_env()
            planned = plan(_CANONICAL_NOW, [trigger_id], store,
                           _FreshConversations(), settings)
            if not planned:
                return None
            action = await _compose_action(
                planned[0], _CANONICAL_NOW, build_adapter(settings), settings,
                prior_bodies=[], history=[], registry=PromptRegistry())
            return dict(action)

    action = asyncio.run(_run())
    if action is None:
        raise ValueError(f"pair is not plannable: trigger={trigger_id} merchant={merchant_id}")
    return {"body": action["body"], "cta": action["cta"], "send_as": action["send_as"],
            "suppression_key": action["suppression_key"], "rationale": action["rationale"]}
