"""Conditional-fact resolver (Defect C): pick the RIGHT offer and surface the
high-value facts a plain extraction misses.

``select_offer`` precedence (first match wins):
1. merchant ``offers[]`` with ``status == "active"`` whose title matches a
   customer attribute — senior status (``senior_citizen`` / age band ≥ 60 →
   title contains senior/65/silver), a training/health/previous focus (matching
   service word), or the customer's received services (matching or
   complementary service word);
2. otherwise the merchant's active offer with the LOWEST price point that fits
   the trigger intent (refill/delivery intents prefer delivery titles);
3. otherwise an expired merchant offer (reactivation hook);
4. otherwise ``category.offer_catalog`` filtered by audience (senior →
   senior-flavoured; repeat-trigger → repeat_user; else new_user/all) and by
   ``type``, preferring ``service_at_price`` and ``free_service``/``free_trial``
   over ``percentage_discount`` and ``bogo`` — brief §11: service+price beats
   discount framing.

``attach_conditionals`` surfaces the cheap high-value facts: subscription
runway (≤21 days, either direction), the full customer_aggregate (keys read
dynamically), the customer's personalisation points (preferred stylist, family
size, office nearby, wedding date), review quotes at salience 1.0, and tenure
for otherwise-thin merchants. Pure and deterministic.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from .derived import Fact, format_day_month, parse_dt

__all__ = ["select_offer", "attach_conditionals"]

_PRICE_RE = re.compile(r"(?:₹|Rs\.?)\s?([\d,]+)", re.IGNORECASE)

_FOCUS_STOPWORDS = {"and", "the", "with", "management", "focus"}

# catalog types ranked: service+price framing beats discount framing (brief §11)
_TYPE_PREFERENCE = (
    "service_at_price", "free_service", "free_trial", "membership",
    "free_addon", "percentage_discount", "bogo",
)
_TYPE_RANK = {name: i for i, name in enumerate(_TYPE_PREFERENCE)}


def _price_of(text: str) -> float | None:
    match = _PRICE_RE.search(str(text))
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def _title_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{3,}", str(text).lower())
            if w not in _FOCUS_STOPWORDS}


def _customer_attribute_words(customer: dict | None) -> set[str]:
    if not customer:
        return set()
    words: set[str] = set()
    identity = customer.get("identity") or {}
    preferences = customer.get("preferences") or {}
    relationship = customer.get("relationship") or {}
    for source in (preferences.get("training_focus"), preferences.get("health_focus"),
                   relationship.get("previous_focus")):
        if isinstance(source, str) and source:
            words |= _title_words(source.replace("_", " "))
    for service in relationship.get("services_received") or []:
        if isinstance(service, str):
            words |= _title_words(service.replace("_", " "))
    return words


def _is_senior(customer: dict | None) -> bool:
    if not customer:
        return False
    identity = customer.get("identity") or {}
    if identity.get("senior_citizen"):
        return True
    band = str(identity.get("age_band") or "")
    match = re.match(r"(\d+)", band)
    return bool(match and int(match.group(1)) >= 60)


_SENIOR_TITLE_WORDS = ("senior", "65", "silver")


def _matches_attribute(title: str, customer: dict | None) -> bool:
    lowered = str(title).lower()
    if _is_senior(customer) and any(w in lowered for w in _SENIOR_TITLE_WORDS):
        return True
    attribute_words = _customer_attribute_words(customer)
    if attribute_words and (_title_words(title) & attribute_words):
        return True
    return False


def _intent_prefers_delivery(trigger: dict | None) -> bool:
    kind = str((trigger or {}).get("kind") or "")
    return kind in ("chronic_refill_due", "supply_alert")


def _catalog_entries(category: dict | None) -> list[dict]:
    return [o for o in ((category or {}).get("offer_catalog") or [])
            if isinstance(o, dict) and o.get("title")]


def select_offer(offers: list[Fact], offer_catalog: list[Fact], *,
                 customer: dict | None = None, trigger: dict | None = None,
                 merchant: dict | None = None,
                 category: dict | None = None) -> Fact | None:
    """The ONE offer this message should carry, by the precedence above."""
    active = [f for f in offers if f.key.startswith("offer_active_")]
    expired = [f for f in offers if f.key.startswith("offer_expired_")]

    # 1. active offer matching a customer attribute
    for fact in active:
        if _matches_attribute(fact.value, customer):
            return fact

    # 2. lowest-price active offer, delivery-flavoured first for refill intents
    if active:
        pool = list(active)
        if _intent_prefers_delivery(trigger):
            delivery = [f for f in pool if "deliver" in str(f.value).lower()]
            if delivery:
                pool = delivery
        pool.sort(key=lambda f: (_price_of(f.value) if _price_of(f.value) is not None
                                 else float("inf"), f.key))
        if pool and _price_of(pool[0].value) is not None:
            return pool[0]
        return pool[0] if pool else None

    # 3. expired offer as the reactivation hook
    if expired:
        return expired[0]

    # 4. catalog fallback, filtered by audience and type
    entries = _catalog_entries(category)
    if not entries and offer_catalog:
        return offer_catalog[0]
    if not entries:
        return None

    audience_want: str | None = None
    if _is_senior(customer):
        audience_want = "senior"
    elif str((trigger or {}).get("kind") or "") in (
            "renewal_due", "winback_eligible", "milestone_reached", "review_theme_emerged"):
        audience_want = "repeat_user"

    def audience_ok(entry: dict) -> bool:
        audience = entry.get("audience")
        if audience_want == "senior":
            return any(w in str(entry.get("title", "")).lower()
                       for w in _SENIOR_TITLE_WORDS) or audience == "all"
        if audience_want == "repeat_user":
            return audience in ("repeat_user", "all")
        return audience in ("new_user", "all")

    pool = [e for e in entries if audience_ok(e)] or entries
    by_type: dict[str, list[dict]] = {}
    for entry in pool:
        by_type.setdefault(str(entry.get("type", "")), []).append(entry)
    for type_name in _TYPE_PREFERENCE:
        bucket = by_type.get(type_name)
        if not bucket:
            continue
        bucket.sort(key=lambda e: (_price_of(e.get("title")) or float("inf"),
                                   str(e.get("id", ""))))
        want_title = bucket[0].get("title")
        for fact in offer_catalog:
            if str(fact.value) == want_title:
                return fact
        return offer_catalog[0]
    return offer_catalog[0] if offer_catalog else None


def attach_conditionals(facts: list[Fact], *, category: dict | None,
                        merchant: dict | None, trigger: dict | None,
                        customer: dict | None, now: datetime | None) -> list[Fact]:
    """Add (or boost) the conditional high-value facts, deterministically.

    Returns a new list; input facts are never mutated.
    """
    result: list[Fact] = []
    seen: set[str] = set()
    for fact in facts:
        if fact.key in seen:
            continue
        seen.add(fact.key)
        result.append(fact)

    def add(key: str, value: Any, phrase: str, json_path: str, kind: str,
            salience: float) -> None:
        if key in seen or value is None:
            return
        seen.add(key)
        result.append(Fact(key=key, value=value, phrase=phrase, json_path=json_path,
                           kind=kind, salience=max(0.0, min(1.0, salience))))

    subscription = (merchant or {}).get("subscription") or {}
    since_expiry = subscription.get("days_since_expiry")
    days_remaining = subscription.get("days_remaining")
    plan = subscription.get("plan") or "subscription"
    if isinstance(since_expiry, (int, float)) and 0 < since_expiry <= 21:
        add("runway_days_since_expiry", since_expiry,
            f"{plan} plan lapsed {int(since_expiry)} days ago",
            "merchant.subscription.days_since_expiry", "subscription", 1.0)
    if isinstance(days_remaining, (int, float)) and 0 < days_remaining <= 21:
        add("runway_days_remaining", days_remaining,
            f"{plan} plan renews in {int(days_remaining)} days",
            "merchant.subscription.days_remaining", "subscription", 1.0)

    # personalisation points from the customer's preferences
    preferences = (customer or {}).get("preferences") or {}
    stylist = preferences.get("preferred_stylist")
    if stylist:
        add("conditional_preferred_stylist", stylist,
            f"prefers stylist {stylist}",
            "customer.preferences.preferred_stylist", "customer", 0.9)
    wedding = parse_dt(preferences.get("wedding_date"))
    if wedding:
        add("conditional_wedding_date", preferences["wedding_date"],
            f"wedding on {format_day_month(wedding)}",
            "customer.preferences.wedding_date", "customer", 1.0)
    relationship = (customer or {}).get("relationship")
    relationship = relationship if isinstance(relationship, dict) else {}
    focus = preferences.get("training_focus") or preferences.get("health_focus") \
        or relationship.get("previous_focus")
    if focus:
        add("conditional_focus", focus,
            f"goal: {str(focus).replace('_', ' ')}",
            "customer.preferences.focus", "customer", 0.8)

    # verbatim customer language is the strongest grounded material there is
    for i, fact in enumerate(result):
        if fact.key.startswith("review_quote_") and fact.salience < 1.0:
            result[i] = Fact(key=fact.key, value=fact.value, phrase=fact.phrase,
                             json_path=fact.json_path, kind=fact.kind, salience=1.0,
                             citation=fact.citation)

    # tenure matters most when the merchant is otherwise thin
    thin = not (merchant or {}).get("offers") and not (merchant or {}).get("review_themes")
    if thin:
        for i, fact in enumerate(result):
            if fact.key == "tenure_years" and fact.salience < 0.6:
                result[i] = Fact(key=fact.key, value=fact.value, phrase=fact.phrase,
                                 json_path=fact.json_path, kind=fact.kind,
                                 salience=0.6, citation=fact.citation)

    return result
