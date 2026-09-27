"""Tests for vera.grounding — numeral whitelist, validator codes, repair."""

import pytest

from vera.grounding.validator import grounding_map, has_hard, repair, validate
from vera.grounding.whitelist import (
    ALLOWED_CONSTANTS,
    extract_numerals,
    is_allowed_constant,
    build_whitelist,
    numeral_variants,
)
from vera.signals.derived import derive_facts, is_placeholder_trigger
from vera.text.normalize import ngrams
from vera.voice import resolve_voice, Voice
from conftest import load

NOW_HOLDER = None  # set via conftest fixture injection below


def _voice(categories):
    return resolve_voice(categories["dentists"], None, None, None)


def _plain_voice():
    return Voice(
        salutation="Dr. Meera", tone="peer_clinical", register="",
        vocab_allowed=("fluoride varnish", "caries"),
        vocab_taboo=("guaranteed", "100% safe"),
        lang="en", script="latin", emoji=None, honorific="Dr.",
    )


DENTIST_TABOO = ("guaranteed", "100% safe", "completely cure", "miracle",
                 "best in city", "doctor approved (use only when actually applicable)")


def _meera_setup(categories, rich_dentist, case_ngrams):
    category = categories["dentists"]
    trigger = {"id": "t", "kind": "research_digest", "scope": "merchant",
               "payload": {"top_item_id": "d_2026W17_jida_fluoride"}}
    facts = derive_facts(category, rich_dentist, trigger, None, _now())
    voice = resolve_voice(category, rich_dentist, None, trigger)
    return facts, voice


def _now():
    from conftest import FIXED_NOW
    return FIXED_NOW


# ---------------------------------------------------------------------------
# whitelist / numeral extraction
# ---------------------------------------------------------------------------

def test_extract_numerals_catches_all_spec_shapes():
    body = ("2410 views, 2,410 sessions, 2.1% CTR, -30% gap, +62% YoY, Rs.299, "
            "₹1499, 1.3km, 2026-11-05, 5 Nov, Wed 5 Nov, 28 April, 6pm, 19:30, "
            "12:30-1pm, 3-month, 6-month, 24h, 90 days, AT2024-1102")
    tokens = extract_numerals(body)
    joined = " | ".join(tokens)
    for expected in ("2410", "2,410", "2.1%", "-30%", "+62%", "Rs.299", "₹1499",
                     "1.3km", "2026-11-05", "5 Nov", "Wed 5 Nov", "28 April",
                     "6pm", "19:30", "3-month", "6-month", "24h", "90 days",
                     "AT2024-1102"):
        assert expected in joined, f"missing {expected} in {joined}"


def test_extract_numerals_range_and_duration_units():
    joined = " | ".join(extract_numerals("Open 12:30-1pm daily; bulk orders need 90 days notice."))
    assert "12:30-1pm" in joined
    assert "90 days" in joined


def test_numeral_variants_interconvert_percent_and_ratio():
    assert numeral_variants("2.1%") & {"2.1"}
    assert numeral_variants("0.021") & {"2.1", "0.021"}
    assert numeral_variants("6pm") & {"18:00", "6pm"}
    assert numeral_variants("2026-11-05") & {"5 nov", "nov 5"}


def test_build_whitelist_covers_fact_values_and_phrases(categories, rich_dentist):
    facts, voice = _meera_setup(categories, rich_dentist, None)
    whitelist = build_whitelist(facts, voice)
    assert "299" in whitelist      # offer price
    assert "2100" in whitelist     # trial_n
    assert "30" in whitelist       # -30% gap
    assert "2.1" in whitelist      # ctr 0.021 as percent


def test_allowed_constant_requires_time_noun_context():
    assert "10" in ALLOWED_CONSTANTS
    assert is_allowed_constant("Give me 10 min and it's live.", "10") is True
    assert is_allowed_constant("I have 10 pending files.", "10") is False


# ---------------------------------------------------------------------------
# hard codes
# ---------------------------------------------------------------------------

def test_ungrounded_numeral_is_hard(categories, rich_dentist):
    facts, voice = _meera_setup(categories, rich_dentist, None)
    body = ("Dr. Meera, your listing pulled 2,410 views this month and a cleaning "
            "at Rs.777 could lift it further. Want the draft?")
    violations = validate(body, "binary_yes_no", "vera", facts=facts, voice=voice,
                          merchant=rich_dentist)
    assert has_hard(violations)
    assert any(v.code == "ungrounded_numeral" and "777" in v.detail for v in violations)


def test_grounded_price_is_clean(categories, rich_dentist):
    facts, voice = _meera_setup(categories, rich_dentist, None)
    body = ("Dr. Meera, your listing pulled 2,410 views this month and the "
            "Dental Cleaning at Rs.299 is live. Want the draft?")
    violations = validate(body, "binary_yes_no", "vera", facts=facts, voice=voice,
                          merchant=rich_dentist)
    assert not any(v.code == "ungrounded_numeral" for v in violations)


