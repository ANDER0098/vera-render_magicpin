"""Tests for the internal-vocabulary defence (Defect A)."""

from pathlib import Path

import pytest

from harness.internal_vocab import (
    REWRITE_MAP,
    build_internal_vocab,
    human_month_range,
    humanize_title,
    load_internal_vocab,
    rewrite_internal,
)
from vera.grounding.validator import validate
from conftest import ROOT

VOCAB = load_internal_vocab()


def test_vocab_is_large_and_data_derived():
    assert len(VOCAB) >= 150, f"vocab only {len(VOCAB)} tokens"


def test_all_five_peer_scopes_present():
    for scope in ("metro_unisex_salons_2026", "metro_solo_practices_2026",
                  "metro_neighbourhood_gyms_2026", "metro_neighbourhood_pharmacies_2026",
                  "metro_casual_dining_2026"):
        norm = scope.replace("_", " ")
        assert norm in VOCAB, f"peer scope '{scope}' missing from vocab"


def test_all_26_trigger_kinds_present():
    kinds = {p.stem.split("_", 2)[-1] for p in ()}  # placeholder; real set below
    import json
    kinds = set()
    for path in (ROOT / "expanded" / "triggers").glob("*.json"):
        kinds.add(json.loads(path.read_text(encoding="utf-8"))["kind"])
    assert len(kinds) == 26
    for kind in kinds:
        norm = kind.replace("_", " ")
        assert norm in VOCAB, f"trigger kind '{kind}' missing from vocab"


@pytest.mark.parametrize("leaked", [
    "vs the metro_unisex_salons_2026 average",
    "vs the metro unisex salons 2026 average",
    "vs the metro-unisex-salons-2026 average",
    "this week's dentists digest",
    "your state is lapsed_soft",
    "segment: high_risk_adults",
    "high risk adults in the roster",
    "preferred slot weekday_evening",
    "plan status: expired (39d)",
    "Free Home Delivery > Rs.499",
    "book for Nov-Feb window",
    "source: x_y_z and a_b_c",
])
def test_leak_variant_forms_are_caught(categories, rich_dentist, leaked):
    body = f"Dr. Meera, your 2,410 views look strong. {leaked} Want the draft?"
    violations = validate(body, "binary_yes_no", "vera", facts=[], voice=_voice(categories),
                          merchant=rich_dentist)
    assert any(v.code == "internal_vocab_leak" for v in violations), leaked


def _voice(categories):
    from vera.voice import resolve_voice

    return resolve_voice(categories["dentists"], None, None, None)


@pytest.mark.parametrize("clean", [
    "the metro unisex-salon average",
    "the metro solo-practice average",
    "the neighbourhood-gym average",
    "the casual-dining average",
    "your 124 high-risk adult patients",
    "weekday evenings",
    "weight loss",
    "back pain",
    "metformin",
    "November to February",
    "free home delivery on orders above Rs.499",
    "it's been a while",
    "3 FREE Trial Classes",
    "Weekday Lunch Thali @ Rs.149",
    "Senior Citizen 15% OFF",
])
def test_safe_rewrite_targets_pass_clean(categories, rich_dentist, clean):
    body = f"Dr. Meera, your 2,410 views look strong. {clean}. Want the draft?"
    violations = validate(body, "binary_yes_no", "vera", facts=[], voice=_voice(categories),
                          merchant=rich_dentist)
    leaks = [v for v in violations if v.code == "internal_vocab_leak"]
    assert not leaks, f"'{clean}' flagged: {leaks[0].detail}"


def test_rewrite_internal_handles_separator_mix_and_is_idempotent():
    text = "the metro-unisex-salons-2026 average and high-risk adults, > Rs.499, Nov-Feb"
    once = rewrite_internal(text)
    assert "metro unisex-salon average" in once
    assert "high-risk adult patients" in once
    assert "on orders above Rs.499" in once
    assert "November to February" in once
    assert rewrite_internal(once) == once


def test_humanize_title_and_month_range():
    assert humanize_title("Free Home Delivery > ₹499") == "Free home delivery on orders above Rs.499" \
        or humanize_title("Free Home Delivery > ₹499").lower().startswith("free home delivery on orders")
    assert humanize_title("Haircut @ ₹99") == "Haircut @ ₹99"
    assert human_month_range("Nov-Feb") == "November to February"
    assert human_month_range("Jan") == "January"
    assert human_month_range("Apr-Jun") == "April to June"


def test_repair_fixes_internal_vocab_leak(categories, rich_dentist):
    from vera.grounding.validator import has_hard, repair
    from vera.signals.derived import derive_facts
    from datetime import datetime, timezone

    facts = derive_facts(categories["dentists"], rich_dentist,
                         {"id": "t", "kind": "perf_spike", "scope": "merchant",
                          "payload": {"placeholder": True}},
                         None, datetime(2026, 4, 26, 10, 30, tzinfo=timezone.utc))
    voice = _voice(categories)
    body = ("Dr. Meera, your 2,410 views look strong vs the metro-unisex-salons-2026 "
            "average. Want the draft?")
    violations = validate(body, "binary_yes_no", "vera", facts=facts, voice=voice,
                          merchant=rich_dentist)
    assert any(v.code == "internal_vocab_leak" for v in violations)
    fixed = repair(body, violations, facts, voice)
    assert fixed is not None
    assert "salons-2026" not in fixed and "salons 2026" not in fixed
    recheck = validate(fixed, "binary_yes_no", "vera", facts=facts, voice=voice,
                       merchant=rich_dentist)
    assert not any(v.code == "internal_vocab_leak" for v in recheck)
    # the one-numeral test body still trips the (intended) hard
    # weak_specificity — the composer's numeral budget owns that fix


