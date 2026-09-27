"""Tests for vera.signals.derived — fact extraction, ranking, determinism."""

from datetime import datetime, timezone

from vera.signals.derived import (
    consent_allows,
    derive_facts,
    facts_by_kind,
    is_placeholder_trigger,
    permitted_intent,
    pick_primary,
    rank_facts,
)
from conftest import load

NOW = datetime(2026, 4, 26, 10, 30, 0, tzinfo=timezone.utc)


def _facts_for(category, merchant, trigger, customer=None):
    return derive_facts(category, merchant, trigger, customer, NOW)


def test_thin_merchant_still_yields_six_plus_facts(categories, thin_salon):
    trigger = {"id": "t", "kind": "perf_dip", "scope": "merchant",
               "payload": {"placeholder": True, "metric_or_topic": "perf_dip"}}
    facts = _facts_for(categories["salons"], thin_salon, trigger)
    assert len(facts) >= 6


def test_gap_arithmetic_ctr_minus_30(categories, rich_dentist):
    trigger = {"id": "t", "kind": "perf_spike", "scope": "merchant", "payload": {"placeholder": True}}
    facts = _facts_for(categories["dentists"], rich_dentist, trigger)
    gap = next(f for f in facts if f.key == "gap_ctr")
    assert gap.value == -30
    assert "merchant.performance.ctr" in gap.json_path
    assert "category.peer_stats.avg_ctr" in gap.json_path


def test_gap_arithmetic_ctr_plus_45(categories, thin_salon):
    # 0.058 vs peer 0.040 -> +45%
    trigger = {"id": "t", "kind": "perf_spike", "scope": "merchant", "payload": {"placeholder": True}}
    facts = _facts_for(categories["salons"], thin_salon, trigger)
    gap = next(f for f in facts if f.key == "gap_ctr")
    assert gap.value == 45


def test_no_facts_for_absent_fields(categories, thin_salon):
    trigger = {"id": "t", "kind": "perf_spike", "scope": "merchant", "payload": {"placeholder": True}}
    facts = _facts_for(categories["salons"], thin_salon, trigger)
    keys = {f.key for f in facts}
    # thin merchant has no offers / review themes / lapsed counts / high-risk cohort
    assert not any(k.startswith("offer_") for k in keys)
    assert not any(k.startswith("review_") for k in keys)
    assert "high_risk_adult_count" not in keys
    assert "lapsed_180d_plus" not in keys
    # ...and the rich merchant DOES produce them
    rich = _facts_for(categories["dentists"], load("expanded/merchants/m_001_drmeera_dentist_delhi.json"),
                      trigger)
    rkeys = {f.key for f in rich}
    assert "offer_active_o_meera_001" in rkeys
    assert "offer_expired_o_meera_002" in rkeys
    assert "high_risk_adult_count" in rkeys
    assert "lapsed_180d_plus_share" in rkeys


def test_derive_facts_is_deterministic(categories, rich_dentist):
    trigger = {"id": "t", "kind": "research_digest", "scope": "merchant",
               "payload": {"top_item_id": "d_2026W17_jida_fluoride"}}
    a = _facts_for(categories["dentists"], rich_dentist, trigger)
    b = _facts_for(categories["dentists"], rich_dentist, trigger)
    assert a == b
    assert rank_facts(a, trigger) == rank_facts(b, trigger)


def test_rank_facts_is_stable_and_total(categories, rich_dentist):
    trigger = {"id": "t", "kind": "research_digest", "scope": "merchant",
               "payload": {"top_item_id": "d_2026W17_jida_fluoride"}}
    facts = _facts_for(categories["dentists"], rich_dentist, trigger)
    once = rank_facts(facts, trigger)
    twice = rank_facts(once, trigger)
    assert once == twice
    keys = [f.key for f in once]
    assert len(keys) == len(set(keys))


def test_pick_primary_is_trigger_anchored_for_real_payload(categories, rich_dentist):
    trigger = {"id": "t", "kind": "research_digest", "scope": "merchant",
               "payload": {"top_item_id": "d_2026W17_jida_fluoride"}}
    facts = _facts_for(categories["dentists"], rich_dentist, trigger)
    primary = pick_primary(facts, trigger)
    assert primary is not None
    assert primary.kind == "digest" and primary.salience >= 0.999
    assert primary.json_path.startswith("category.digest[")


def test_pick_primary_is_peer_or_season_anchored_for_placeholder(categories, thin_salon):
    trigger = {"id": "t", "kind": "perf_dip", "scope": "merchant",
               "payload": {"placeholder": True, "metric_or_topic": "perf_dip"}}
    facts = _facts_for(categories["salons"], thin_salon, trigger)
    primary = pick_primary(facts, trigger)
    assert primary is not None
    # m_023 has an expired subscription: the diagnosis names the subscription
    # as the leading cause (Defect B) — never a stapled CTR-vs-calls pair
    assert primary.kind in ("seasonal", "delta", "subscription") \
        or primary.key.startswith("gap_")


def test_placeholder_flag(categories, rich_dentist):
    placeholder = {"id": "t", "kind": "perf_dip", "scope": "merchant",
                   "payload": {"placeholder": True, "metric_or_topic": "perf_dip"}}
    real = {"id": "t", "kind": "perf_dip", "scope": "merchant",
            "payload": {"metric": "calls", "delta_pct": -0.5}}
    assert is_placeholder_trigger(placeholder) is True
    assert is_placeholder_trigger(real) is False
    facts_p = _facts_for(categories["dentists"], rich_dentist, placeholder)
    facts_r = _facts_for(categories["dentists"], rich_dentist, real)
    assert not any(f.kind == "trigger" for f in facts_p)
    assert any(f.kind == "trigger" for f in facts_r)


