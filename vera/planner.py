"""Deterministic trigger eligibility, consent checks and top-K ranking."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .config import Settings
from .signals.derived import (Fact, consent_allows, derive_facts,
                              parse_dt, pick_primary, permitted_intent)
from .signals.diagnose import Diagnosis, diagnose
from .signals.relevance import attach_conditionals
from .voice import Voice, resolve_voice

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass
class PlannedSend:
    trigger: dict[str, Any]
    trigger_id: str
    merchant_id: str
    customer_id: str | None
    category: dict[str, Any]
    merchant: dict[str, Any]
    customer: dict[str, Any] | None
    facts: list[Fact]
    voice: Voice
    primary_fact: Fact | None
    diagnosis: Diagnosis | None
    cta_shape: str
    send_as: str
    lever_set: tuple[str, ...]
    lang: str
    conversation_id: str
    suppression_key: str
    score: float


def plan(now: datetime, available_triggers: list[str], store: Any,
         conversation_state: Any, settings: Settings | None = None) -> list[PlannedSend]:
    settings = settings or Settings.from_env()
    candidates: list[tuple[float, str, PlannedSend]] = []
    seen_ids: set[str] = set()
    for trigger_id in available_triggers:
        trigger_id = str(trigger_id)
        if trigger_id in seen_ids:
            continue
        seen_ids.add(trigger_id)
        trigger_record = store.get("trigger", trigger_id)
        if trigger_record is None:
            continue
        trigger = trigger_record.payload
        merchant_id = str(trigger.get("merchant_id") or "")
        if not merchant_id:
            continue
        merchant_record = store.get("merchant", merchant_id)
        merchant = merchant_record.payload if merchant_record else {}
        customer_id = trigger.get("customer_id")
        customer_id = str(customer_id) if customer_id else None
        scope = str(trigger.get("scope") or "merchant")
        customer_record = store.get("customer", customer_id) if customer_id else None
        customer = customer_record.payload if customer_record else None
        category_slug = str(merchant.get("category_slug") or trigger.get("category_slug") or
                            (trigger.get("payload") or {}).get("category") or "")
        category_record = store.get("category", category_slug) if category_slug else None
        category = category_record.payload if category_record else {"slug": category_slug}
        suppression_key = str(trigger.get("suppression_key") or trigger_id)
        target_conversation = str(trigger.get("conversation_id") or "")

        if conversation_state.is_suppressed(suppression_key) or conversation_state.is_opted_out(merchant_id):
            continue
        if target_conversation and conversation_state.is_closed(target_conversation):
            continue
        if any(conversation_state.is_closed(cid)
               for cid in conversation_state.closed_ids_for_merchant(merchant_id)):
            continue
        if _in_cooldown(merchant_id, now, conversation_state.last_sent_at, settings.cooldown_seconds):
            continue
        if scope == "customer" and customer is not None:
            intent = _trigger_intent(str(trigger.get("kind") or ""))
            consent = ((customer.get("consent") or {}).get("scope") or [])
            if not consent_allows(consent, intent):
                continue

        facts = derive_facts(category, merchant, trigger, customer, now)
        facts = attach_conditionals(facts, category=category, merchant=merchant,
                                    trigger=trigger, customer=customer, now=now)
        primary = pick_primary(facts, trigger, merchant=merchant, category=category, now=now)
        diagnosis = diagnose(facts, merchant=merchant, category=category,
                             trigger=trigger, now=now)
        voice = resolve_voice(category, merchant, customer, trigger,
                              merchant.get("conversation_history") or [])
        cta = _cta_shape(trigger)
        send_as = "merchant_on_behalf" if scope == "customer" or customer is not None else "vera"
        score = _score(facts, trigger, category, merchant, customer, now, conversation_state, settings)
        conv_id = _conversation_id(merchant_id, str(trigger.get("kind") or "generic"), now,
                                   trigger_id, conversation_state)
        planned = PlannedSend(trigger, trigger_id, merchant_id, customer_id, category,
                              merchant, customer, facts, voice, primary, diagnosis, cta,
                              send_as, tuple(dict.fromkeys(f.kind for f in facts if f.salience >= .6)),
                              voice.lang, conv_id, suppression_key, score)
        candidates.append((score, trigger_id, planned))

    # Rank descending; alphabetical trigger ID breaks every exact tie.
    candidates.sort(key=lambda item: (-item[0], item[1]))
    chosen: list[PlannedSend] = []
    used_merchants: set[str] = set()
    used_conversations: set[tuple[str, str]] = set()
    portfolio_kinds: set[str] = set()
    limit = min(20, max(1, settings.action_limit))
    remaining = list(candidates)
    while remaining and len(chosen) < limit:
        remaining.sort(key=lambda entry: (
            -(entry[0] + (settings.score_novelty
                          if str(entry[2].trigger.get("kind") or "") not in portfolio_kinds else 0.0)),
            entry[1]))
        selected = next((index for index, entry in enumerate(remaining)
                         if entry[2].merchant_id not in used_merchants and
                         (entry[2].merchant_id, entry[2].conversation_id) not in used_conversations), None)
        if selected is None:
            break
        _score_value, _trigger_id, item = remaining.pop(selected)
        pair = (item.merchant_id, item.conversation_id)
        used_merchants.add(item.merchant_id)
        used_conversations.add(pair)
        portfolio_kinds.add(str(item.trigger.get("kind") or ""))
        chosen.append(item)
    return chosen


def _score(facts: list[Fact], trigger: dict, category: dict, merchant: dict,
           customer: dict | None, now: datetime, state: Any,
           settings: Settings) -> float:
    urgency = _number(trigger.get("urgency"), 1.0)
    fact_density = sum(1 for fact in facts if fact.salience >= .6)
    deadline = 0.0
    for key in ("deadline_iso", "due_date", "appointment_at", "wedding_date", "date"):
        raw = (trigger.get("payload") or {}).get(key)
        dt = parse_dt(raw)
        if dt:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            delta = (dt - now).total_seconds() / 86400
            deadline = max(deadline, max(0.0, 21.0 - min(21.0, abs(delta))) / 21.0)
    gaps = [abs(float(f.value)) for f in facts
            if f.key.startswith("gap_") and isinstance(f.value, (int, float))]
    peer_gap = min(100.0, max(gaps, default=0.0)) / 20.0
    category_fit = 1.0 if any(f.kind in {"digest", "seasonal", "trend", "offer"} for f in facts) else .25
    history = merchant.get("conversation_history") or []
    recency = min(3.0, len(history) / 4.0) if isinstance(history, list) else 0.0
    return (settings.score_urgency * urgency + settings.score_fact_density * fact_density +
            settings.score_deadline * deadline + settings.score_peer_gap * peer_gap +
            settings.score_category_fit * category_fit -
            settings.score_recency_penalty * recency)


def _trigger_intent(kind: str) -> str:
    value = kind.lower()
    if "refill" in value or "dispatch" in value:
        return "refill"
    if "appointment" in value or "trial" in value or "bridal" in value or "wedding" in value:
        return "appointment"
    # Lapse/winback outreach is marketing, not a clinical recall reminder.
    # Customers who opted into winback_offers or promotional_offers may receive
    # it; recall-only consent must not be widened into a promotion.
    if "lapsed" in value or "winback" in value:
        return "promotional"
    if "recall" in value:
        return "recall"
    return "promotional"


def _cta_shape(trigger: dict) -> str:
    kind = str(trigger.get("kind") or "")
    payload = trigger.get("payload") or {}
    if kind in {"recall_due", "appointment_tomorrow", "trial_followup"}:
        slots = payload.get("available_slots") or payload.get("next_session_options") or payload.get("slots") or []
        labels = [slot.get("label") if isinstance(slot, dict) else slot for slot in slots]
        if sum(bool(label) for label in labels) >= 2:
            return "multi_choice_slot"
        return "binary_yes_no"
    if any(part in kind for part in ("refill", "dispatch", "commitment")):
        return "binary_confirm_cancel"
    if any(part in kind for part in ("research_digest", "curious_ask_due", "cde_opportunity")):
        return "open_ended"
    if kind in {"research_digest", "category_research_digest_release"}:
        return "open_ended"
    if kind in {"performance_report", "pure_information"}:
        return "none"
    return "binary_yes_no"


def _conversation_id(merchant_id: str, kind: str, now: datetime,
                     trigger_id: str, state: Any) -> str:
    merchant_short = re.sub(r"[^a-zA-Z0-9]+", "_", merchant_id).strip("_")[:16] or "merchant"
    kind_short = re.sub(r"[^a-zA-Z0-9]+", "_", kind).strip("_")[:32] or "generic"
    iso_year, iso_week, _ = now.isocalendar()
    base = f"conv_{merchant_short}_{kind_short}_{iso_year}-W{iso_week:02d}"
    existing = state.all_conversation_ids()
    if base not in existing:
        return base
    suffix = re.sub(r"[^a-zA-Z0-9]+", "_", trigger_id).strip("_")[-16:]
    candidate = f"{base}_{suffix}"
    index = 2
    while candidate in existing:
        candidate = f"{base}_{suffix}_{index}"
        index += 1
    return candidate


def _in_cooldown(merchant_id: str, now: datetime, sent: dict[str, str], seconds: int) -> bool:
    value = sent.get(merchant_id)
    if not value or seconds <= 0:
        return False
    then = parse_dt(value)
    if not then:
        return False
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return 0 <= (now - then).total_seconds() < seconds


def _number(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