def test_url_present_is_hard(categories, rich_dentist):
    facts, voice = _meera_setup(categories, rich_dentist, None)
    for body in ("Dr. Meera, see https://example.com/x for the abstract — want it?",
                 "Dr. Meera, see www.example.com for the abstract — want it?",
                 "Dr. Meera, your listing data is on magicpin.in — want the digest?"):
        violations = validate(body, "binary_yes_no", "vera", facts=facts, voice=voice,
                              merchant=rich_dentist)
        assert any(v.code == "url_present" for v in violations), body


def test_taboo_vocab_is_hard_for_dentists(categories, rich_dentist):
    facts, voice = _meera_setup(categories, rich_dentist, None)
    body = ("Dr. Meera, your 2,410 views this month show scaling demand — the "
             "results are guaranteed. Want the draft?")
    violations = validate(body, "binary_yes_no", "vera", facts=facts, voice=voice,
                          merchant=rich_dentist)
    assert any(v.code == "taboo_vocab" for v in violations)


def test_clean_body_has_no_taboo_violation(categories, rich_dentist):
    facts, voice = _meera_setup(categories, rich_dentist, None)
    body = ("Dr. Meera, your 2,410 views and 18 calls this month make scaling a "
            "strong recall lever. Want the draft?")
    violations = validate(body, "binary_yes_no", "vera", facts=facts, voice=voice,
                          merchant=rich_dentist)
    assert not any(v.code == "taboo_vocab" for v in violations)


def test_multiple_ctas_is_hard(categories, rich_dentist):
    facts, voice = _meera_setup(categories, rich_dentist, None)
    body = ("Dr. Meera, your 2,410 views beat the benchmark. Want the abstract? "
            "Shall I also draft the WhatsApp?")
    violations = validate(body, "binary_yes_no", "vera", facts=facts, voice=voice,
                          merchant=rich_dentist)
    assert any(v.code == "multiple_ctas" for v in violations)


def test_single_cta_is_clean(categories, rich_dentist):
    facts, voice = _meera_setup(categories, rich_dentist, None)
    body = ("Dr. Meera, your 2,410 views beat the 1,820 metro average (+32%). "
            "Shall I draft the post?")
    violations = validate(body, "binary_yes_no", "vera", facts=facts, voice=voice,
                          merchant=rich_dentist)
    assert not any(v.code == "multiple_ctas" for v in violations)


def test_multi_choice_slot_reply_numbers_allowed(categories, rich_dentist, priya):
    facts = derive_facts(categories["dentists"], rich_dentist,
                         {"id": "t", "kind": "recall_due", "scope": "customer",
                          "payload": {"service_due": "6_month_cleaning",
                                      "available_slots": [
                                          {"iso": "2026-11-05T18:00:00+05:30", "label": "Wed 5 Nov, 6pm"},
                                          {"iso": "2026-11-06T17:00:00+05:30", "label": "Thu 6 Nov, 5pm"}]}},
                         priya, _now())
    voice = resolve_voice(categories["dentists"], rich_dentist, priya,
                          {"scope": "customer", "kind": "recall_due"})
    body = ("Hi Priya, Dr. Meera's Dental Clinic here. Your recall is due — I held 2 "
            "slots: Wed 5 Nov, 6pm or Thu 6 Nov, 5pm. Reply 1 or 2, or share a time.")
    violations = validate(body, "multi_choice_slot", "merchant_on_behalf", facts=facts,
                          voice=voice, merchant=rich_dentist, customer=priya,
                          scope="customer")
    assert not any(v.code == "multiple_ctas" for v in violations)
    assert not any(v.code == "ungrounded_numeral" for v in violations)


def test_plagiarism_eight_gram_of_case_study_is_hard(categories, rich_dentist, case_ngrams):
    facts, voice = _meera_setup(categories, rich_dentist, case_ngrams)
    # 8+ word span lifted from case study 1
    body = ("Dr. Meera, one item relevant to your high-risk adult patients — "
            "worth a look. Want me to send it?")
    violations = validate(body, "binary_yes_no", "vera", facts=facts, voice=voice,
                          merchant=rich_dentist, case_corpus_ngrams=case_ngrams)
    assert any(v.code == "plagiarism" for v in violations)


def test_original_body_passes_plagiarism(categories, rich_dentist, case_ngrams):
    facts, voice = _meera_setup(categories, rich_dentist, case_ngrams)
    body = ("Dr. Meera, this week's digest has a fluoride recall item that fits "
            "your 2,410-view listing. Want the abstract draft?")
    violations = validate(body, "binary_yes_no", "vera", facts=facts, voice=voice,
                          merchant=rich_dentist, case_corpus_ngrams=case_ngrams)
    assert not any(v.code == "plagiarism" for v in violations)


