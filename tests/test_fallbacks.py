"""Tests for vera.fallbacks — all 26 kinds + generic, both languages, rich/thin."""

import re
from dataclasses import replace

import pytest

from vera.fallbacks.compose import (
    SUPPORTED_KINDS,
    compose_fallback,
    is_supported_kind,
)
from vera.grounding.validator import grounding_map, has_hard, validate
from vera.signals.derived import derive_facts
from vera.voice import resolve_voice
from conftest import load

EXPECTED_KINDS = {
    "active_planning_intent", "appointment_tomorrow", "category_seasonal",
    "cde_opportunity", "chronic_refill_due", "competitor_opened", "curious_ask_due",
    "customer_lapsed_hard", "customer_lapsed_soft", "dormant_with_vera",
    "festival_upcoming", "gbp_unverified", "ipl_match_today", "milestone_reached",
    "perf_dip", "perf_spike", "recall_due", "regulation_change", "renewal_due",
    "research_digest", "review_theme_emerged", "seasonal_perf_dip", "supply_alert",
    "trial_followup", "wedding_package_followup", "winback_eligible",
}

DEOGARHI = re.compile(r"[\u0900-\u097F]")
URL = re.compile(r"(?:https?://|www\.)", re.IGNORECASE)
CTA_VALUES = {"binary_yes_no", "binary_confirm_cancel", "open_ended",
              "multi_choice_slot", "none"}


CUSTOMER_KINDS = {"recall_due", "appointment_tomorrow", "chronic_refill_due",
                  "trial_followup", "customer_lapsed_hard", "customer_lapsed_soft",
                  "wedding_package_followup"}


def _trigger_for(kind, merchant, customer=None):
    """Real seed payload when one exists for (kind, merchant) and matches the
    scope implied by *customer*; else a thin placeholder trigger."""
    implied_scope = "customer" if customer is not None else "merchant"
    for trg in load("dataset/triggers_seed.json")["triggers"]:
        if trg["kind"] == kind and trg["merchant_id"] == merchant["merchant_id"] \
                and trg["scope"] == implied_scope:
            return trg
    return {
        "id": f"test_{kind}_{merchant['merchant_id']}",
        "scope": implied_scope,
        "kind": kind, "source": "internal",
        "merchant_id": merchant["merchant_id"],
        "customer_id": (customer or {}).get("customer_id"),
        "payload": {"placeholder": True, "metric_or_topic": kind},
        "urgency": 3, "suppression_key": f"test:{kind}",
        "expires_at": "2099-01-01T00:00:00Z",
    }


def _compose(categories, merchant, kind, customer=None, lang=None, now=None):
    category = categories[merchant["category_slug"]]
    trigger = _trigger_for(kind, merchant, customer)
    facts = derive_facts(category, merchant, trigger, customer, now)
    voice = resolve_voice(category, merchant, customer, trigger)
    if lang is not None:
        voice = replace(voice, lang=lang)
    message = compose_fallback(kind, category=category, merchant=merchant,
                               trigger=trigger, customer=customer, facts=facts,
                               voice=voice, now=now)
    violations = validate(message.body, message.cta, message.send_as,
                          facts=facts, voice=voice, category=category,
                          merchant=merchant, customer=customer,
                          scope=trigger.get("scope"))
    return message, violations, facts, voice, trigger


def test_supported_kinds_exactly_26():
    assert len(SUPPORTED_KINDS) == 26
    assert SUPPORTED_KINDS == EXPECTED_KINDS
    assert is_supported_kind("recall_due") is True
    assert is_supported_kind("kind_that_does_not_exist") is False


@pytest.mark.parametrize("kind", sorted(EXPECTED_KINDS))
@pytest.mark.parametrize("merchant_path", [
    "expanded/merchants/m_001_drmeera_dentist_delhi.json",
    "expanded/merchants/m_023_sushma_salon_pune.json",
])
@pytest.mark.parametrize("lang", ["en", "hi_en"])
def test_all_kinds_render_clean(categories, rich_dentist, thin_salon, now,
                                kind, merchant_path, lang):
    merchant = rich_dentist if "m_001" in merchant_path else thin_salon
    if kind in ("chronic_refill_due",) and merchant["category_slug"] != "pharmacies":
        pass  # kind-specific data may be absent; frame must still render
    message, violations, facts, voice, trigger = _compose(
        categories, merchant, kind, customer=None, lang=lang, now=now)

    assert message.body.strip(), f"{kind}/{lang}: empty body"
    assert message.cta in CTA_VALUES
    assert message.send_as == "vera"
    assert message.template_name == f"vera_{kind}_v1"
    assert 3 <= len(message.template_params) <= 6
    assert all(isinstance(p, str) and p.strip() for p in message.template_params)
    assert not has_hard(violations), f"{kind}/{lang}: {violations}\n{message.body}"
    assert not DEOGARHI.search(message.body)
    assert not URL.search(message.body)
    assert "₹" not in message.body  # Rs. only in output
    assert "No field invented" in message.rationale


