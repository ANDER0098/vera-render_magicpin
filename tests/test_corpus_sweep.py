"""Golden corpus sweep: all 100 triggers, both `now` values, zero hard violations.

Writes harness/out/fallback_sweep.jsonl (one line per trigger) for human copy
review, prints the violation histogram, fails the build on internal-vocab
leaks, on >5% weak_specificity, or on any machine glyph in a body.
"""

import json
import re
from datetime import datetime

import pytest

from harness.sweep import run_sweep
from conftest import ROOT

OUT_PATH = ROOT / "harness" / "out" / "fallback_sweep.jsonl"
_MACHINE_GLYPHS = re.compile(r"[_><=]")


def _assert_clean(result: dict) -> dict:
    assert result["rendered"] == 100, f"expected 100 triggers, got {result['rendered']}"
    assert result["hard_violations"] == 0, "hard violations in the corpus sweep"
    assert not result["duplicate_bodies"], \
        f"duplicate bodies within a merchant: {result['duplicate_bodies']}"
    return result


def test_sweep_fixed_now_2026_04_26():
    now = datetime(2026, 4, 26, 10, 30, 0)
    result = run_sweep(now, OUT_PATH)
    _assert_clean(result)
    print(f"\ncorpus sweep histogram (100 triggers, now=2026-04-26):")
    for code in sorted(result["soft_by_code"]):
        print(f"  soft {code}: {result['soft_by_code'][code]}")
    if not result["soft_by_code"]:
        print("  soft: none")
    assert result["soft_by_code"].get("internal_vocab_leak", 0) == 0
    weak = result["soft_by_code"].get("weak_specificity", 0)
    assert weak <= 100 * 0.05, f"weak_specificity on {weak}% of the corpus (cap 5%)"


def test_sweep_real_wall_clock_now_is_also_clean():
    real_now = datetime.now()
    result = run_sweep(real_now, None)
    _assert_clean(result)


def test_sweep_far_future_now_2027_is_also_clean():
    """T1 robustness at an alternate time — Phase 3 can inject any clock."""
    now = datetime(2027, 11, 15, 9, 0, 0)
    result = run_sweep(now, None)
    _assert_clean(result)


def test_seven_payload_canonical_pairs_render_clean():
    """D1: the 7 canonical pairs carrying payload-enum fields (T01, T02, T05,
    T06, T13, T20, T28) must render with zero leaks."""
    from vera.fallbacks.compose import compose_fallback
    from vera.signals.derived import derive_facts
    from vera.signals.relevance import attach_conditionals
    from vera.voice import resolve_voice
    from vera.grounding.validator import has_hard, validate

    pairs = json.loads((ROOT / "expanded" / "test_pairs.json").read_text(encoding="utf-8"))["pairs"]
    triggers, merchants = {}, {}
    for path in (ROOT / "expanded" / "triggers").glob("*.json"):
        t = json.loads(path.read_text(encoding="utf-8"))
        triggers[t["id"]] = t
    for path in (ROOT / "expanded" / "merchants").glob("*.json"):
        m = json.loads(path.read_text(encoding="utf-8"))
        merchants[m["merchant_id"]] = m
    categories = {p.stem: json.loads(p.read_text(encoding="utf-8"))
                  for p in (ROOT / "expanded" / "categories").glob("*.json")}
    now = datetime(2026, 4, 26, 10, 30, 0)
    wanted = {"T01", "T02", "T05", "T06", "T13", "T20", "T28"}
    checked = set()
    for pair in pairs:
        if pair["test_id"] not in wanted:
            continue
        trigger = triggers[pair["trigger_id"]]
        merchant = merchants[trigger["merchant_id"]]
        category = categories[merchant["category_slug"]]
        facts = attach_conditionals(
            derive_facts(category, merchant, trigger, None, now),
            category=category, merchant=merchant, trigger=trigger,
            customer=None, now=now)
        voice = resolve_voice(category, merchant, None, trigger)
        message = compose_fallback(trigger["kind"], category=category, merchant=merchant,
                                   trigger=trigger, customer=None, facts=facts,
                                   voice=voice, now=now)
        violations = validate(message.body, message.cta, message.send_as, facts=facts,
                              voice=voice, category=category, merchant=merchant,
                              trigger=trigger, scope=trigger.get("scope"))
        assert not has_hard(violations), \
            f"{pair['test_id']}: {violations}\n{message.body}"
        checked.add(pair["test_id"])
    assert checked == wanted, f"missing pairs: {wanted - checked}"