def test_digest_indirection_resolution(categories, rich_dentist):
    trigger = {"id": "t", "kind": "research_digest", "scope": "merchant",
               "payload": {"top_item_id": "d_2026W17_jida_fluoride"}}
    facts = _facts_for(categories["dentists"], rich_dentist, trigger)
    resolved = next(f for f in facts if f.key == "digest_resolved_d_2026W17_jida_fluoride")
    assert "fluoride" in resolved.phrase.lower() or "recall" in resolved.phrase.lower()
    assert resolved.citation == "JIDA Oct 2026, p.14"
    trial = next(f for f in facts if f.key == "digest_resolved_d_2026W17_jida_fluoride_trial_n")
    assert trial.value == 2100


def test_slot_labels_kept_verbatim(categories, rich_dentist, priya):
    trigger = next(t for t in load("dataset/triggers_seed.json")["triggers"]
                   if t["id"] == "trg_003_recall_due_priya")
    facts = _facts_for(categories["dentists"], rich_dentist, trigger, priya)
    slots = next(f for f in facts if f.key == "payload_available_slots")
    assert slots.value == ["Wed 5 Nov, 6pm", "Thu 6 Nov, 5pm"]
    count = next(f for f in facts if f.key == "payload_available_slots_count")
    assert count.value == 2


def test_seasonal_beats_in_and_out_of_season(categories, rich_dentist):
    trigger = {"id": "t", "kind": "festival_upcoming", "scope": "merchant",
               "payload": {"placeholder": True}}
    facts = _facts_for(categories["dentists"], rich_dentist, trigger)  # April
    seasonal = {f.key: f.salience for f in facts if f.kind == "seasonal"}
    # April falls inside the dentists' Apr-Jun beat, outside Nov-Feb / Oct-Dec / Jan
    in_season = [k for k, s in seasonal.items() if s >= 0.999]
    out_season = [k for k, s in seasonal.items() if s <= 0.25]
    assert len(in_season) == 1
    assert len(out_season) == len(seasonal) - 1

    november = datetime(2026, 11, 15, 10, 0, 0, tzinfo=timezone.utc)
    facts_nov = derive_facts(categories["dentists"], rich_dentist, trigger, None, november)
    seasonal_nov = {f.key: f.salience for f in facts_nov if f.kind == "seasonal"}
    assert sum(1 for s in seasonal_nov.values() if s >= 0.999) >= 2  # Nov-Feb + Oct-Dec


def test_customer_facts_and_consent(categories, rich_dentist, priya):
    trigger = {"id": "t", "kind": "customer_lapsed_soft", "scope": "customer",
               "payload": {"placeholder": True}}
    facts = _facts_for(categories["dentists"], rich_dentist, trigger, priya)
    keys = {f.key for f in facts}
    assert "customer_name" in keys
    assert "consent_scope" in keys
    assert "customer_language" in keys
    # Priya's last_visit (2026-05-12) is after the fixed now -> no stale "days since"
    assert "customer_days_since_last_visit" not in keys
    # her consent is recall/appointment only -> promotional must not be permitted
    assert permitted_intent(priya) == "recall"
    assert consent_allows(priya["consent"]["scope"], "promotional") is False
    assert consent_allows(priya["consent"]["scope"], "recall") is True


def test_days_since_last_visit_computed(categories, rich_dentist, sharma):
    trigger = {"id": "t", "kind": "chronic_refill_due", "scope": "customer",
               "payload": {"molecule_list": ["metformin"], "stock_runs_out_iso": "2026-04-28"}}
    facts = _facts_for(categories["dentists"], rich_dentist, trigger, sharma)
    days = next(f for f in facts if f.key == "customer_days_since_last_visit")
    assert days.value == 4  # 2026-04-22 -> 2026-04-26


def test_milestone_gap_derivation(categories):
    merchant = load("expanded/merchants/m_006_southindiancafe_restaurant_bangalore.json")
    trigger = next(t for t in load("dataset/triggers_seed.json")["triggers"]
                   if t["id"] == "trg_012_milestone_mylari")
    facts = _facts_for(categories["restaurants"], merchant, trigger)
    gap = next(f for f in facts if f.key == "payload_milestone_gap")
    assert gap.value == 5
    assert gap.json_path == "derived:milestone_value - value_now"


def test_signals_parsed_from_strings(categories):
    merchant = load("expanded/merchants/m_001_drmeera_dentist_delhi.json")
    trigger = {"id": "t", "kind": "perf_spike", "scope": "merchant", "payload": {"placeholder": True}}
    facts = _facts_for(categories["dentists"], merchant, trigger)
    stale = next(f for f in facts if f.key.startswith("signal_stale_posts"))
    assert stale.value == 22
    assert "22 days ago" in stale.phrase


def test_facts_by_kind(categories, rich_dentist):
    trigger = {"id": "t", "kind": "perf_spike", "scope": "merchant", "payload": {"placeholder": True}}
    facts = _facts_for(categories["dentists"], rich_dentist, trigger)
    offers = facts_by_kind(facts, "offer")
    assert offers and all(f.kind == "offer" for f in offers)
    active = [f for f in offers if f.salience >= 0.9]
    expired = [f for f in offers if f.salience <= 0.6]
    assert active and expired  # active ranked before expired
