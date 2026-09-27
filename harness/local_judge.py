"""Local Judge scoring instrument for the magicpin AI Challenge.

Faithfully reproduces the shipped judge's LLMScorer context, system prompt,
and scoring contract, with dual strict/enriched context evaluation, fail-loud
exception handling, SQLite caching, and token-bucket rate limiting.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from harness.providers import FakeProvider, LLMProvider, get_provider

logger = logging.getLogger("harness.local_judge")

# =============================================================================
# GROUND TRUTH SCORING SYSTEM PROMPT
# Shipped verbatim from judge_simulator.py LLMScorer.SYSTEM.
# MUST REMAIN BYTE-IDENTICAL TO THE SHIPPED FILE.
# =============================================================================
SYSTEM_PROMPT = """You are a STRICT judge for the magicpin AI Challenge. You score merchant engagement messages.

SCORING DIMENSIONS (0-10 each, be strict - 5 is average, 7+ is good, 9+ is excellent):

1. SPECIFICITY: Does the message have VERIFIABLE facts?
   - Numbers (percentages, counts, prices)
   - Dates/times
   - Source citations
   - Concrete claims vs vague statements

2. CATEGORY FIT: Does the voice match the business type?
   - Dentists: clinical, peer-to-peer, technical OK, use "Dr." prefix
   - Salons: warm, friendly, practical
   - Restaurants: operator-to-operator
   - Gyms: coaching, motivational
   - Pharmacies: trustworthy, precise

3. MERCHANT FIT: Is it personalized to THIS merchant?
   - Uses their name/owner name correctly
   - References their actual data (not fabricated)
   - Honors language preference

4. TRIGGER RELEVANCE: Does it connect to WHY NOW?
   - Clear reason for this specific message
   - Uses data from the trigger payload
   - Not a generic nudge

5. ENGAGEMENT COMPULSION: Would they reply?
   - Loss aversion, curiosity, social proof
   - Clear CTA
   - Low friction ask

PENALTIES:
- Fabricating data not in context: -2
- Exposing internal jargon to merchant: -1

RESPOND ONLY WITH THIS EXACT JSON FORMAT:
{
  "specificity": <0-10>,
  "specificity_reason": "<why this score, 1-2 sentences>",
  "category_fit": <0-10>,
  "category_fit_reason": "<why this score>",
  "merchant_fit": <0-10>,
  "merchant_fit_reason": "<why this score>",
  "decision_quality": <0-10>,
  "decision_quality_reason": "<why this score>",
  "engagement_compulsion": <0-10>,
  "engagement_reason": "<why this score>",
  "hint": "<one sentence guidance for improvement, cryptic not direct>"
}"""


class JudgeError(Exception):
    """Raised when an LLM judge call fails or produces invalid score output.

    Never substitute a heuristic fallback score. Fail loudly.
    """
    def __init__(self, provider: str, model: str, cause: str):
        super().__init__(f"Judge failed for provider='{provider}' model='{model}': {cause}")
        self.provider = provider
        self.model = model
        self.cause = cause


@dataclass
class ScoreResult:
    """Output contract matching shipped LLMScorer scorecard."""
    specificity: int = 0
    specificity_reason: str = ""
    category_fit: int = 0
    category_fit_reason: str = ""
    merchant_fit: int = 0
    merchant_fit_reason: str = ""
    decision_quality: int = 0
    decision_quality_reason: str = ""
    engagement_compulsion: int = 0
    engagement_reason: str = ""
    penalties: int = 0
    penalty_reasons: List[str] = field(default_factory=list)
    hint: str = ""

    @property
    def total(self) -> int:
        raw_sum = (
            self.specificity
            + self.category_fit
            + self.merchant_fit
            + self.decision_quality
            + self.engagement_compulsion
            - self.penalties
        )
        return max(0, raw_sum)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["total"] = self.total
        return d

    def __getitem__(self, item: str) -> Any:
        return self.to_dict()[item]

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)


# =============================================================================
# CONTEXT BUILDERS
# =============================================================================

def _to_dict(obj: Any) -> Dict[str, Any]:
    """Helper to convert objects or dicts into plain dictionaries."""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "to_dict") and callable(obj.to_dict):
        return obj.to_dict()
    if hasattr(obj, "__dict__"):
        return vars(obj)
    return {}


def build_strict_context(category: Any, merchant: Any, trigger: Any, customer: Any = None) -> str:
    """Reproduces the shipped LLMScorer.score() prompt EXACTLY:

    category slug, voice.tone, voice.vocab_taboo[:5],
    merchant identity.name / owner_first_name / locality / languages,
    performance views/calls/ctr, signals, active offer titles only,
    trigger kind + full payload JSON + urgency, and
    customer identity only (or "None (merchant-facing)"). Nothing else.
    """
    cat = _to_dict(category)
    m = _to_dict(merchant)
    trg = _to_dict(trigger)
    cust = _to_dict(customer) if customer else None

    slug = cat.get("slug", "unknown")
    tone = cat.get("voice", {}).get("tone", "unknown") if isinstance(cat.get("voice"), dict) else getattr(cat.get("voice"), "tone", "unknown")
    vocab_taboo = cat.get("voice", {}).get("vocab_taboo", []) if isinstance(cat.get("voice"), dict) else getattr(cat.get("voice"), "vocab_taboo", [])
    taboos = vocab_taboo[:5]

    m_id = m.get("identity", {})
    name = m_id.get("name", "unknown")
    owner = m_id.get("owner_first_name", "unknown")
    locality = m_id.get("locality", "unknown")
    languages = m_id.get("languages", [])

    perf = m.get("performance", {})
    views = perf.get("views", "?")
    calls = perf.get("calls", "?")
    ctr = perf.get("ctr", "?")

    signals = m.get("signals", [])
    active_offers = [o.get("title") for o in m.get("offers", []) if isinstance(o, dict) and o.get("status") == "active"]

    t_kind = trg.get("kind", "unknown")
    t_payload = json.dumps(trg.get("payload", {}))
    t_urgency = trg.get("urgency", "?")

    if cust:
        c_identity = json.dumps(cust.get("identity", {}))
    else:
        c_identity = "None (merchant-facing)"

    return f"""=== CONTEXT PROVIDED TO BOT ===
