"""Tests for the conditional-fact resolver (Defect C) and recipient register (Defect E)."""

from dataclasses import replace

from vera.signals.derived import derive_facts
from vera.signals.relevance import attach_conditionals, select_offer
from vera.voice import resolve_voice
from conftest import load


def _facts_for(categories, merchant_path, now, trigger=None, customer_path=None):
    merchant = load(merchant_path)
    category = categories[merchant["category_slug"]]
    customer = load(customer_path) if customer_path else None
    if trigger is None:
        trigger = {"id": "t", "kind": "perf_dip", "scope": "merchant", "payload": {"placeholder": True}}
    facts = derive_facts(category, merchant, trigger, customer, now)
    facts = attach_conditionals(facts, category=category, merchant=merchant,
                                trigger=trigger, customer=customer, now=now)
    return facts, merchant, category, customer, trigger


def _offers(facts):
    return [f for f in facts if f.kind == "offer" and f.key.startswith("offer_")]


def _catalog(facts):
    return [f for f in facts if f.key.startswith("catalog_offer_")]


def test_senior_customer_selects_senior_offer(categories, now):
    facts, merchant, category, customer, trigger = _facts_for(
        categories, "expanded/merchants/m_009_apollo_pharmacy_jaipur.json", now,
        customer_path="expanded/customers/c_013_grandfather_for_m009.json")
    selected = select_offer(_offers(facts), _catalog(facts), customer=customer,
                            trigger=trigger, merchant=merchant, category=category)
    assert selected is not None
    assert "Senior" in str(selected.value)
    assert selected.key == "offer_active_o_apollo_002"


def test_weight_loss_gym_customer_gets_trial_not_discount(categories, now):
    facts, merchant, category, customer, trigger = _facts_for(
        categories, "expanded/merchants/m_007_powerhouse_gym_bangalore.json", now,
        customer_path="expanded/customers/c_010_rashmi_for_m007.json")
    selected = select_offer(_offers(facts), _catalog(facts), customer=customer,
                            trigger=trigger, merchant=merchant, category=category)
    assert selected is not None
    title = str(selected.value).lower()
    assert "trial" in title or "personal training" in title
    assert "off" not in title or "discount" not in title


def test_thin_merchant_still_resolves_a_priced_catalog_offer(categories, now):
    facts, merchant, category, customer, trigger = _facts_for(
        categories, "expanded/merchants/m_023_sushma_salon_pune.json", now)
    selected = select_offer(_offers(facts), _catalog(facts), customer=None,
                            trigger=trigger, merchant=merchant, category=category)
    assert selected is not None
    assert "Rs." in str(selected.value) or "@ Rs." in selected.phrase or "₹" in str(selected.value)


def test_never_percentage_discount_when_service_price_exists(categories, now):
    # restaurants catalog has BOTH 'Flat 30% OFF' (percentage_discount) and
    # 'Weekday Lunch Thali @ Rs.149' (service_at_price): the resolver must
    # prefer the service+price framing for a neutral new-user context
    facts, merchant, category, customer, trigger = _facts_for(
        categories, "expanded/merchants/m_006_southindiancafe_restaurant_bangalore.json", now)
    merchant_no_offers = dict(merchant)
    merchant_no_offers["offers"] = []
    facts2 = derive_facts(category, merchant_no_offers, trigger, None, now)
    facts2 = attach_conditionals(facts2, category=category, merchant=merchant_no_offers,
                                 trigger=trigger, customer=None, now=now)
    selected = select_offer(_offers(facts2), _catalog(facts2), customer=None,
                            trigger=trigger, merchant=merchant_no_offers, category=category)
    assert selected is not None
    title = str(selected.value).lower()
    entries = {str(e.get("id")): e for e in category["offer_catalog"]}
    chosen_type = None
    for entry in category["offer_catalog"]:
        if str(entry.get("title")) == str(selected.value):
            chosen_type = entry.get("type")
    assert chosen_type != "percentage_discount", title
    assert chosen_type in ("service_at_price", "free_service", "free_trial", "free_addon", "membership")


def test_attach_conditionals_surfaces_runway_and_personalisation(categories, now):
    # m_004 Glamour: subscription expired 38 days (>21, so no runway fact);
    # use m_002 Bharat whose renewal is due in 12 days
    facts, merchant, _, _, _ = _facts_for(
        categories, "expanded/merchants/m_002_bharat_dentist_mumbai.json", now)
    keys = {f.key for f in facts}
    assert "runway_days_remaining" in keys
    runway = next(f for f in facts if f.key == "runway_days_remaining")
    assert runway.salience == 1.0

    # Kavya's preferences carry a wedding_date — cheapest personalisation point
    facts_k, _, _, _, _ = _facts_for(
        categories, "expanded/merchants/m_003_studio11_salon_hyderabad.json", now,
        customer_path="expanded/customers/c_005_kavya_for_m003.json")
    kkeys = {f.key for f in facts_k}
    assert "conditional_wedding_date" in kkeys
    assert "customer_preferred_slots" in kkeys


def test_review_quote_boosted_to_salience_one(categories, now):
    facts, _, _, _, _ = _facts_for(
        categories, "expanded/merchants/m_001_drmeera_dentist_delhi.json", now)
    quote = next(f for f in facts if f.key.startswith("review_quote_"))
    assert quote.salience == 1.0


def test_channel_via_son_sets_guardian_recipient(categories, now):
    merchant = load("expanded/merchants/m_009_apollo_pharmacy_jaipur.json")
    customer = load("expanded/customers/c_013_grandfather_for_m009.json")
    trigger = {"id": "t", "kind": "chronic_refill_due", "scope": "customer",
               "payload": {"placeholder": True}}
    voice = resolve_voice(categories["pharmacies"], merchant, customer, trigger)
    assert voice.recipient == "guardian"
    assert voice.third_person_subject == "Sharma ji"
    assert voice.salutation == "Namaste"


def test_channel_via_parent_sets_parent_recipient(categories, now):
    merchant = load("expanded/merchants/m_008_zenyoga_gym_chennai.json")
    customer = load("expanded/customers/c_012_karthik_jr_for_m008.json")
    trigger = {"id": "t", "kind": "trial_followup", "scope": "customer",
               "payload": {"placeholder": True}}
    voice = resolve_voice(categories["gyms"], merchant, customer, trigger)
    assert voice.recipient == "parent"
    assert voice.third_person_subject == "Karthik"
    assert voice.salutation == "Sumitra"


def test_refill_body_addresses_the_intermediary(categories, now):
    from vera.fallbacks.compose import compose_fallback

    merchant = load("expanded/merchants/m_009_apollo_pharmacy_jaipur.json")
    customer = load("expanded/customers/c_013_grandfather_for_m009.json")
    trigger = next(t for t in load("dataset/triggers_seed.json")["triggers"]
                   if t["id"] == "trg_019_chronic_refill_grandfather")
    category = categories["pharmacies"]
    facts = attach_conditionals(derive_facts(category, merchant, trigger, customer, now),
                                category=category, merchant=merchant, trigger=trigger,
                                customer=customer, now=now)
    voice = resolve_voice(category, merchant, customer, trigger)
    message = compose_fallback("chronic_refill_due", category=category, merchant=merchant,
                               trigger=trigger, customer=customer, facts=facts,
                               voice=voice, now=now)
    assert "Sharma ji ki" in message.body          # third person for the customer
    assert "aapke bete" not in message.body        # the reader IS the son
    assert "Reply CONFIRM" in message.body         # ask directed at the reader
    assert "15%" in message.body                   # senior discount applied
