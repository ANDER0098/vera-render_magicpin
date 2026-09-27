import json
from datetime import datetime, timezone

import pytest

from vera.signals.diagnose import diagnose
from vera.grounding.validator import validate, has_hard, repair
from vera.signals.derived import derive_facts
from conftest import load

NOW = datetime(2026, 4, 26, 10, 30, tzinfo=timezone.utc)


def _voice(categories):
    from vera.voice import resolve_voice

    return resolve_voice(categories["dentists"], None, None, None)


def test_unsupported_causal_claim_fires_without_license(categories, rich_dentist):
    facts = derive_facts(categories["dentists"], rich_dentist, None, None, NOW)
    body = ("Dr. Meera, your 2,410 views are strong isliye the calls will follow. "
            "Want the draft?")
    violations = validate(body, "binary_yes_no", "vera", facts=facts, voice=_voice(categories),
                          merchant=rich_dentist, licensed_claims=())
    assert any(v.code == "unsupported_causal_claim" and "isliye" in v.detail
               for v in violations)


def test_causal_claim_passes_when_licensed(categories, rich_dentist):
    facts = derive_facts(categories["dentists"], rich_dentist, None, None, NOW)
    body = ("Dr. Meera, your 2,410 views are strong isliye the calls will follow. "
            "Want the draft?")
    violations = validate(body, "binary_yes_no", "vera", facts=facts, voice=_voice(categories),
                          merchant=rich_dentist, licensed_claims=("isliye",))
    assert not any(v.code == "unsupported_causal_claim" for v in violations)


@pytest.mark.parametrize("connective", [
    "because", "ki wajah se", "effect dikh raha", "leading to", "due to",
    "ke karan", "matlab", "kyunki",
])
def test_each_causal_connective_is_checked(categories, rich_dentist, connective):
    facts = derive_facts(categories["dentists"], rich_dentist, None, None, NOW)
    body = f"Dr. Meera, your 2,410 views grew {connective} the listing improved. Want the draft?"
    violations = validate(body, "binary_yes_no", "vera", facts=facts, voice=_voice(categories),
                          merchant=rich_dentist, licensed_claims=())
    assert any(v.code == "unsupported_causal_claim" for v in violations), connective


def test_merchant_named_karan_is_not_a_causal_claim(categories, now):
    from vera.fallbacks.compose import compose_fallback
    from vera.voice import resolve_voice

    merchant = load("expanded/merchants/m_039_karan_gym_jaipur.json")
    trigger = {"id": "t", "kind": "perf_dip", "scope": "merchant", "payload": {"placeholder": True}}
    category = categories["gyms"]
    facts = derive_facts(category, merchant, trigger, None, now)
    voice = resolve_voice(category, merchant, None, trigger)
    message = compose_fallback("perf_dip", category=category, merchant=merchant,
                               trigger=trigger, customer=None, facts=facts,
                               voice=voice, now=now)
    assert not any(v.code == "unsupported_causal_claim" for v in
                   validate(message.body, message.cta, message.send_as, facts=facts,
                            voice=voice, merchant=merchant, trigger=trigger,
                            licensed_claims=diagnose(facts, merchant=merchant,
                                                     category=category, trigger=trigger,
                                                     now=now).licensed_claims)), message.body


def test_fact_dump_fires_on_same_family_list(categories, rich_dentist):
    from vera.signals.relevance import attach_conditionals

    trigger = {"id": "t", "kind": "category_seasonal", "scope": "merchant",
               "payload": {"season": "summer_2026",
                           "trends": ["ORS_demand_+40", "sunscreen_demand_+38",
                                      "antifungal_demand_+45", "cold_cough_demand_-60"]}}
    facts = attach_conditionals(
        derive_facts(categories["pharmacies"], load("expanded/merchants/m_009_apollo_pharmacy_jaipur.json"),
                     trigger, None, NOW),
        category=categories["pharmacies"],
        merchant=load("expanded/merchants/m_009_apollo_pharmacy_jaipur.json"),
        trigger=trigger, customer=None, now=NOW)
    voice = _voice(categories)
    body = ("Ramesh, this summer demand is shifting: ORS demand +40%, sunscreen demand +38%, "
            "antifungal demand +45%, cold cough demand -60%. Want the list?")
    violations = validate(body, "binary_yes_no", "vera", facts=facts, voice=voice,
                          merchant=None, trigger=trigger)
    assert any(v.code == "fact_dump" for v in violations)