Category: {slug}
Voice: {tone}
Taboos: {taboos}

Merchant: {name}
Owner: {owner}
Locality: {locality}
Languages: {languages}
Performance: views={views}, calls={calls}, ctr={ctr}
Signals: {signals}
Active Offers: {active_offers}

Trigger Kind: {t_kind}
Trigger Payload: {t_payload}
Trigger Urgency: {t_urgency}

Customer: {c_identity}"""


def build_enriched_context(category: Any, merchant: Any, trigger: Any, customer: Any = None) -> str:
    """The strict block PLUS:

    category peer_stats, digest (full items with source), offer_catalog,
    seasonal_beats, trend_signals, patient_content_library;
    merchant subscription, customer_aggregate, review_themes, conversation_history,
    performance.delta_7d, directions, leads, established_year, city, verified;
    and the full CustomerContext (relationship, state, preferences, consent).
    """
    strict_block = build_strict_context(category, merchant, trigger, customer)
    cat = _to_dict(category)
    m = _to_dict(merchant)
    cust = _to_dict(customer) if customer else None

    # Category enriched fields
    peer_stats = json.dumps(cat.get("peer_stats", {}))
    digest = json.dumps(cat.get("digest", []))
    offer_catalog = json.dumps(cat.get("offer_catalog", []))
    seasonal_beats = json.dumps(cat.get("seasonal_beats", []))
    trend_signals = json.dumps(cat.get("trend_signals", []))
    patient_content_library = json.dumps(cat.get("patient_content_library", []))

    # Merchant enriched fields
    m_id = m.get("identity", {})
    established_year = m_id.get("established_year", "?")
    city = m_id.get("city", "?")
    verified = m_id.get("verified", "?")

    m_perf = m.get("performance", {})
    delta_7d = json.dumps(m_perf.get("delta_7d", {}))
    directions = m_perf.get("directions", "?")
    leads = m_perf.get("leads", "?")

    subscription = json.dumps(m.get("subscription", {}))
    customer_aggregate = json.dumps(m.get("customer_aggregate", {}))
    review_themes = json.dumps(m.get("review_themes", []))
    conversation_history = json.dumps(m.get("conversation_history", []))

    # Customer full context
    if cust:
        full_cust_dict = {
            "identity": cust.get("identity", {}),
            "relationship": cust.get("relationship", {}),
            "state": cust.get("state", {}),
            "preferences": cust.get("preferences", {}),
            "consent": cust.get("consent", {}),
        }
        customer_full = json.dumps(full_cust_dict)
    else:
        customer_full = "None (merchant-facing)"

    enriched_block = f"""