@pytest.mark.parametrize("kind", sorted(CUSTOMER_KINDS))
def test_customer_facing_kinds_render_clean(categories, rich_dentist, thin_salon,
                                            priya, sharma, now, kind):
    for merchant, customer in ((rich_dentist, priya), (thin_salon, sharma)):
        message, violations, facts, voice, trigger = _compose(
            categories, merchant, kind, customer=customer, now=now)
        assert trigger["scope"] == "customer"
        assert message.body.strip(), f"{kind}: empty body for {merchant['merchant_id']}"
        assert message.send_as == "merchant_on_behalf"
        assert message.template_name == f"merchant_{kind}_v1"
        assert message.cta in CTA_VALUES
        assert not has_hard(violations), f"{kind}: {violations}\n{message.body}"
        assert not DEOGARHI.search(message.body)
        assert not URL.search(message.body)


def test_unknown_kind_never_crashes_and_returns_valid_message(categories, thin_salon, now):
    message, violations, facts, voice, trigger = _compose(
        categories, thin_salon, "kind_that_does_not_exist", now=now)
    assert message.body.strip()
    assert not has_hard(violations)
    assert message.cta in CTA_VALUES
    assert message.template_name == "vera_kind_that_does_not_exist_v1"


def test_rationale_maps_every_tier1_numeral_to_a_json_path(categories, rich_dentist, now):
    message, violations, facts, voice, trigger = _compose(
        categories, rich_dentist, "research_digest", now=now)
    lines = grounding_map(message.body, facts, voice)
    assert not any(line.startswith("unmatched") for line in lines)
    for line in lines:
        if line.startswith("numeral"):
            assert "<-" in line


def test_duplicate_triggers_get_distinct_bodies(categories, rich_dentist, now):
    for kind in ("perf_dip", "festival_upcoming", "review_theme_emerged"):
        trig_a = {"id": f"t_a_{kind}", "scope": "merchant", "kind": kind,
                  "payload": {"placeholder": True}}
        trig_b = {"id": f"t_b_{kind}", "scope": "merchant", "kind": kind,
                  "payload": {"placeholder": True}}
        facts = derive_facts(categories["dentists"], rich_dentist, trig_a, None, now)
        voice = resolve_voice(categories["dentists"], rich_dentist, None, trig_a)
        msg_a = compose_fallback(kind, category=categories["dentists"],
                                 merchant=rich_dentist, trigger=trig_a,
                                 customer=None, facts=facts, voice=voice, now=now)
        # second trigger for the same merchant sees the first body as prior history
        msg_b = compose_fallback(kind, category=categories["dentists"],
                                 merchant=rich_dentist, trigger=trig_b,
                                 customer=None, facts=facts, voice=voice, now=now,
                                 prior_bodies=[msg_a.body])
        assert msg_a.body != msg_b.body, f"{kind}: identical bodies for distinct triggers"


def test_slot_frames_use_labels_verbatim(categories, rich_dentist, priya, now):
    message, violations, facts, voice, trigger = _compose(
        categories, rich_dentist, "recall_due", customer=priya, now=now)
    assert message.cta == "multi_choice_slot"
    assert "Wed 5 Nov, 6pm" in message.body
    assert "Thu 6 Nov, 5pm" in message.body


def test_ipl_contrarian_call_on_saturday(categories, now):
    merchant = load("expanded/merchants/m_005_pizzajunction_restaurant_delhi.json")
    message, violations, facts, voice, trigger = _compose(
        categories, merchant, "ipl_match_today", now=now)
    assert "skip" in message.body.lower() or "iski jagah" in message.body.lower()
    assert "delivery" in message.body.lower()


def test_seasonal_dip_reframes_not_alarms(categories, now):
    merchant = load("expanded/merchants/m_007_powerhouse_gym_bangalore.json")
    message, violations, facts, voice, trigger = _compose(
        categories, merchant, "seasonal_perf_dip", now=now)
    lowered = message.body.lower()
    assert "normal" in lowered or "asli dip nahi" in lowered


def test_senior_customer_gets_namaste_ji(categories, now):
    merchant = load("expanded/merchants/m_009_apollo_pharmacy_jaipur.json")
    customer = load("expanded/customers/c_013_grandfather_for_m009.json")
    message, violations, facts, voice, trigger = _compose(
        categories, merchant, "chronic_refill_due", customer=customer, now=now)
    assert "Namaste" in message.body and "ji" in message.body
    for molecule in ("metformin", "atorvastatin", "telmisartan"):
        assert molecule in message.body
    assert "28 April" in message.body
    assert message.cta == "binary_confirm_cancel"


def test_parent_addressing_uses_parent_name(categories, now):
    merchant = load("expanded/merchants/m_008_zenyoga_gym_chennai.json")
    customer = load("expanded/customers/c_012_karthik_jr_for_m008.json")
    message, violations, facts, voice, trigger = _compose(
        categories, merchant, "trial_followup", customer=customer, now=now)
    assert "Sumitra" in message.body
    assert "Karthik" in message.body


def test_compose_is_deterministic(categories, rich_dentist, now):
    first = _compose(categories, rich_dentist, "research_digest", now=now)[0]
    second = _compose(categories, rich_dentist, "research_digest", now=now)[0]
    assert first.body == second.body
    assert first.rationale == second.rationale
    assert first.template_params == second.template_params