def test_hi_dominance_lint_for_hi_preference_customers():
    """D4: language_pref 'hi' customers get Hindi-dominant copy — >= 6 distinct
    Hindi function words, no 'saved address', no bare English 'order details'."""
    from vera.fallbacks.compose import compose_fallback
    from vera.signals.derived import derive_facts
    from vera.signals.relevance import attach_conditionals
    from vera.voice import resolve_voice

    merchant = json.loads((ROOT / "expanded/merchants/m_009_apollo_pharmacy_jaipur.json")
                          .read_text(encoding="utf-8"))
    customer = json.loads((ROOT / "expanded/customers/c_013_grandfather_for_m009.json")
                          .read_text(encoding="utf-8"))
    category = json.loads((ROOT / "dataset/categories/pharmacies.json").read_text(encoding="utf-8"))
    trigger = next(t for t in json.loads(
        (ROOT / "dataset/triggers_seed.json").read_text(encoding="utf-8"))["triggers"]
        if t["id"] == "trg_019_chronic_refill_grandfather")
    now = datetime(2026, 4, 26, 10, 30, 0)
    facts = attach_conditionals(derive_facts(category, merchant, trigger, customer, now),
                                category=category, merchant=merchant, trigger=trigger,
                                customer=customer, now=now)
    voice = resolve_voice(category, merchant, customer, trigger)
    assert voice.lang == "hi"
    message = compose_fallback("chronic_refill_due", category=category, merchant=merchant,
                               trigger=trigger, customer=customer, facts=facts,
                               voice=voice, now=now)
    body = message.body.lower()
    function_words = {"hai", "hain", "ki", "ka", "ke", "ko", "par", "tak", "se",
                      "mein", "liye", "kijiye", "dijiye", "bata", "ho", "gaya",
                      "gayi", "chahiye", "ready", "dun", "bhej"}
    distinct = {w.strip(".,!?—:;\"'()") for w in body.split()}
    markers = {w for w in function_words if w in distinct}
    assert len(markers) >= 6, f"hi body lacks Hindi dominance: {sorted(markers)}"
    assert "saved address" not in body
    assert "registered address" in body or "address par" in body
    assert "order details" not in body


def test_sweep_output_bodies_have_no_machine_glyphs():
    now = datetime(2026, 4, 26, 10, 30, 0)
    run_sweep(now, OUT_PATH)
    lines = OUT_PATH.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 100
    for line in lines:
        record = json.loads(line)
        assert not _MACHINE_GLYPHS.search(record["body"]), \
            f"machine glyph in body for {record['trigger_id']}: {record['body'][:120]}"


_HINGLISH_FUNCTION_WORDS = (
    "aapke", "aapki", "aapka", "hai", "hain", "kya", "bhej", "dun", "denge",
    "liye", "mein", "se", "par", "tak", "ho", "gaya", "gayi", "rakha", "ready",
    "bata", "chalega", "karein",
)
# half-translated constructions the production transcripts never use
_BANNED_HINGLISH = (
    "peer average se peeche hai",
    "peer average se peeche hain",
)