=== ENRICHED CONTEXT (SUPERIOR GROUNDING) ===
Category Peer Stats: {peer_stats}
Category Digest: {digest}
Category Offer Catalog: {offer_catalog}
Category Seasonal Beats: {seasonal_beats}
Category Trend Signals: {trend_signals}
Category Patient Content Library: {patient_content_library}

Merchant Subscription: {subscription}
Merchant Customer Aggregate: {customer_aggregate}
Merchant Review Themes: {review_themes}
Merchant Conversation History: {conversation_history}
Merchant Performance Delta 7d: {delta_7d}
Merchant Directions: {directions}
Merchant Leads: {leads}
Merchant Established Year: {established_year}
Merchant City: {city}
Merchant Verified: {verified}

Full Customer Context: {customer_full}"""

    return strict_block + enriched_block


# =============================================================================
# RATE LIMITER & CACHE
# =============================================================================

class TokenBucketRateLimiter:
    """Token-bucket rate limiter with configurable RPM."""

    def __init__(self, rpm: float = 8.0):
        self.rpm = float(rpm)
        self.rate = self.rpm / 60.0  # tokens per second
        self.capacity = 1.0
        self.tokens = self.capacity
        self.last_check = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """Block until 1 token is available."""
        with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self.last_check
                self.last_check = now
                self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                needed = 1.0 - self.tokens
                sleep_sec = max(0.001, needed / self.rate)
                time.sleep(sleep_sec)


class JudgeCache:
    """On-disk SQLite response cache for scorecards."""

    def __init__(self, cache_dir: Optional[Path] = None):
        if cache_dir is None:
            cache_dir = Path(__file__).parent / "out" / "judge_cache"
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.cache_dir / "cache.sqlite3"
        self.hits = 0
        self.misses = 0
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS score_cache (
                    cache_key TEXT PRIMARY KEY,
                    result_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.commit()

    @staticmethod
    def compute_key(
        model: str,
        context_variant: str,
        system_prompt: str,
        body: str,
        cta: str,
        send_as: str,
        context_json: str,
    ) -> str:
        raw = f"{model}:{context_variant}:{system_prompt}:{body}:{cta}:{send_as}:{context_json}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def get(self, key: str) -> Optional[ScoreResult]:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT result_json FROM score_cache WHERE cache_key = ?", (key,))
            row = cursor.fetchone()
            if row:
                self.hits += 1
                data = json.loads(row[0])
                return ScoreResult(
                    specificity=data.get("specificity", 0),
                    specificity_reason=data.get("specificity_reason", ""),
                    category_fit=data.get("category_fit", 0),
                    category_fit_reason=data.get("category_fit_reason", ""),
                    merchant_fit=data.get("merchant_fit", 0),
                    merchant_fit_reason=data.get("merchant_fit_reason", ""),
                    decision_quality=data.get("decision_quality", 0),
                    decision_quality_reason=data.get("decision_quality_reason", ""),
                    engagement_compulsion=data.get("engagement_compulsion", 0),
                    engagement_reason=data.get("engagement_reason", ""),
                    penalties=data.get("penalties", 0),
                    penalty_reasons=data.get("penalty_reasons", []),
                    hint=data.get("hint", ""),
                )
            self.misses += 1
            return None

    def put(self, key: str, result: ScoreResult) -> None:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO score_cache (cache_key, result_json, created_at) VALUES (?, ?, ?)",
                (key, json.dumps(result.to_dict()), time.time()),
            )
            conn.commit()

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return (self.hits / total) if total > 0 else 0.0


# Module-level default cache and rate limiter
_DEFAULT_CACHE = JudgeCache()
_DEFAULT_LIMITER = TokenBucketRateLimiter(rpm=8.0)


# =============================================================================
# SCORING ENGINE
# =============================================================================

def parse_score_response(
    response: str,
    provider_name: str,
    model_name: str,
) -> ScoreResult:
    """Parse JSON response from LLM judge and enforce 0-10 int clamping and failure modes."""
    match = re.search(r"\{[\s\S]*\}", response)
    if not match:
        raise JudgeError(
            provider=provider_name,
            model=model_name,
            cause="Judge response did not contain a JSON object",
        )

    try:
        data = json.loads(match.group())
    except Exception as e:
        raise JudgeError(
            provider=provider_name,
            model=model_name,
            cause=f"Judge response was not valid JSON: {e}",
        ) from e

    required_fields = (
        "specificity",
        "category_fit",
        "merchant_fit",
        "engagement_compulsion",
    )
    for rf in required_fields:
        if rf not in data:
            raise JudgeError(
                provider=provider_name,
                model=model_name,
                cause=f"Judge response missing required dimension '{rf}'",
            )

    try:
        specificity = min(10, max(0, int(data.get("specificity", 0))))
        category_fit = min(10, max(0, int(data.get("category_fit", 0))))
        merchant_fit = min(10, max(0, int(data.get("merchant_fit", 0))))

        # Fallback to trigger_relevance if decision_quality is omitted
        dq_val = data.get("decision_quality", data.get("trigger_relevance", 0))
        decision_quality = min(10, max(0, int(dq_val)))

        engagement_compulsion = min(10, max(0, int(data.get("engagement_compulsion", 0))))
        penalties = max(0, int(data.get("penalties", 0)))
    except (ValueError, TypeError) as e:
        raise JudgeError(
            provider=provider_name,
            model=model_name,
            cause=f"Invalid score integer value in response: {e}",
        ) from e

    # Fail loudly on all-zero dimensions
    if (
        specificity == 0
        and category_fit == 0
        and merchant_fit == 0
        and decision_quality == 0
        and engagement_compulsion == 0
    ):
        raise JudgeError(
            provider=provider_name,
            model=model_name,
            cause="Judge returned all-zero dimensions (invalid response)",
        )

    penalty_reasons = data.get("penalty_reasons", [])
    if isinstance(penalty_reasons, str):
        penalty_reasons = [penalty_reasons]
    elif not isinstance(penalty_reasons, list):
        penalty_reasons = []

    return ScoreResult(
        specificity=specificity,
        specificity_reason=str(data.get("specificity_reason", "")),
        category_fit=category_fit,
        category_fit_reason=str(data.get("category_fit_reason", "")),
        merchant_fit=merchant_fit,
        merchant_fit_reason=str(data.get("merchant_fit_reason", "")),
        decision_quality=decision_quality,
        decision_quality_reason=str(data.get("decision_quality_reason", data.get("trigger_relevance_reason", ""))),
        engagement_compulsion=engagement_compulsion,
        engagement_reason=str(data.get("engagement_reason", "")),
        penalties=penalties,
        penalty_reasons=penalty_reasons,
        hint=str(data.get("hint", "")),
    )


def score(
    action: Dict[str, Any],
    category: Any,
    merchant: Any,
    trigger: Any,
    customer: Any = None,
    judge: Optional[Union[str, LLMProvider]] = None,
    context_variant: str = "strict",
    cache: Optional[JudgeCache] = None,
    limiter: Optional[TokenBucketRateLimiter] = None,
) -> ScoreResult:
    """Score a single bot action under either strict or enriched context."""
    if judge is None:
        judge_instance = get_provider()
    elif isinstance(judge, str):
        judge_instance = get_provider(judge)
    elif isinstance(judge, LLMProvider):
        judge_instance = judge
    else:
        raise ValueError(f"Invalid judge argument: {judge}")

    body = action.get("body", "")
    cta = action.get("cta", "none")
    send_as = action.get("send_as", "vera")

    if context_variant == "enriched":
        context_str = build_enriched_context(category, merchant, trigger, customer)
    else:
        context_str = build_strict_context(category, merchant, trigger, customer)

    # Cache lookup
    active_cache = cache or _DEFAULT_CACHE
    context_json = json.dumps({
        "category": _to_dict(category).get("slug"),
        "merchant": _to_dict(merchant).get("merchant_id"),
        "trigger": _to_dict(trigger).get("id", _to_dict(trigger).get("kind")),
        "customer": _to_dict(customer).get("customer_id") if customer else None,
        "variant": context_variant,
    }, sort_keys=True)

    cache_key = active_cache.compute_key(
        model=judge_instance.name(),
        context_variant=context_variant,
        system_prompt=SYSTEM_PROMPT,
        body=body,
        cta=cta,
        send_as=send_as,
        context_json=context_json,
    )

    cached_res = active_cache.get(cache_key)
    if cached_res is not None:
        return cached_res

    # Assemble prompt
    prompt = f"""SCORE THIS MESSAGE:

