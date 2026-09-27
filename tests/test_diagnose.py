"""Tests for the diagnosis funnel (Defect B) — one test per rule, real merchants."""

import json

import pytest

from vera.signals.diagnose import diagnose
from vera.signals.relevance import attach_conditionals
from vera.signals.derived import derive_facts, pick_primary
from conftest import load

NOW_ARGS = {}


def _diagnose_for(categories, merchant_path, trigger, now, customer_path=None):
    merchant = load(merchant_path)
    category = categories[merchant["category_slug"]]
    customer = load(customer_path) if customer_path else None
    facts = attach_conditionals(
        derive_facts(category, merchant, trigger, customer, now),
        category=category, merchant=merchant, trigger=trigger,
        customer=customer, now=now)
    return diagnose(facts, merchant=merchant, category=category,
                    trigger=trigger, now=now), merchant, category, customer, facts


def _seed(trigger_id):
    return next(t for t in load("dataset/triggers_seed.json")["triggers"]
                if t["id"] == trigger_id)


def test_funnel_subscription_cause_names_expiry_and_reconciles(categories, now):
    diagnosis, merchant, _, _, _ = _diagnose_for(
        categories, "expanded/merchants/m_023_sushma_salon_pune.json",
        {"id": "t", "kind": "perf_dip", "scope": "merchant", "payload": {"placeholder": True}},
        now)
    assert diagnosis.rule_id == "subscription_lapse_with_dip"
    # D2: the FUNNEL leads and reconciles in one sentence with BOTH benchmark
    # values; the expired subscription is a separate aside, never the cause
    assert "2,547" in diagnosis.story and "22" in diagnosis.story
    assert "2,400" in diagnosis.story and "4.0%" in diagnosis.story and "28" in diagnosis.story
    assert diagnosis.primary.key == "gap_calls"
    assert any(f.key == "subscription_days_since_expiry" for f in diagnosis.supporting)
    # no causal connective: a lapse may not be cited as damaging a metric
    # that is above its benchmark
    for connective in ("because", "isliye", "effect dikh raha", "due to", "ke karan"):
        assert connective not in diagnosis.story.lower()
        assert connective not in diagnosis.story_hi.lower()
    assert "39" in diagnosis.story and "lapse" in diagnosis.story  # the aside


def test_funnel_leakage_rule(categories, now):
    # m_023 with an ACTIVE subscription falls to the plain leakage read
    diagnosis, _, _, _, _ = _diagnose_for(
        categories, "expanded/merchants/m_023_sushma_salon_pune.json",
        {"id": "t", "kind": "perf_dip", "scope": "merchant", "payload": {"placeholder": True}},
        now)
    merchant = load("expanded/merchants/m_023_sushma_salon_pune.json")
    merchant["subscription"] = {"status": "active", "plan": "Pro", "days_remaining": 90}
    from vera.signals.derived import derive_facts
    facts = derive_facts(categories["salons"], merchant,
                         {"id": "t", "kind": "perf_dip", "scope": "merchant",
                          "payload": {"placeholder": True}}, None, now)
    diagnosis = diagnose(facts, merchant=merchant, category=categories["salons"],
                         trigger={"id": "t", "kind": "perf_dip", "scope": "merchant",
                                  "payload": {"placeholder": True}}, now=now)
    assert diagnosis.rule == "funnel_leakage"
    assert "22 calls" in diagnosis.story
    assert diagnosis.primary.key == "gap_calls"


def test_seasonal_reframe_rule(categories, now):
    diagnosis, _, _, _, _ = _diagnose_for(
        categories, "expanded/merchants/m_007_powerhouse_gym_bangalore.json",
        _seed("trg_014_seasonal_acquisition_dip_powerhouse"), now)
    assert diagnosis.rule == "seasonal"
    assert "normal" in diagnosis.story or "lull" in diagnosis.story


