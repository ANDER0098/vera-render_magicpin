"""Unit tests for harness/local_judge.py and harness/providers.py.

Uses FakeProvider with zero network calls and no API keys.
Verifies context isolation, clamping, total score calculation, fail-loud
exception handling, caching, and rate limiting.
"""

import json
import time
from pathlib import Path
import pytest

from harness.local_judge import (
    JudgeCache,
    JudgeError,
    ScoreResult,
    TokenBucketRateLimiter,
    build_enriched_context,
    build_strict_context,
    parse_score_response,
    score,
)
from harness.providers import FakeProvider


@pytest.fixture
def sample_data():
    category = {
        "slug": "dentists",
        "voice": {
            "tone": "peer_clinical",
            "vocab_taboo": ["guaranteed", "100% safe", "completely cure", "miracle", "best in city", "extra_taboo"],
        },
        "peer_stats": {"avg_rating": 4.5, "avg_views_30d": 1800},
        "digest": [{"id": "d1", "title": "Fluoride Study", "source": "JIDA"}],
        "offer_catalog": [{"id": "den_001", "title": "Cleaning @ 299"}],
    }
    merchant = {
        "merchant_id": "m_001",
        "identity": {
            "name": "Dr. Meera's Clinic",
            "owner_first_name": "Meera",
            "locality": "Lajpat Nagar",
            "languages": ["en", "hi"],
            "city": "Delhi",
            "verified": True,
        },
        "performance": {"views": 2410, "calls": 18, "ctr": 0.021, "delta_7d": {"views_pct": 0.1}},
        "signals": ["stale_posts:22d"],
        "offers": [{"title": "Dental Cleaning @ ₹299", "status": "active"}, {"title": "Expired", "status": "expired"}],
        "subscription": {"status": "active", "plan": "Pro"},
        "customer_aggregate": {"total_unique_ytd": 500},
    }
    trigger = {
        "id": "trg_001",
        "kind": "renewal_due",
        "payload": {"category": "dentists"},
        "urgency": 2,
    }
    customer = {
        "customer_id": "c_001",
        "identity": {"first_name": "Rahul", "phone": "+919876543210"},
        "relationship": {"status": "active"},
        "state": {"last_visit": "2026-01-10"},
        "preferences": {"preferred_channel": "whatsapp"},
        "consent": {"promotional": True},
    }
    action = {
        "body": "Dr. Meera, fluoride varnish trial shows 38% lower caries recurrence. Want the summary?",
        "cta": "binary_yes_no",
        "send_as": "vera",
    }
    return category, merchant, trigger, customer, action


def test_strict_context_contains_shipped_fields_and_no_enriched(sample_data):
    cat, merch, trg, cust, _ = sample_data
    strict_block = build_strict_context(cat, merch, trg, cust)

    # Shipped fields present
    assert "Category: dentists" in strict_block
    assert "Voice: peer_clinical" in strict_block
    assert "Merchant: Dr. Meera's Clinic" in strict_block
    assert "Owner: Meera" in strict_block
    assert "Locality: Lajpat Nagar" in strict_block
    assert "Languages: ['en', 'hi']" in strict_block
    assert "views=2410" in strict_block
    assert "calls=18" in strict_block
    assert "ctr=0.021" in strict_block
    assert "Signals: ['stale_posts:22d']" in strict_block
    assert "Active Offers: ['Dental Cleaning @ ₹299']" in strict_block
    assert "Expired" not in strict_block  # Only active offers
    assert "Trigger Kind: renewal_due" in strict_block
    assert "Trigger Urgency: 2" in strict_block

    # NONE of enriched fields
    assert "peer_stats" not in strict_block
    assert "digest" not in strict_block
    assert "offer_catalog" not in strict_block
    assert "customer_aggregate" not in strict_block
    assert "subscription" not in strict_block


def test_enriched_context_contains_peer_stats_and_digest(sample_data):
    cat, merch, trg, cust, _ = sample_data
    enriched_block = build_enriched_context(cat, merch, trg, cust)

    # Shipped strict fields still present
    assert "Category: dentists" in enriched_block
    assert "Merchant: Dr. Meera's Clinic" in enriched_block

    # Enriched fields present
    assert "Category Peer Stats:" in enriched_block
    assert "Fluoride Study" in enriched_block  # From digest
    assert "Category Digest:" in enriched_block
    assert "Merchant Subscription:" in enriched_block
    assert "Merchant Customer Aggregate:" in enriched_block