{context_str}

=== BOT'S MESSAGE ===
Body ({len(body)} chars): "{body}"
CTA: {cta}
Send As: {send_as}

Score each dimension 0-10 with clear reasoning. Be STRICT."""

    # Rate limiting
    active_limiter = limiter or _DEFAULT_LIMITER
    active_limiter.acquire()

    # Call LLM
    try:
        response_text = judge_instance.complete(prompt, system=SYSTEM_PROMPT)
    except Exception as e:
        raise JudgeError(
            provider=judge_instance.provider_name,
            model=judge_instance.model,
            cause=f"Provider call raised: {e}",
        ) from e

    result = parse_score_response(
        response=response_text,
        provider_name=judge_instance.provider_name,
        model_name=judge_instance.model,
    )

    # Store in cache
    active_cache.put(cache_key, result)
    return result


# =============================================================================
# CLI & DRY-RUN
# =============================================================================

def _find_json_file(directory: Path, identifier: str) -> Optional[Path]:
    """Find JSON file matching identifier by exact stem or prefix."""
    if not directory.exists():
        return None
    exact = directory / f"{identifier}.json"
    if exact.exists():
        return exact
    for p in directory.glob(f"*{identifier}*.json"):
        return p
    return None


def run_dry_run(merchant_id: str, trigger_id: str, customer_id: Optional[str] = None) -> Tuple[str, str]:
    """Builds and returns both strict and enriched context blocks without network calls."""
    root_dir = Path(__file__).parent.parent
    expanded_dir = root_dir / "expanded"

    # 1. Load Merchant
    m_file = _find_json_file(expanded_dir / "merchants", merchant_id)
    if not m_file:
        raise FileNotFoundError(f"Merchant '{merchant_id}' not found in expanded/merchants/")
    with open(m_file, "r", encoding="utf-8") as f:
        merchant = json.load(f)

    # 2. Load Category
    cat_slug = merchant.get("category_slug", "dentists")
    cat_file = _find_json_file(expanded_dir / "categories", cat_slug)
    if not cat_file:
        raise FileNotFoundError(f"Category '{cat_slug}' not found in expanded/categories/")
    with open(cat_file, "r", encoding="utf-8") as f:
        category = json.load(f)

    # 3. Load Trigger
    trg_file = _find_json_file(expanded_dir / "triggers", trigger_id)
    if not trg_file:
        raise FileNotFoundError(f"Trigger '{trigger_id}' not found in expanded/triggers/")
    with open(trg_file, "r", encoding="utf-8") as f:
        trigger = json.load(f)

    # 4. Load Customer (if specified or referenced)
    customer = None
    if customer_id:
        c_file = _find_json_file(expanded_dir / "customers", customer_id)
        if c_file:
            with open(c_file, "r", encoding="utf-8") as f:
                customer = json.load(f)

    strict_ctx = build_strict_context(category, merchant, trigger, customer)
    enriched_ctx = build_enriched_context(category, merchant, trigger, customer)

    return strict_ctx, enriched_ctx


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    parser = argparse.ArgumentParser(description="Local Judge for magicpin AI Challenge")
    parser.add_argument("--dry-run", action="store_true", help="Print strict and enriched context blocks without LLM call")
    parser.add_argument("--merchant", type=str, required=True, help="Merchant ID (e.g. m_001_drmeera_dentist_delhi)")
    parser.add_argument("--trigger", type=str, required=True, help="Trigger ID (e.g. trg_001_research_digest_dentists)")
    parser.add_argument("--customer", type=str, default=None, help="Customer ID (optional)")

    args = parser.parse_args()

    if args.dry_run:
        strict_block, enriched_block = run_dry_run(args.merchant, args.trigger, args.customer)
        print("\n" + "=" * 70)
        print("STRICT CONTEXT BLOCK (OFFICIAL JUDGE MATCH)")
        print("=" * 70)
        print(strict_block)
        print("\n" + "=" * 70)
        print("ENRICHED CONTEXT BLOCK (SUPERIOR GROUNDING)")
        print("=" * 70)
        print(enriched_block)
        print("\n" + "=" * 70)
        print("DRY-RUN COMPLETE: Zero network calls made. No API key required.")
        print("=" * 70)


if __name__ == "__main__":
    main()