def test_jargon_leak_is_hard(categories, rich_dentist):
    facts, voice = _meera_setup(categories, rich_dentist, None)
    body = ("Dr. Meera, the trigger payload shows 2,410 views this month. Want "
            "the draft?")
    violations = validate(body, "binary_yes_no", "vera", facts=facts, voice=voice,
                          merchant=rich_dentist)
    assert any(v.code == "jargon_leak" for v in violations)


def test_send_as_mismatch_is_hard(categories, rich_dentist, priya):
    facts, voice = _meera_setup(categories, rich_dentist, None)
    body = "Hi Priya, Dr. Meera's clinic here — your recall is due. Reply YES to book."
    violations = validate(body, "binary_yes_no", "vera", facts=facts, voice=voice,
                          merchant=rich_dentist, customer=priya, scope="customer")
    assert any(v.code == "send_as_mismatch" for v in violations)


def test_consent_violation_is_hard_for_promo_to_recall_only_customer(categories, rich_dentist, priya):
    facts, voice = _meera_setup(categories, rich_dentist, None)
    body = ("Hi Priya, Dr. Meera's clinic here — flat 30% off on whitening this "
            "week, deal ends Sunday. Reply YES to grab it.")
    violations = validate(body, "binary_yes_no", "merchant_on_behalf", facts=facts,
                          voice=voice, merchant=rich_dentist, customer=priya,
                          scope="customer")
    assert any(v.code == "consent_violation" for v in violations)


def test_non_latin_script_is_hard(categories, rich_dentist):
    facts, voice = _meera_setup(categories, rich_dentist, None)
    body = "Dr. Meera, आपके लिए 2 slots ready hain — want them?"
    violations = validate(body, "binary_yes_no", "vera", facts=facts, voice=voice,
                          merchant=rich_dentist)
    assert any(v.code == "non_latin_script" for v in violations)


def test_self_reintroduction_is_hard_after_first_touch(categories, rich_dentist):
    facts, voice = _meera_setup(categories, rich_dentist, None)
    body = "Hi Vera here again — your 2,410 views this month look good. Want the digest?"
    violations = validate(body, "binary_yes_no", "vera", facts=facts, voice=voice,
                          merchant=rich_dentist, is_first_touch=False)
    assert any(v.code == "self_reintroduction" for v in violations)


def test_empty_body_and_missing_fields_are_hard():
    violations = validate("   ", "binary_yes_no", "vera", facts=[], voice=_plain_voice())
    assert any(v.code == "empty_body" for v in violations)
    assert has_hard(violations)


def test_repeat_body_is_hard(categories, rich_dentist):
    facts, voice = _meera_setup(categories, rich_dentist, None)
    body = "Dr. Meera, your 2,410 views beat the 1,820 metro average (+32%). Shall I draft the post?"
    violations = validate(body, "binary_yes_no", "vera", facts=facts, voice=voice,
                          merchant=rich_dentist, prior_bodies=[body.upper()])
    assert any(v.code == "repeat_body" for v in violations)


# ---------------------------------------------------------------------------
# soft codes
# ---------------------------------------------------------------------------

def test_body_too_long_soft(categories, rich_dentist):
    facts, voice = _meera_setup(categories, rich_dentist, None)
    body = "Dr. Meera, " + "your 2,410 views are strong and the research note maps to your book. " * 8 \
        + "Want the draft?"
    violations = validate(body, "binary_yes_no", "vera", facts=facts, voice=voice,
                          merchant=rich_dentist)
    assert any(v.code == "body_too_long" and v.severity == "soft" for v in violations)
    assert not has_hard(violations)


def test_body_too_short_soft(categories, rich_dentist):
    facts, voice = _meera_setup(categories, rich_dentist, None)
    violations = validate("Dr. Meera — 2,410 views. Want it?", "binary_yes_no", "vera",
                          facts=facts, voice=voice, merchant=rich_dentist)
    assert any(v.code == "body_too_short" and v.severity == "soft" for v in violations)


def test_no_preamble_check_soft(categories, rich_dentist):
    facts, voice = _meera_setup(categories, rich_dentist, None)
    body = ("I hope you're doing well. Your 2,410 views beat the 1,820 metro "
            "average (+32%) — want the draft?")
    violations = validate(body, "binary_yes_no", "vera", facts=facts, voice=voice,
                          merchant=rich_dentist)
    assert any(v.code == "no_preamble_check" for v in violations)