def test_review_theme_rule_quotes_verbatim(categories, now):
    diagnosis, _, _, _, _ = _diagnose_for(
        categories, "expanded/merchants/m_001_drmeera_dentist_delhi.json",
        _seed("trg_011_review_theme_late_delivery") if False else
        {"id": "t", "kind": "review_theme_emerged", "scope": "merchant",
         "payload": {"placeholder": True}},
        now)
    assert diagnosis.rule == "review_theme"
    assert '"had to wait 30 min on Sunday afternoon"' in diagnosis.story
    assert "3 reviews" in diagnosis.story


def test_milestone_rule_frames_the_gap(categories, now):
    diagnosis, _, _, _, _ = _diagnose_for(
        categories, "expanded/merchants/m_006_southindiancafe_restaurant_bangalore.json",
        _seed("trg_012_milestone_mylari"), now)
    assert diagnosis.rule == "milestone"
    assert "5" in diagnosis.story and "150" in diagnosis.story
    assert diagnosis.primary.key == "payload_milestone_gap"


def test_digest_rule_keeps_research_triggers_digest_anchored(categories, now):
    diagnosis, _, _, _, _ = _diagnose_for(
        categories, "expanded/merchants/m_001_drmeera_dentist_delhi.json",
        {"id": "t", "kind": "research_digest", "scope": "merchant",
         "payload": {"top_item_id": "d_2026W17_jida_fluoride"}},
        now)
    assert diagnosis.rule == "digest"
    assert diagnosis.primary.kind == "digest"
    assert diagnosis.primary.salience >= 0.999


def test_asset_pivot_when_nothing_material(categories, now):
    # m_006 is peer-leading on every axis: nothing material -> asset pivot
    diagnosis, _, _, _, _ = _diagnose_for(
        categories, "expanded/merchants/m_006_southindiancafe_restaurant_bangalore.json",
        {"id": "t", "kind": "festival_upcoming", "scope": "merchant",
         "payload": {"placeholder": True}},
        now)
    assert diagnosis.rule == "asset_pivot"
    assert diagnosis.primary is not None


def test_pick_primary_delegates_to_diagnosis(categories, now):
    merchant = load("expanded/merchants/m_023_sushma_salon_pune.json")
    trigger = {"id": "t", "kind": "perf_dip", "scope": "merchant", "payload": {"placeholder": True}}
    facts = derive_facts(categories["salons"], merchant, trigger, None, now)
    facts = attach_conditionals(facts, category=categories["salons"], merchant=merchant,
                                trigger=trigger, customer=None, now=now)
    primary = pick_primary(facts, trigger, merchant=merchant,
                           category=categories["salons"], now=now)
    # the funnel's driver (the call deficit) leads; the lapse is the aside
    assert primary.key == "gap_calls"


def test_m023_body_names_subscription_and_reconciles(categories, now):
    from vera.voice import resolve_voice
    from vera.fallbacks.compose import compose_fallback

    merchant = load("expanded/merchants/m_023_sushma_salon_pune.json")
    trigger = {"id": "t", "kind": "perf_dip", "scope": "merchant", "payload": {"placeholder": True}}
    facts = derive_facts(categories["salons"], merchant, trigger, None, now)
    facts = attach_conditionals(facts, category=categories["salons"], merchant=merchant,
                                trigger=trigger, customer=None, now=now)
    voice = resolve_voice(categories["salons"], merchant, None, trigger)
    message = compose_fallback("perf_dip", category=categories["salons"], merchant=merchant,
                               trigger=trigger, customer=None, facts=facts,
                               voice=voice, now=now)
    body_lower = message.body.lower()
    # both benchmark values quantified (D2.6)
    assert "2,400" in message.body and "4.0%" in message.body and "28" in message.body
    assert "2,547" in message.body and "22" in message.body
    # the subscription appears only as an aside (D2.5)
    assert "39" in body_lower and "lapse" in body_lower
    # no false causal claim: the listing is NOT asserted damaged
    assert "effect dikh raha" not in body_lower
    # a concrete service_at_price offer is named (D2.7)
    assert "Rs.99" in message.body or "Rs.149" in message.body or "Rs.299" in message.body \
        or "Rs.499" in message.body
    # Hindi grammar: no English article inside the Hindi clause (D2 grammar fix)
    assert "dono the" not in body_lower
