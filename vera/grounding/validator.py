"""Grounding validator: hard/soft checks + deterministic repair.

This module is the bot's immune system: it decides what an LLM-composed (or
fallback-composed) message is *allowed* to say. Hard violations block the
send; soft violations warn and may be repaired.

Invariant: ``validate`` and ``repair`` are pure and deterministic. ``repair``
is idempotent and never introduces a numeral (it only deletes, strips or
reorders).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable

from ..signals.derived import Fact, permitted_intent
from ..text.normalize import ngrams, normalize_text
from ..voice import Voice
from .whitelist import (
    ALLOWED_CONSTANTS,
    build_whitelist,
    extract_numerals,
    is_allowed_constant,
    numeral_variants,
)

__all__ = ["Violation", "validate", "repair", "has_hard", "grounding_map"]


@dataclass(frozen=True)
class Violation:
    code: str       # e.g. "ungrounded_numeral"
    detail: str
    severity: str   # "hard" | "soft"


# ---------------------------------------------------------------------------
# patterns
# ---------------------------------------------------------------------------

_URL_EXPLICIT = re.compile(r"(?:https?://|www\.)", re.IGNORECASE)
_URL_BARE_DOMAIN = re.compile(
    r"\b[a-z0-9-]{2,}\.(?:com|net|org|co|in|io|biz|info|shop|site|online|app|dev|me|us|uk)\b",
    re.IGNORECASE,
)
_JARGON_WORDS = (
    "trigger", "payload", "suppression_key", "context_id", "merchant_id",
    "customer_id", "slug", "json", "schema", "signal", "digest item",
    "categorycontext", "tick", "endpoint", "llm",
)
_JARGON_RE = re.compile(
    r"\b(?:trigger|payload|suppression_key|context_id|merchant_id|customer_id|"
    r"slug|json|schema|signal|categorycontext|tick|endpoint|llm)\b|digest item",
    re.IGNORECASE,
)
# Defect A format-level leaks: machine glyphs, snake_case, 3+-part hyphen
# slugs, analyst labels, and analyst vocabulary.
_INTERNAL_GLYPHS = re.compile(r"[_<>=]")
_SNAKE_CASE = re.compile(r"\b[a-z]+_[a-z_]+\b")
_HYPHEN_SLUG = re.compile(r"\b[a-z]+(?:-[a-z0-9]+){2,}\b", re.IGNORECASE)
_ANALYST_LABEL = re.compile(r"\b(?:Context|Note|Insight|Data|Signal|Fact):", re.IGNORECASE)
_ANALYST_WORD = re.compile(
    r"\b(?:payload|benchmark|cohort|segment|metric|median|percentile|delta|"
    r"aggregate|snapshot|derived|whitelist|validator|frame|fallback|composer)\b",
    re.IGNORECASE,
)
# D1: id-shaped machine values (digest ids like d_2026W17_jida_fluoride)
_ID_SLUG = re.compile(r"\b[dcmt]_\d{0,4}[A-Za-z0-9_]*\b|\b[a-z]_[a-z0-9]{2,}\b", re.IGNORECASE)
# D2: causal connectives — licensed only via Diagnosis.licensed_claims
_CAUSAL_PHRASES = (
    "because", "isliye", "ki wajah se", "wajah se", "effect dikh raha",
    "asr dikh raha", "leading to", "causing", "caused", "so that's why",
    "means that", "is why", "result of", "due to", "ke karan", "kyunki",
)
# standalone "karan" is deliberately NOT checked — it is a common Indian
# first name; "ke karan" (the causal phrase) is checked instead
_CAUSAL_WORDS = re.compile(
    r"\b(?:because|isliye|causing|caused|matlab)\b", re.IGNORECASE,
)
# D6: trend/demand claims that require a source when one exists in context
_DEMAND_CLAIM = re.compile(
    r"\bdemand\b|yoY|\bsearches\b|\bshelves?\b", re.IGNORECASE,
)
_PREAMBLES = (
    "i hope you're doing well", "i hope you are doing well", "hope this message finds",
    "just reaching out", "i'm writing to", "i am writing to", "just checking in",
)
_PROMO_MARKERS = re.compile(
    r"\bflat\b|\bdeal\b|\bspecial offer\b|\bpromo\b|\blimited period\b|\bhurry\b|% off on",
    re.IGNORECASE,
)
# A price is promotional framing only when it is attached to offer/deal wording
# ("get the X @ Rs.99 offer") — quoting a catalog service price verbatim
# ("Bridal Trial @ Rs.999") in a consented context is not a promo.
_PRICE_PROMO = re.compile(
    r"\b(?:offer|deal|promo|package)\b[^.?!]*@\s?(?:Rs\.?|₹)\s?\d"
    r"|@\s?(?:Rs\.?|₹)\s?\d[^.?!]*\b(?:offer|deal|promo)\b",
    re.IGNORECASE,
)
_RESEARCH_MARKERS = re.compile(
    r"\bresearch\b|\bstudy\b|\bclinical trial\b|\bpatient trial\b|\bjournal\b|"
    r"jida|dci|cdsco|\bvoluntary recall\b|sub-potency|\bcompliance\b|"
    r"\bcircular\b|\bregulation\b|schedule h1|\bdose limit|\bbulletin\b",
    re.IGNORECASE,
)
_CITATION_TAIL = re.compile(r"\s*[—–]\s*[A-Z0-9][^.!?]*$")
_REPLY_TOKEN = re.compile(
    r"(?<![\w-])(?:reply|press|type)\s+(?:yes|no|stop|confirm|cancel|[1-9])\b",
    re.IGNORECASE,
)
_DASH_SEP = re.compile(r"\s[—–]\s")


def _split_citation(body: str) -> tuple[str, str]:
    """Split a trailing '— source' citation off the body.

    A citation tail never ends with sentence punctuation (it is a source
    string like 'JIDA Oct 2026, p.14' — internal dots are fine). Prose after a
    dash always ends with .!? so it is not mistaken for a citation.
    """
    last = None
    for match in _DASH_SEP.finditer(body):
        last = match
    if last is None:
        return body, ""
    tail = body[last.end():].strip()
    core = body[: last.start()].strip()
    if tail and len(tail) <= 100 and not tail.endswith((".", "?", "!")):
        return core, tail
    return body.strip(), ""
_QUESTION = "?"
_IMPERATIVE_ASKS = re.compile(
    r"\bwant me to\b|\bshall i\b|\bshould i\b|\blet me\b|\bsend me\b|\btell us\b|\bshare your\b",
    re.IGNORECASE,
)
_ALLOWED_EMOJI = frozenset("🦷💇✨💪🍕🍽💍👋🙏😊")

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _sentences(body: str) -> list[str]:
    return [s for s in _SENTENCE_SPLIT.split(body.strip()) if s.strip()]


def _ask_count(body: str) -> int:
    """Number of ask-bearing sentences (a '?' and its 'shall i' phrasing are
    ONE ask — count per sentence, not per marker)."""
    count = 0
    for sentence in _sentences(body):
        if (_QUESTION in sentence or _REPLY_TOKEN.search(sentence)
                or _IMPERATIVE_ASKS.search(sentence)):
            count += 1
    return count


def _reply_token_count(body: str) -> int:
    return len(_REPLY_TOKEN.findall(body))


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

def validate(body: str, cta: str, send_as: str, *, facts: list[Fact],
             voice: Voice, category: dict | None = None,
             merchant: dict | None = None, customer: dict | None = None,
             prior_bodies: Iterable[str] = (),
             case_corpus_ngrams: set | None = None,
             is_first_touch: bool = True,
             scope: str | None = None,
             trigger: dict | None = None,
             licensed_claims: Iterable[str] = ()) -> list[Violation]:
    """Run every hard and soft check. Returns the violation list (maybe empty)."""
    violations: list[Violation] = []

    # --- required fields / empty -------------------------------------------
    if not body or not body.strip():
        violations.append(Violation("empty_body", "body is blank", "hard"))
        violations.append(Violation("missing_required_field", "body missing", "hard"))
        return violations
    if not cta or not str(cta).strip():
        violations.append(Violation("missing_required_field", "cta missing", "hard"))
    if send_as not in ("vera", "merchant_on_behalf"):
        violations.append(Violation("missing_required_field", f"send_as invalid: {send_as!r}", "hard"))

    # --- numerals -----------------------------------------------------------
    whitelist = build_whitelist(facts, voice)
    if str(cta) == "multi_choice_slot":
        whitelist |= {str(i) for i in range(1, 10)}
    for token in extract_numerals(body):
        variants = numeral_variants(token)
        if variants & whitelist:
            continue
        if is_allowed_constant(body, token):
            continue
        violations.append(
            Violation("ungrounded_numeral", f"'{token}' has no backing fact", "hard")
        )

    # --- vocabulary / script / jargon ---------------------------------------
    for taboo in voice.vocab_taboo:
        if taboo and taboo.strip() and taboo.lower() in body.lower():
            violations.append(Violation("taboo_vocab", f"taboo string '{taboo}'", "hard"))
            break

    if _URL_EXPLICIT.search(body) or _URL_BARE_DOMAIN.search(body):
        violations.append(Violation("url_present", "URL or bare domain in body", "hard"))

    jargon_hits = sorted({m.group(0).lower() for m in _JARGON_RE.finditer(body)})
    if jargon_hits:
        violations.append(
            Violation("jargon_leak", "internal jargon: " + ", ".join(jargon_hits), "hard")
        )

    # --- internal enum / machine-format leakage (Defect A) --------------------
    # '— <source>' citations are verbatim dataset data (a source string may
    # itself contain 'aggregate', 'Google Trends', etc.) — leak checks scan
    # the copy with every known citation string removed
    leak_scan, _cit = _split_citation(body)
    for fact in facts:
        if fact.citation and fact.citation in leak_scan:
            leak_scan = leak_scan.replace(fact.citation, " ")
    leak_hits = _internal_vocab_hits(leak_scan)
    leak_hits += [f"glyph {ch!r}" for ch in sorted(set(_INTERNAL_GLYPHS.findall(leak_scan)))]
    snake = sorted({m.group(0) for m in _SNAKE_CASE.finditer(leak_scan)})
    if snake:
        leak_hits.append("snake_case: " + ", ".join(snake[:3]))
    slug = sorted({m.group(0) for m in _HYPHEN_SLUG.finditer(leak_scan)})
    if slug:
        leak_hits.append("hyphen slug: " + ", ".join(slug[:3]))
    labels = sorted({m.group(0) for m in _ANALYST_LABEL.finditer(leak_scan)})
    if labels:
        leak_hits.append("analyst label: " + ", ".join(labels[:3]))
    analyst = sorted({m.group(0).lower() for m in _ANALYST_WORD.finditer(leak_scan)})
    if analyst:
        leak_hits.append("analyst word: " + ", ".join(analyst[:3]))
    ids = sorted({m.group(0) for m in _ID_SLUG.finditer(leak_scan)})
    if ids:
        leak_hits.append("machine id: " + ", ".join(ids[:3]))
    if leak_hits:
        violations.append(
            Violation("internal_vocab_leak", "machine enum in copy: " + ", ".join(leak_hits[:4]),
                      "hard")
        )

    # --- D1: generic payload/preference enum net ------------------------------
    net_hits = _payload_net_hits(leak_scan, trigger, customer)
    if net_hits:
        violations.append(
            Violation("payload_enum_leak",
                      "raw payload/preference enum in copy: " + ", ".join(net_hits[:3]),
                      "hard")
        )

    # --- D2: causal claims must be licensed by the diagnosis ------------------
    causal = _unlicensed_causal(body, licensed_claims)
    if causal:
        violations.append(
            Violation("unsupported_causal_claim",
                      f"causal connective '{causal}' not licensed by the diagnosis",
                      "hard")
        )

    # --- D3: fact dump — many numerals from one source ------------------------
    dump = _fact_dump_path(body, facts, voice, str(cta), str(trigger.get("kind") or "") if trigger else "")
    if dump:
        violations.append(
            Violation("fact_dump",
                      f"{dump[1]} numerals all grounded to {dump[0]}",
                      "hard")
        )

    # --- D5: an offer's condition may be reworded but never dropped -----------
    condition = _dropped_offer_condition(body, facts)
    if condition:
        violations.append(
            Violation("offer_condition_dropped",
                      f"offer '{condition}' referenced without its threshold",
                      "hard")
        )

    bad_chars = _non_latin_chars(body)
    if bad_chars:
        violations.append(
            Violation("non_latin_script",
                      f"non-Latin characters: {', '.join(sorted(bad_chars)[:5])}", "hard")
        )

    if not is_first_touch and re.search(r"\bvera\b", body, re.IGNORECASE):
        violations.append(
            Violation("self_reintroduction", "mentions Vera after the first touch", "hard")
        )

    # --- CTA shape -----------------------------------------------------------
    multi_choice = str(cta) == "multi_choice_slot"
    asks = _ask_count(body)
    replies = _reply_token_count(body)
    if not multi_choice and asks > 1:
        violations.append(
            Violation("multiple_ctas", f"{asks} ask-bearing sentences", "hard")
        )
    if multi_choice and replies > 1:
        violations.append(Violation("multiple_ctas", f"{replies} reply tokens", "hard"))

    core_body, _citation_tail = _split_citation(body.strip())
    core_sentences = _sentences(core_body)
    last_sentence = core_sentences[-1] if core_sentences else ""
    has_ask = (_QUESTION in last_sentence or _REPLY_TOKEN.search(last_sentence)
               or _IMPERATIVE_ASKS.search(last_sentence))
    if str(cta) != "none" and not has_ask and core_body:
        violations.append(Violation("cta_not_last", "ask is not in the final sentence", "hard"))

    # --- repetition / plagiarism ---------------------------------------------
    normalized = normalize_text(body)
    for prior in prior_bodies:
        if prior and normalize_text(prior) == normalized:
            violations.append(Violation("repeat_body", "identical to a prior body", "hard"))
            break
    if case_corpus_ngrams:
        overlap = ngrams(body, 8) & set(case_corpus_ngrams)
        if overlap:
            sample = " ".join(sorted(overlap)[0])
            violations.append(
                Violation("plagiarism", f"8-gram overlap with case corpus: '{sample}...'", "hard")
            )

    # --- routing ---------------------------------------------------------------
    # scope may be supplied by the caller (trigger.scope); otherwise infer from
    # customer presence — a supplied customer means a customer-facing send.
    effective_scope = scope or ("customer" if customer is not None else "merchant")
    expected_send_as = "merchant_on_behalf" if effective_scope == "customer" else "vera"
    if send_as != expected_send_as:
        violations.append(
            Violation("send_as_mismatch",
                      f"scope '{effective_scope}' expects send_as '{expected_send_as}', got '{send_as}'",
                      "hard")
        )

    # --- consent ----------------------------------------------------------------
    if customer is not None and effective_scope == "customer":
        intent = permitted_intent(customer)
        if intent != "promotional" and (_PROMO_MARKERS.search(body) or _PRICE_PROMO.search(body)):
            violations.append(
                Violation("consent_violation",
                          f"promotional content but permitted intent is '{intent}'", "hard")
            )

    # --- soft checks --------------------------------------------------------------
    if len(body) > 480:
        violations.append(Violation("body_too_long", f"{len(body)} chars", "soft"))
    if len(body.strip()) < 60:
        violations.append(Violation("body_too_short", f"{len(body.strip())} chars", "soft"))
    lowered_start = body.strip().lower()
    if any(lowered_start.startswith(p) for p in _PREAMBLES):
        violations.append(Violation("no_preamble_check", "email-style preamble", "soft"))

    has_price_offer = any(
        f.kind == "offer" and re.search(r"\d", str(f.value) + f.phrase) for f in facts
    )
    if re.search(r"\b\d+\s?%\s?off\b", body, re.IGNORECASE) and has_price_offer:
        violations.append(
            Violation("pct_instead_of_service_price", "percent-off framing with a priced offer available", "soft")
        )

    if send_as == "vera" and voice.salutation:
        first_token = voice.salutation.split()[0] if voice.salutation.split() else ""
        if first_token.lower() not in body.lower():
            violations.append(Violation("no_owner_name", "merchant-facing body lacks the salutation", "soft"))

    if voice.lang != "en":
        marker_count = sum(1 for marker in ("namaste", "ji", "kya", "nahi", "hai", "apke", "aap",
                                            "kar", "ya", "chahiye", "ready", "hain") 
                           if marker in body.lower())
        if marker_count == 0:
            violations.append(
                Violation("language_mismatch", f"lang={voice.lang} but zero Hinglish markers", "soft")
            )

    tier1 = 0
    for token in extract_numerals(body):
        if numeral_variants(token) & whitelist and not is_allowed_constant(body, token):
            tier1 += 1
    payload = (trigger or {}).get("payload") or {}
    topic_tokens = {token for token in re.findall(
        r"[a-z]{4,}", str(payload.get("intent_topic") or "").replace("_", " ").lower())}
    rendered_topic_tokens = topic_tokens & set(re.findall(r"[a-z]{4,}", body.lower()))
    concrete_active_plan = bool(
        (trigger or {}).get("kind") == "active_planning_intent"
        and not payload.get("placeholder")
        and payload.get("intent_topic")
        and len(rendered_topic_tokens) >= min(2, len(topic_tokens))
        and re.search(r"\b(?:sample|draft|starter)\b", body, re.IGNORECASE)
    )
    if concrete_active_plan:
        # The requested topic inside a concrete sample is the relevant
        # specificity for this intent; unrelated metrics lower quality.
        pass
    elif tier1 <= 1:
        # a frame that cannot reach two grounded numerals is a bug, not a
        # data problem — every category is dense with numbers
        violations.append(Violation("weak_specificity", f"only {tier1} grounded numerals", "hard"))
    elif tier1 == 2:
        violations.append(Violation("weak_specificity", "only 2 grounded numerals", "soft"))

    if (_RESEARCH_MARKERS.search(body) or _demand_claim_present(body, facts)) \
            and not (_citation_tail or re.search(r"[—–]\s*\S", body)):
        violations.append(
            Violation("no_source_citation", "research/compliance/trend claim without a '— source' suffix", "soft")
        )

    # --- D5: digest quantification — trial_n and summary % render together ----
    quant_reported = False
    for fact in facts:
        if quant_reported:
            break
        if fact.key.endswith("trial_n") and isinstance(fact.value, (int, float)):
            trial_digits = re.sub(r"[^0-9]", "", str(int(fact.value)))
            base = fact.key[: -len("trial_n")]
            summary_stat = next((f for f in facts
                                 if f.key == base + "summary_stat"
                                 and isinstance(f.value, (int, float))), None)
            if summary_stat and str(int(summary_stat.value)) not in re.sub(r"[^0-9]", "", body) \
                    and trial_digits in re.sub(r"[^0-9]", "", body):
                violations.append(
                    Violation("digest_quantification",
                              f"digest renders its trial size but not its {int(summary_stat.value)}% finding",
                              "soft")
                )
                quant_reported = True

    return violations


def _non_latin_chars(body: str) -> set[str]:
    """Characters outside Latin letters/digits, common punctuation, ₹ and the
    allowed emoji set."""
    bad: set[str] = set()
    for ch in body:
        if ch.isascii():
            continue
        if ch in _ALLOWED_EMOJI or ch == "₹":
            continue
        category = _char_category(ch)
        if category in ("Lu", "Ll", "Lm", "Lo", "Nd"):
            # letters/digits must be Latin-1 range (excludes Devanagari etc.)
            if ord(ch) > 0x24F:
                bad.add(ch)
        elif category in ("Sc", "Sk", "Po", "Pd", "Ps", "Pe", "Pi", "Pf", "Pc", "Sm", "So", "Zs"):
            # currency/punct/symbols allowed within Latin-1 + typographic range
            if ord(ch) > 0x20BF:
                bad.add(ch)
    return bad


def _scan_normalize(text: str) -> str:
    """Lowercase and fold every non-alphanumeric run to a single space, so
    'metro-unisex-salons-2026' and 'metro_unisex_salons_2026' both scan as
    'metro unisex salons 2026'."""
    return re.sub(r"[^0-9a-z]+", " ", text.lower()).strip()


@lru_cache(maxsize=1)
def _blocked_vocab() -> frozenset[str]:
    from harness.internal_vocab import load_internal_vocab

    return load_internal_vocab()


def _internal_vocab_hits(body: str) -> list[str]:
    """All blocked internal-vocab surface forms present in *body*.

    Multi-word forms match as substrings of the scan-normalised body;
    single-word forms match on word boundaries (so 'pro' never fires inside
    'provider' — and 'pro' is sanctioned anyway).
    """
    vocab = _blocked_vocab()
    if not vocab:
        return []
    norm = _scan_normalize(body)
    norm_dense = re.sub(r"\s+", " ", norm)
    hits: list[str] = []
    for form in vocab:
        if " " in form:
            probe = form.replace("-", " ")
            if probe in norm_dense:
                hits.append(form)
        else:
            probe = form.replace("-", " ")
            if re.search(rf"\b{re.escape(probe)}\b", norm_dense):
                hits.append(form)
    return sorted(set(hits))


def _leak_hits_all(body: str) -> list[str]:
    """Vocabulary hits plus every format-level leak (glyphs, snake_case,
    hyphen slugs, analyst labels, analyst words, machine ids) — the complete
    Defect-A surface used by repair."""
    hits = _internal_vocab_hits(body)
    hits += [f"glyph {ch!r}" for ch in sorted(set(_INTERNAL_GLYPHS.findall(body)))]
    if _SNAKE_CASE.search(body):
        hits.append("snake_case")
    if _HYPHEN_SLUG.search(body):
        hits.append("hyphen slug")
    if _ANALYST_LABEL.search(body):
        hits.append("analyst label")
    analyst = {m.group(0).lower() for m in _ANALYST_WORD.finditer(body)}
    if analyst:
        hits.append("analyst word: " + ", ".join(sorted(analyst)[:3]))
    if _ID_SLUG.search(body):
        hits.append("machine id")
    return hits


def _payload_net_hits(body: str, trigger: dict | None,
                      customer: dict | None) -> list[str]:
    """D1 generic net: 2+-token surface variants of the trigger's payload
    strings (and customer preference values) present in the body."""
    from harness.internal_vocab import payload_net_variants

    if not body:
        return []
    norm = _scan_normalize(body)
    norm_dense = re.sub(r"\s+", " ", norm)
    hits: list[str] = []
    for variant in payload_net_variants(trigger, customer):
        if variant in norm_dense:
            hits.append(variant)
    return hits


def _unlicensed_causal(body: str, licensed_claims: Iterable[str]) -> str | None:
    """D2: the first causal connective in *body* that the diagnosis did not
    license. Phrases match as substrings of the scan-normalised body; single
    words match on word boundaries."""
    licensed = {str(c).lower().strip() for c in (licensed_claims or ())}
    norm_dense = re.sub(r"\s+", " ", _scan_normalize(body))
    for phrase in _CAUSAL_PHRASES:
        probe = _scan_normalize(phrase)
        if probe in norm_dense and phrase.lower() not in licensed:
            return phrase
    match = _CAUSAL_WORDS.search(body)
    if match and match.group(0).lower() not in licensed:
        return match.group(0).lower()
    return None


_DATE_TOKEN = re.compile(
    r"\d{4}-\d{2}-\d{2}|\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
    r"|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d{1,2}",
    re.IGNORECASE,
)


def _is_date_token(token: str) -> bool:
    return bool(_DATE_TOKEN.fullmatch(token.strip().rstrip(",.")))


def _fact_dump_path(body: str, facts: list[Fact], voice: Voice, cta: str,
                    trigger_kind: str) -> tuple[str, int] | None:
    """D3: more than 2 same-family numerals from ONE source list rendered in
    a single body — a fact dump. Checked directly against LIST-valued facts
    (payload trend lists etc.), independent of grounding attribution order.
    Date tokens are anchors, not dump material; molecule/slot list frames
    are exempt (the list IS the point of the message)."""
    if trigger_kind == "chronic_refill_due" or cta == "multi_choice_slot":
        return None
    body_digits = re.sub(r"[^0-9]", "", body)
    for fact in facts:
        if not isinstance(fact.value, (list, tuple)):
            continue
        rendered: set[str] = set()
        for item in fact.value:
            if not isinstance(item, str):
                continue
            for token in extract_numerals(item):
                if _is_date_token(token):
                    continue
                for variant in numeral_variants(token):
                    digits = re.sub(r"[^0-9]", "", variant)
                    if digits and digits in body_digits:
                        rendered.add(digits)
        if len(rendered) > 2:
            return fact.json_path, len(rendered)
    return None


def _dropped_offer_condition(body: str, facts: list[Fact]) -> str | None:
    """D5: an offer with a threshold/eligibility clause is referenced in the
    body without its threshold numbers. Rewording is fine; dropping is not.
    Referenced = the offer's threshold-free title phrase (or its price)
    appears in the body — shared single words like 'free' do not count."""
    norm_dense = re.sub(r"\s+", " ", _scan_normalize(body))
    body_digits = re.sub(r"[^0-9]", "", body)
    for fact in facts:
        if fact.kind != "offer":
            continue
        title = str(fact.value)
        thresholds = re.findall(
            r"(?:>|above|over|minimum|min|save|upto|up to)\s*(?:₹|Rs\.?)?\s*([\d,]+)",
            title, re.IGNORECASE) or re.findall(r">\s*(?:₹|Rs\.?)?\s*([\d,]+)", title)
        if not thresholds:
            continue
        title_sans = re.split(r"(?:>|above|over|minimum|min|save|upto|up to)\s*(?:₹|Rs\.?)?\s*[\d,]+",
                              title, flags=re.IGNORECASE)[0]
        title_norm = re.sub(r"\s+", " ", _scan_normalize(title_sans)).strip()
        price_digits = ""
        price_match = re.search(r"(?:₹|Rs\.?)\s?([\d,]+)", title)
        if price_match:
            price_digits = re.sub(r"[^0-9]", "", price_match.group(1))
        referenced = (len(title_norm) >= 6 and title_norm in norm_dense) \
            or (price_digits and price_digits in body_digits)
        if not referenced:
            continue
        for threshold in thresholds:
            digits = re.sub(r"[^0-9]", "", threshold)
            if digits and digits not in body_digits:
                return title
    return None


def _demand_claim_present(body: str, facts: list[Fact]) -> bool:
    """D6: the body asserts a market-trend/demand claim AND a sourced digest
    item exists in context — then a citation is required."""
    if not _DEMAND_CLAIM.search(body):
        return False
    return any(f.kind == "digest" and f.citation for f in facts)


def _char_category(ch: str) -> str:
    return unicodedata.category(ch)


# ---------------------------------------------------------------------------
# repair
# ---------------------------------------------------------------------------

_REPAIRABLE_HARD = {
    "url_present", "jargon_leak", "cta_not_last", "multiple_ctas",
    "taboo_vocab", "non_latin_script", "self_reintroduction",
    "ungrounded_numeral", "internal_vocab_leak", "weak_specificity",
}


def has_hard(violations: list[Violation]) -> bool:
    return any(v.severity == "hard" for v in violations)


def _strip_urls(body: str) -> str:
    body = _URL_EXPLICIT.sub("", body)
    body = _URL_BARE_DOMAIN.sub("", body)
    return re.sub(r"\s{2,}", " ", body).strip()


def _strip_bad_chars(body: str) -> str:
    bad = _non_latin_chars(body)
    for ch in bad:
        body = body.replace(ch, "")
    return re.sub(r"\s{2,}", " ", body).strip()


def repair(body: str, violations: list[Violation], facts: list[Fact],
           voice: Voice) -> str | None:
    """Attempt a minimal deterministic fix. Returns None when unrepairable.

    Idempotent: repairing a clean body returns it unchanged. Never introduces
    a numeral — only deletions, strippings and reorders.
    """
    if not body or not body.strip():
        return None
    codes = {v.code for v in violations}
    hard_codes = {v.code for v in violations if v.severity == "hard"}
    if hard_codes - _REPAIRABLE_HARD:
        return None
    if codes & {"repeat_body", "plagiarism", "empty_body", "missing_required_field",
                "send_as_mismatch", "consent_violation"}:
        return None

    working = body

    if "url_present" in codes:
        working = _strip_urls(working)
    if "non_latin_script" in codes:
        working = _strip_bad_chars(working)
    if "self_reintroduction" in codes:
        working = re.sub(r"[^.!?]*\bvera\b[^.!?]*[.!?]?\s*", "", working, flags=re.IGNORECASE).strip()

    if "jargon_leak" in codes:
        for match in reversed(list(_JARGON_RE.finditer(working))):
            start, end = match.span()
            replacement = ""
            if working[max(0, start - 1): start] == " " and end < len(working) and working[end] == " ":
                end += 1
            working = working[:start] + replacement + working[end:]
        working = re.sub(r"\s{2,}", " ", working).strip()

    if "taboo_vocab" in codes:
        for violation in violations:
            if violation.code == "taboo_vocab":
                taboo = violation.detail.split("'", 1)[1].rsplit("'", 1)[0]
                working = re.sub(re.escape(taboo), "", working, flags=re.IGNORECASE)
        working = re.sub(r"\s{2,}", " ", working).strip()

    if "ungrounded_numeral" in codes:
        whitelist = build_whitelist(facts, voice)
        offending: list[str] = []
        for token in extract_numerals(working):
            if not (numeral_variants(token) & whitelist) and not is_allowed_constant(working, token):
                offending.append(token)
        sentences = _sentences(working)
        kept: list[str] = []
        for sentence in sentences:
            tokens = extract_numerals(sentence)
            bad_here = [t for t in tokens if t in offending]
            is_last = sentence is sentences[-1]
            if bad_here and (is_last or len(sentences) - len(kept) <= 1):
                return None  # would need to drop the CTA — not repairable
            if bad_here:
                continue
            kept.append(sentence)
        if not kept:
            return None
        working = " ".join(kept)
        # restore a missing final punctuation
        if working and working[-1] not in ".!?":
            working += "."

    if "multiple_ctas" in codes:
        sentences = _sentences(working)
        ask_indexes = [
            i for i, s in enumerate(sentences)
            if _QUESTION in s or _REPLY_TOKEN.search(s) or _IMPERATIVE_ASKS.search(s)
        ]
        if len(ask_indexes) > 1:
            keep = ask_indexes[-1]
            fixed: list[str] = []
            for i, sentence in enumerate(sentences):
                if i in ask_indexes and i != keep:
                    sentence = sentence.replace("?", ".")
                    sentence = _IMPERATIVE_ASKS.sub("", sentence)
                    sentence = re.sub(r"\s{2,}", " ", sentence).strip()
                    if sentence and sentence[0].islower():
                        sentence = sentence[0].upper() + sentence[1:]
                fixed.append(sentence)
            working = " ".join(fixed)

    if "internal_vocab_leak" in codes:
        # first apply the safe rewrite map (which also strips glyphs, prices
        # like '> Rs.499' and month ranges), then drop any clause still leaking
        from harness.internal_vocab import rewrite_internal

        working = rewrite_internal(working)
        remaining = _leak_hits_all(working)
        if remaining:
            sentences = _sentences(working)
            kept = []
            for i, sentence in enumerate(sentences):
                is_last = i == len(sentences) - 1
                if _leak_hits_all(sentence) and not is_last:
                    continue
                kept.append(sentence)
            if kept:
                candidate = " ".join(kept)
                if not _leak_hits_all(candidate):
                    working = candidate
        if _leak_hits_all(working):
            return None

    if "cta_not_last" in codes:
        core, citation = _split_citation(working)
        sentences = _sentences(core)
        ask_indexes = [
            i for i, s in enumerate(sentences)
            if _QUESTION in s or _REPLY_TOKEN.search(s) or _IMPERATIVE_ASKS.search(s)
        ]
        if ask_indexes:
            keep = ask_indexes[-1]
            if keep != len(sentences) - 1:
                ask_sentence = sentences.pop(keep)
                sentences.append(ask_sentence)
                working = " ".join(sentences)
                if citation:
                    working = f"{working} — {citation}"

    if not working or not working.strip():
        return None
    return working


def grounding_map(body: str, facts: list[Fact], voice: Voice) -> list[str]:
    """Numeral -> json_path provenance lines for the rationale field.

    Attribution prefers the fact whose raw VALUE canonicalises to the numeral;
    phrase matches are the fallback (a phrase like '1,480 views in 30 days'
    mentions '30' without the numeral belonging to that fact).
    """
    whitelist = build_whitelist(facts, voice)
    value_index: dict[str, list[str]] = {}
    phrase_index: dict[str, list[str]] = {}
    for fact in facts:
        value_variants: set[str] = set()
        for source in _fact_value_strings(fact):
            for token in extract_numerals(str(source)):
                value_variants |= numeral_variants(token)
        for variant in value_variants:
            value_index.setdefault(variant, []).append(fact.json_path)
        for source in _fact_phrase_strings(fact):
            for token in extract_numerals(str(source)):
                for variant in numeral_variants(token):
                    if variant not in value_variants:
                        phrase_index.setdefault(variant, []).append(fact.json_path)
    lines: list[str] = []
    seen: set[str] = set()
    for token in extract_numerals(body):
        if token in seen:
            continue
        seen.add(token)
        variants = numeral_variants(token)
        paths: list[str] = []
        for variant in sorted(variants):
            for path in value_index.get(variant, []) + phrase_index.get(variant, []):
                if path not in paths:
                    paths.append(path)
        if not paths and is_allowed_constant(body, token):
            lines.append(f"numeral '{token}' <- allowed effort/CTA constant")
        elif paths:
            lines.append(f"numeral '{token}' <- {paths[0]}")
        else:
            lines.append(f"unmatched: {token}")
    return lines


def _fact_value_strings(fact: Fact) -> list[str]:
    sources: list[str] = []
    value = fact.value
    if isinstance(value, bool):
        return []
    if isinstance(value, (int, float)):
        sources.append(f"{value:g}")
        if isinstance(value, float) and 0 < abs(value) < 1:
            sources.append(f"{value * 100:g}")
    elif isinstance(value, str):
        sources.append(value)
    elif isinstance(value, (list, tuple, dict)):
        items = value if isinstance(value, (list, tuple)) else list(value.values())
        for item in items:
            if isinstance(item, (str, int, float)) and not isinstance(item, bool):
                sources.append(f"{item:g}" if isinstance(item, float) else str(item))
    if fact.citation:
        sources.append(fact.citation)
    return [s for s in sources if s]


def _fact_phrase_strings(fact: Fact) -> list[str]:
    return [fact.phrase] if fact.phrase else []


def _fact_source_values(fact: Fact) -> list[str]:
    sources: list[str] = []
    value = fact.value
    if isinstance(value, bool):
        return []
    if isinstance(value, (int, float)):
        sources.append(f"{value:g}")
        if isinstance(value, float) and 0 < abs(value) < 1:
            sources.append(f"{value * 100:g}")
    elif isinstance(value, str):
        sources.append(value)
    elif isinstance(value, (list, tuple, dict)):
        items = value if isinstance(value, (list, tuple)) else list(value.values())
        for item in items:
            if isinstance(item, (str, int, float)) and not isinstance(item, bool):
                sources.append(f"{item:g}" if isinstance(item, float) else str(item))
    if fact.phrase:
        sources.append(fact.phrase)
    if fact.citation:
        sources.append(fact.citation)
    return [s for s in sources if s]