def test_pct_instead_of_service_price_soft(categories, rich_dentist):
    facts, voice = _meera_setup(categories, rich_dentist, None)
    body = ("Dr. Meera, flat 30% off on cleanings this month — your 2,410 views "
            "deserve it. Want the banner?")
    violations = validate(body, "binary_yes_no", "vera", facts=facts, voice=voice,
                          merchant=rich_dentist)
    assert any(v.code == "pct_instead_of_service_price" for v in violations)


def test_no_owner_name_soft(categories, rich_dentist):
    facts, voice = _meera_setup(categories, rich_dentist, None)
    body = "Your listing pulled 2,410 views this month — want the draft?"
    violations = validate(body, "binary_yes_no", "vera", facts=facts, voice=voice,
                          merchant=rich_dentist)
    assert any(v.code == "no_owner_name" for v in violations)


def test_language_mismatch_soft(categories, rich_dentist):
    facts, voice = _meera_setup(categories, rich_dentist, None)
    from dataclasses import replace
    hi_voice = replace(voice, lang="hi_en")
    body = "Dr. Meera, your 2,410 views beat the 1,820 metro average (+32%). Shall I draft the post?"
    violations = validate(body, "binary_yes_no", "vera", facts=facts, voice=hi_voice,
                          merchant=rich_dentist)
    assert any(v.code == "language_mismatch" for v in violations)


def test_weak_specificity_soft(categories, rich_dentist):
    facts, voice = _meera_setup(categories, rich_dentist, None)
    body = "Dr. Meera, your listing is doing fine these days — want the draft?"
    violations = validate(body, "binary_yes_no", "vera", facts=facts, voice=voice,
                          merchant=rich_dentist)
    assert any(v.code == "weak_specificity" for v in violations)


def test_no_source_citation_soft(categories, rich_dentist):
    facts, voice = _meera_setup(categories, rich_dentist, None)
    body = ("Dr. Meera, a new clinical study says 3-month fluoride recall cuts "
            "caries sharply. Want the abstract?")
    violations = validate(body, "binary_yes_no", "vera", facts=facts, voice=voice,
                          merchant=rich_dentist)
    assert any(v.code == "no_source_citation" for v in violations)
    cited = body + " — JIDA Oct 2026, p.14"
    violations_cited = validate(cited, "binary_yes_no", "vera", facts=facts, voice=voice,
                                merchant=rich_dentist)
    assert not any(v.code == "no_source_citation" for v in violations_cited)


# ---------------------------------------------------------------------------
# repair
# ---------------------------------------------------------------------------

def test_repair_is_idempotent_and_never_adds_numerals(categories, rich_dentist):
    facts, voice = _meera_setup(categories, rich_dentist, None)
    body = ("Dr. Meera, your 2,410 views beat the 1,820 metro average (+32%). "
            "See www.example.com or want the draft, and shall I also post it?")
    violations = validate(body, "binary_yes_no", "vera", facts=facts, voice=voice,
                          merchant=rich_dentist)
    fixed = repair(body, violations, facts, voice)
    assert fixed is not None
    recheck = validate(fixed, "binary_yes_no", "vera", facts=facts, voice=voice,
                       merchant=rich_dentist)
    assert not has_hard(recheck)
    second_pass = repair(fixed, recheck, facts, voice)
    assert second_pass == fixed
    # no new numerals introduced
    from vera.grounding.whitelist import extract_numerals as en
    original_numerals = {t for t in en(body)}
    fixed_numerals = {t for t in en(fixed)}
    assert fixed_numerals <= original_numerals


def test_repair_returns_none_for_unrepairable(categories, rich_dentist, case_ngrams):
    facts, voice = _meera_setup(categories, rich_dentist, case_ngrams)
    lifted = ("Dr. Meera, JIDA's Oct issue landed. One item relevant to your high-risk adult "
              "patients — 2,100-patient trial showed 3-month fluoride recall cuts caries "
              "recurrence 38% better than 6-month. Worth a look.")
    violations = validate(lifted, "binary_yes_no", "vera", facts=facts, voice=voice,
                          merchant=rich_dentist, case_corpus_ngrams=case_ngrams)
    assert any(v.code == "plagiarism" for v in violations)
    assert repair(lifted, violations, facts, voice) is None


def test_grounding_map_paths_cover_body_numerals(categories, rich_dentist):
    facts, voice = _meera_setup(categories, rich_dentist, None)
    body = ("Dr. Meera, your 2,410 views and 18 calls beat the 1,820 metro "
            "average (+32%) — the Dental Cleaning at Rs.299 is live. Want the draft?")
    lines = grounding_map(body, facts, voice)
    assert not any(line.startswith("unmatched") for line in lines)
    assert any("merchant.performance.views" in line for line in lines)
    assert any("merchant.offers" in line or "offer_catalog" in line for line in lines)