def test_molecule_list_is_exempt_from_fact_dump(categories, now):
    from vera.fallbacks.compose import compose_fallback
    from vera.voice import resolve_voice

    merchant = load("expanded/merchants/m_009_apollo_pharmacy_jaipur.json")
    customer = load("expanded/customers/c_013_grandfather_for_m009.json")
    trigger = next(t for t in load("dataset/triggers_seed.json")["triggers"]
                   if t["id"] == "trg_019_chronic_refill_grandfather")
    category = categories["pharmacies"]
    from vera.signals.relevance import attach_conditionals
    facts = attach_conditionals(derive_facts(category, merchant, trigger, customer, now),
                                category=category, merchant=merchant, trigger=trigger,
                                customer=customer, now=now)
    voice = resolve_voice(category, merchant, customer, trigger)
    message = compose_fallback("chronic_refill_due", category=category, merchant=merchant,
                               trigger=trigger, customer=customer, facts=facts,
                               voice=voice, now=now)
    assert not any(v.code == "fact_dump" for v in
                   validate(message.body, message.cta, message.send_as, facts=facts,
                            voice=voice, merchant=merchant, customer=customer,
                            trigger=trigger, scope="customer")), message.body


def test_offer_condition_dropped_fires_and_passes(categories, now):
    from vera.signals.relevance import attach_conditionals
    from vera.voice import resolve_voice

    merchant = load("expanded/merchants/m_009_apollo_pharmacy_jaipur.json")
    trigger = {"id": "t", "kind": "chronic_refill_due", "scope": "customer",
               "payload": {"molecule_list": ["metformin"], "stock_runs_out_iso": "2026-04-28"}}
    category = categories["pharmacies"]
    facts = attach_conditionals(derive_facts(category, merchant, trigger, None, NOW),
                                category=category, merchant=merchant, trigger=trigger,
                                customer=None, now=NOW)
    voice = resolve_voice(category, merchant, None, trigger)
    dropped = ("Namaste — Apollo Health Plus Pharmacy here. Free home delivery to your "
               "registered address. Reply CONFIRM to dispatch.")
    violations = validate(dropped, "binary_confirm_cancel", "merchant_on_behalf", facts=facts,
                          voice=voice, merchant=merchant, trigger=trigger, scope="customer")
    assert any(v.code == "offer_condition_dropped" and "499" in v.detail for v in violations)

    conditioned = ("Namaste — Apollo Health Plus Pharmacy here. Free home delivery on orders "
                   "above Rs.499 to your registered address. Reply CONFIRM to dispatch.")
    violations_ok = validate(conditioned, "binary_confirm_cancel", "merchant_on_behalf",
                             facts=facts, voice=voice, merchant=merchant, trigger=trigger,
                             scope="customer")
    assert not any(v.code == "offer_condition_dropped" for v in violations_ok)


def test_offer_condition_not_fired_when_offer_unreferenced(categories, now):
    from vera.signals.relevance import attach_conditionals
    from vera.voice import resolve_voice

    merchant = load("expanded/merchants/m_009_apollo_pharmacy_jaipur.json")
    trigger = {"id": "t", "kind": "perf_dip", "scope": "merchant", "payload": {"placeholder": True}}
    category = categories["pharmacies"]
    facts = attach_conditionals(derive_facts(category, merchant, trigger, None, NOW),
                                category=category, merchant=merchant, trigger=trigger,
                                customer=None, now=NOW)
    voice = resolve_voice(category, merchant, None, trigger)
    body = ("Ramesh, your 1,850 views and 38 calls this month look healthy versus peer "
            "norms. Shall I send the digest?")
    violations = validate(body, "binary_yes_no", "vera", facts=facts, voice=voice,
                          merchant=merchant, trigger=trigger)
    assert not any(v.code == "offer_condition_dropped" for v in violations)