def test_hinglish_and_english_language_lint():
    """Defect E3/E4: hi_en bodies carry real Hinglish function words (>= 3
    distinct); en bodies carry none."""
    import sys
    from pathlib import Path as P

    sys.path.insert(0, str(P(__file__).resolve().parents[1]))
    from harness.sweep import run_sweep as _rs  # noqa: F401  (path already set via conftest)

    now = datetime(2026, 4, 26, 10, 30, 0)
    run_sweep(now, OUT_PATH)
    records = [json.loads(line) for line in OUT_PATH.read_text(encoding="utf-8").splitlines()]
    hi_en = [r for r in records if r["lang"] == "hi_en"]
    en = [r for r in records if r["lang"] == "en"]
    assert hi_en and en, "sweep must cover both languages"

    for record in hi_en:
        words = set(record["body"].lower().split())
        distinct = {w.strip(".,!?—:;\"'()") for w in record["body"].lower().split()}
        markers = {m for m in _HINGLISH_FUNCTION_WORDS
                   if any(m == t or (t.startswith(m) and len(t) <= len(m) + 2) for t in distinct)}
        assert len(markers) >= 3, \
            f"{record['trigger_id']}: hi_en body lacks Hinglish function words " \
            f"({sorted(markers)}): {record['body'][:140]}"
        low = record["body"].lower()
        for banned in _BANNED_HINGLISH:
            assert banned not in low, \
                f"{record['trigger_id']}: half-translated construction '{banned}'"

    hindi_markers_en = ("nahi", "kya", "bhej", "dijiye", "bataiye", "kijiye",
                        "chahiye", "ghante", "jo slot")
    for record in en:
        low = record["body"].lower()
        for marker in hindi_markers_en:
            assert marker not in low, \
                f"{record['trigger_id']}: en body contains Hindi '{marker}': {record['body'][:140]}"


def test_placeholder_canonical_pairs_render_with_three_numerals():
    """The 13 placeholder-payload canonical pairs (43% of the like-for-like
    comparison set) must each render with >= 3 grounded numerals."""
    from vera.fallbacks.compose import compose_fallback
    from vera.signals.derived import derive_facts
    from vera.signals.relevance import attach_conditionals
    from vera.grounding.whitelist import extract_numerals, is_allowed_constant, \
        build_whitelist, numeral_variants
    from vera.voice import resolve_voice
    from datetime import timedelta
    import datetime as dt

    pairs = json.loads((ROOT / "expanded" / "test_pairs.json").read_text(encoding="utf-8"))["pairs"]
    triggers = {}
    for path in (ROOT / "expanded" / "triggers").glob("*.json"):
        t = json.loads(path.read_text(encoding="utf-8"))
        triggers[t["id"]] = t
    merchants = {}
    for path in (ROOT / "expanded" / "merchants").glob("*.json"):
        m = json.loads(path.read_text(encoding="utf-8"))
        merchants[m["merchant_id"]] = m
    categories = {p.stem: json.loads(p.read_text(encoding="utf-8"))
                  for p in (ROOT / "expanded" / "categories").glob("*.json")}

    now = datetime(2026, 4, 26, 10, 30, 0)
    checked = 0
    for pair in pairs:
        trigger = triggers[pair["trigger_id"]]
        if not trigger["payload"].get("placeholder"):
            continue
        merchant = merchants[trigger["merchant_id"]]
        category = categories[merchant["category_slug"]]
        customer = None
        facts = attach_conditionals(
            derive_facts(category, merchant, trigger, None, now),
            category=category, merchant=merchant, trigger=trigger,
            customer=None, now=now)
        voice = resolve_voice(category, merchant, None, trigger)
        message = compose_fallback(trigger["kind"], category=category, merchant=merchant,
                                   trigger=trigger, customer=None, facts=facts,
                                   voice=voice, now=now)
        whitelist = build_whitelist(facts, voice)
        tier1 = {token for token in extract_numerals(message.body)
                 if numeral_variants(token) & whitelist
                 and not is_allowed_constant(message.body, token)}
        checked += 1
        assert len(tier1) >= 3, \
            f"{pair['test_id']} ({trigger['kind']}) has only {len(tier1)} grounded " \
            f"numerals: {message.body}"
    assert checked == 13, f"expected the 13 placeholder canonical pairs, got {checked}"