def test_clamping_ranges():
    # Input with out-of-range values: 14 -> 10, -3 -> 0, "7" -> 7
    json_response = json.dumps({
        "specificity": 14,
        "specificity_reason": "High",
        "category_fit": -3,
        "category_fit_reason": "Low",
        "merchant_fit": "7",
        "merchant_fit_reason": "String 7",
        "decision_quality": 8,
        "decision_quality_reason": "OK",
        "engagement_compulsion": 9,
        "engagement_reason": "Good",
        "penalties": 2,
        "penalty_reasons": ["Exposing jargon"],
        "hint": "Refine phrasing",
    })

    result = parse_score_response(json_response, provider_name="fake", model_name="fake-model")
    assert result.specificity == 10
    assert result.category_fit == 0
    assert result.merchant_fit == 7
    assert result.decision_quality == 8
    assert result.engagement_compulsion == 9
    assert result.penalties == 2


def test_total_calculation_with_penalties_and_flooring():
    # sum = 6 + 6 + 6 + 6 + 6 = 30. penalties = 5 -> total = 25
    r1 = ScoreResult(
        specificity=6, category_fit=6, merchant_fit=6,
        decision_quality=6, engagement_compulsion=6,
        penalties=5
    )
    assert r1.total == 25

    # Excessive penalties: sum = 4, penalties = 10 -> floored at 0
    r2 = ScoreResult(
        specificity=1, category_fit=1, merchant_fit=1,
        decision_quality=1, engagement_compulsion=0,
        penalties=10
    )
    assert r2.total == 0


def test_provider_exception_propagates_as_judge_error(sample_data, tmp_path):
    cat, merch, trg, cust, action = sample_data
    failing_provider = FakeProvider(raise_exception=ConnectionError("Upstream timeout"))
    cache = JudgeCache(cache_dir=tmp_path)

    with pytest.raises(JudgeError) as exc_info:
        score(
            action=action,
            category=cat,
            merchant=merch,
            trigger=trg,
            customer=cust,
            judge=failing_provider,
            cache=cache,
        )

    # Assert it raises JudgeError and does NOT return a numeric fallback score
    err = exc_info.value
    assert isinstance(err, JudgeError)
    assert err.provider == "fake"
    assert "Upstream timeout" in str(err)


def test_all_zero_scorecard_raises(sample_data):
    all_zero_json = json.dumps({
        "specificity": 0,
        "category_fit": 0,
        "merchant_fit": 0,
        "decision_quality": 0,
        "engagement_compulsion": 0,
        "hint": "Failed",
    })

    with pytest.raises(JudgeError) as exc_info:
        parse_score_response(all_zero_json, provider_name="fake", model_name="fake-model")
    assert "all-zero" in str(exc_info.value)


def test_cache_avoids_second_network_call(sample_data, tmp_path):
    cat, merch, trg, cust, action = sample_data
    valid_json = json.dumps({
        "specificity": 8,
        "specificity_reason": "Specific facts",
        "category_fit": 8,
        "category_fit_reason": "Matches tone",
        "merchant_fit": 7,
        "merchant_fit_reason": "Uses name",
        "decision_quality": 9,
        "decision_quality_reason": "Good recommendation",
        "engagement_compulsion": 7,
        "engagement_reason": "Clear ask",
        "hint": "None",
    })

    provider = FakeProvider(response_text=valid_json)
    cache = JudgeCache(cache_dir=tmp_path)

    # First call: cache miss, provider completes
    res1 = score(action, cat, merch, trg, cust, judge=provider, cache=cache)
    assert provider.call_count == 1
    assert res1.specificity == 8
    assert res1.total == 39

    # Reset call counter
    provider.call_count = 0

    # Second call: must hit cache, call counter stays 0
    res2 = score(action, cat, merch, trg, cust, judge=provider, cache=cache)
    assert provider.call_count == 0
    assert res2.specificity == 8
    assert res2.total == 39
    assert cache.hits >= 1
    assert cache.hit_rate > 0.0


def test_rate_limiter_delays():
    # 20 calls at rpm=600 takes under 5s (approx 1.9s)
    limiter_fast = TokenBucketRateLimiter(rpm=600.0)
    t0 = time.monotonic()
    for _ in range(20):
        limiter_fast.acquire()
    elapsed_fast = time.monotonic() - t0
    assert elapsed_fast < 5.0

    # At rpm=60 (1 token/sec), 12 calls takes > 10s (approx 11s)
    limiter_slow = TokenBucketRateLimiter(rpm=60.0)
    t1 = time.monotonic()
    for _ in range(12):
        limiter_slow.acquire()
    elapsed_slow = time.monotonic() - t1
    assert elapsed_slow >= 10.0