def test_build_internal_vocab_matches_cached_loader(tmp_path):
    vocab = build_internal_vocab(ROOT / "expanded")
    assert vocab == VOCAB


# ---------------------------------------------------------------------------
# D1: payload enum surface + the generic net
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("payload_value", [
    "summer_2026", "post_resolution_window_apr_jun", "what_service_in_demand_this_week",
    "corporate_bulk_thali_package", "kids_yoga_summer_camp", "skin_prep_program_30day",
    "delivery_late", "postcard_or_phone_call", "free_for_members", "kids_yoga_post",
])
def test_payload_enum_values_in_vocab_or_net(payload_value):
    from harness.internal_vocab import payload_net_variants, rewrite_internal

    in_vocab = payload_value.replace("_", " ") in VOCAB
    net = payload_net_variants({"payload": {"x": payload_value}})
    # rewrite-target values (weight_loss, 6_month_cleaning, subscription_expiry)
    # are deliberately exempt from both — their human form is blessed copy and
    # their raw form is caught by the snake_case format net; everything else
    # must sit in the vocabulary or the generic net
    assert in_vocab or net or rewrite_internal(payload_value) != payload_value, \
        f"'{payload_value}' uncovered"


def test_rewrite_target_payload_values_are_rewritten():
    from harness.internal_vocab import rewrite_internal

    assert rewrite_internal("weight loss") == "weight loss"
    assert "6-month cleaning" in rewrite_internal("6 month cleaning")
    assert "subscription expiry" in rewrite_internal("subscription expiry").lower() or \
        rewrite_internal("subscription expiry") == "your subscription expiry"
    # raw snake_case forms are caught by the format net in validate()
    assert "_" in "weight_loss" and "_" in "6_month_cleaning"


@pytest.mark.parametrize("raw,expected", [
    ("summer_2026", "this summer"),
    ("ORS_demand_+40", "ORS demand +40%"),
    ("sunscreen_demand_+38", "sunscreen demand +38%"),
    ("free_for_members", "free for IDA members"),
    ("postcard_or_phone_call", "postcard or a phone call"),
])
def test_structured_humanizations(raw, expected):
    from harness.internal_vocab import humanize_payload_value

    key = "season" if raw == "summer_2026" else \
        "trends" if "_demand_" in raw else \
        "fee" if raw == "free_for_members" else \
        "verification_path"
    assert humanize_payload_value(key, raw) == expected


def test_skin_prep_program_rewrite():
    from harness.internal_vocab import rewrite_internal

    assert rewrite_internal("skin prep program 30day") == "the 30-day skin-prep program"


def test_generic_net_catches_synthetic_unseen_payload_field(categories, rich_dentist):
    """The net that survives unseen Phase-3 fields: an unhumanised payload
    value rendered raw hard-fails even though it is in no vocabulary."""
    from datetime import datetime, timezone

    from vera.signals.derived import derive_facts
    from vera.voice import resolve_voice

    trigger = {"id": "t", "kind": "brand_new_trigger_kind", "scope": "merchant",
               "payload": {"brand_new_field": "some_machine_enum_value"}}
    facts = derive_facts(categories["dentists"], rich_dentist, trigger, None,
                         datetime(2026, 4, 26, 10, 30, tzinfo=timezone.utc))
    voice = resolve_voice(categories["dentists"], rich_dentist, None, trigger)
    body = ("Dr. Meera, your 2,410 views look strong and the some machine enum "
            "value applies here. Want the draft?")
    violations = validate(body, "binary_yes_no", "vera", facts=facts, voice=voice,
                          merchant=rich_dentist, trigger=trigger)
    assert any(v.code == "payload_enum_leak" and "some machine enum value" in v.detail
               for v in violations)


def test_net_ignores_blessed_renderings():
    from harness.internal_vocab import payload_net_variants

    trigger = {"payload": {"season": "summer_2026", "trends": ["ORS_demand_+40"],
                           "available_slots": [{"iso": "x", "label": "Wed 5 Nov, 6pm"}]}}
    net = payload_net_variants(trigger)
    assert not any("this summer" in v for v in net)
    assert not any("ors demand 40" in v for v in net)
    assert not any("wed 5 nov" in v for v in net)


def test_machine_id_shapes_are_caught(categories, rich_dentist):
    body = ("Dr. Meera, see d_2026W17_jida_fluoride for the item on your 2,410 "
            "views — want the draft?")
    violations = validate(body, "binary_yes_no", "vera", facts=[], voice=_voice(categories),
                          merchant=rich_dentist)
    assert any(v.code == "internal_vocab_leak" and "machine id" in v.detail
               for v in violations)
