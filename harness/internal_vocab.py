"""Internal-vocabulary defence (Defect A): machine enums must never reach copy.

Two mechanisms, both data-driven:

1. ``build_internal_vocab(expanded_dir)`` scans ``expanded/`` and collects every
   machine-enum value from the leak surface (category slugs/voice/peer scope/
   offer audiences/types/digest kinds/month ranges, merchant subscription/
   signals/review keys/aggregate keys, customer states/preferences/consent/
   services/conditions/age bands, trigger kinds/scopes/sources) and expands
   each token to every surface form (raw, underscore→space, underscore→hyphen,
   title-cased, singular/plural for single-word tokens). Analyst words and
   analyst labels are included. A *sanctioned* set (words that appear verbatim
   inside merchant-facing offer titles or the category's ``vocab_allowed``,
   plus plan names and a small curated natural-language list) is subtracted,
   as are the rewrite targets below, so "3 FREE Trial Classes" or "the metro
   unisex-salon average" can never be flagged while "dentists" or
   "metro_unisex_salons_2026" always is.

2. ``REWRITE_MAP`` renders the known internal values as merchant language
   (``metro_unisex_salons_2026`` → "the metro unisex-salon average",
   ``> ₹499`` → "on orders above Rs.499", ``Nov-Feb`` → "November to February").
   ``rewrite_internal()`` applies it; ``repair`` uses it as the first fix for
   ``internal_vocab_leak``.

The vocabulary is derived from the data — the only authored lists here are the
rewrite map (merchant-facing renderings) and the natural-word exceptions.
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path

__all__ = [
    "build_internal_vocab",
    "load_internal_vocab",
    "rewrite_internal",
    "humanize_title",
    "REWRITE_MAP",
    "human_month_range",
]

# ---------------------------------------------------------------------------
# curated natural-language exceptions
# ---------------------------------------------------------------------------

# Single-word enum values that are ordinary English words in merchant-facing
# copy (or appear verbatim inside the merchant's own offer titles). They stay
# IN the vocabulary for coverage, but are not blocked.
_NATURAL_WORDS = frozenset({
    "new", "active", "all", "whatsapp", "research", "compliance", "seasonal",
    "alert", "supply", "tech", "compete", "cde", "internal", "external",
    "neg", "pos", "basic", "pro", "trial",
    "customer", "customers", "merchant", "merchants",
    "rising", "calls", "views",
    # calendar words are universal language, never internal enums
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
})

# Words sanctioned because they appear in the merchant's own voice packs.
_SANCTION_SOURCES = ("vocab_allowed",)

# Analyst labels that must never open a sentence in copy.
ANALYST_LABELS: tuple[str, ...] = ("Context:", "Note:", "Insight:", "Data:", "Signal:", "Fact:")

ANALYST_WORDS: frozenset[str] = frozenset({
    "payload", "benchmark", "cohort", "segment", "metric", "median",
    "percentile", "delta", "aggregate", "snapshot", "derived", "whitelist",
    "validator", "frame", "fallback", "composer",
})

# ---------------------------------------------------------------------------
# rewrite map: internal value -> merchant-facing rendering
# ---------------------------------------------------------------------------

_REWRITE_PAIRS: tuple[tuple[str, str], ...] = (
    # peer_stats.scope values
    ("metro_unisex_salons_2026", "the metro unisex-salon average"),
    ("metro_solo_practices_2026", "the metro solo-practice average"),
    ("metro_neighbourhood_gyms_2026", "the neighbourhood-gym average"),
    ("metro_neighbourhood_pharmacies_2026", "the neighbourhood-pharmacy average"),
    ("metro_casual_dining_2026", "the casual-dining average"),
    # bare category labels (slug used as an English noun)
    ("dentists", "dental"),
    ("salons", "salon"),
    ("gyms", "gym"),
    ("restaurants", "restaurant"),
    ("pharmacies", "pharmacy"),
    # customer states
    ("lapsed_soft", "it's been a while"),
    ("lapsed_hard", "it's been months"),
    ("churned", "we've not seen you in over a year"),
    # preferences
    ("weekday_evening", "weekday evenings"),
    ("whatsapp_via_son", ""),
    ("whatsapp_via_parent", ""),
    ("none_recorded", ""),
    # segments / focus / conditions
    ("high_risk_adults", "your high-risk adult patients"),
    ("weight_loss", "weight loss"),
    ("back_pain_management", "back pain"),
    ("chronic_rx_metformin", "metformin"),
    ("chronic_rx_atorvastatin", "atorvastatin"),
    ("chronic_rx_telmisartan", "telmisartan"),
    # D1 payload enums
    ("summer_2026", "this summer"),
    ("post_resolution_window_apr_jun", "the April-June post-resolution lull"),
    ("what_service_in_demand_this_week", "which service got asked for the most this week"),
    ("corporate_bulk_thali_package", "a corporate bulk-thali package"),
    ("kids_yoga_summer_camp", "a kids' summer yoga camp"),
    ("skin_prep_program_30day", "the 30-day skin-prep program"),
    ("6_month_cleaning", "your 6-month cleaning"),
    ("delivery_late", "late delivery"),
    ("postcard_or_phone_call", "by postcard or a phone call"),
    ("free_for_members", "free for IDA members"),
    ("subscription_expiry", "your subscription expiry"),
    ("kids_yoga_post", "the kids-yoga post"),
    ("saved address", "registered address"),
    ("delivery_address_saved", "registered address"),
)

REWRITE_MAP: dict[str, str] = {}
for _raw, _human in _REWRITE_PAIRS:
    REWRITE_MAP[_raw] = _human
    REWRITE_MAP[_raw.replace("_", " ")] = _human
    REWRITE_MAP[_raw.replace("_", "-")] = _human

_GLYPH_PRICE = re.compile(r">\s*(?:₹|Rs\.?)\s?([\d,]+)", re.IGNORECASE)


def human_month_range(month_range: str) -> str:
    """'Nov-Feb' -> 'November to February'; 'Jan' -> 'January'; wrap-around aware."""
    _MONTHS = {
        "jan": "January", "feb": "February", "mar": "March", "apr": "April",
        "may": "May", "jun": "June", "jul": "July", "aug": "August",
        "sep": "September", "oct": "October", "nov": "November", "dec": "December",
    }
    parts = [p.strip() for p in (month_range or "").split("-") if p.strip()]
    names = [_MONTHS.get(p[:3].lower(), p) for p in parts]
    if not names:
        return month_range
    if len(names) == 1:
        return names[0]
    return f"{names[0]} to {names[-1]}"


def humanize_title(title: str) -> str:
    """Render an offer title for copy: '> Rs.499' becomes 'on orders above Rs.499'.

    Idempotent; leaves every other title untouched."""
    if not title:
        return title
    out = _GLYPH_PRICE.sub(r"on orders above Rs.\1", title)
    out = out.replace(">=", "at or above").replace("<=", "up to")
    out = re.sub(r"\s{2,}", " ", out).strip()
    return out


def _rewrite_glyph_prices(text: str) -> str:
    return humanize_title(text)


_MONTH_RANGE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s?-\s?"
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b",
    re.IGNORECASE,
)

# peer_stats.scope slug -> the human phrase used inside gap copy
_SCOPE_DISPLAY: dict[str, str] = {
    "metro_unisex_salons_2026": "metro unisex-salon",
    "metro_solo_practices_2026": "metro solo-practice",
    "metro_neighbourhood_gyms_2026": "neighbourhood-gym",
    "metro_neighbourhood_pharmacies_2026": "neighbourhood-pharmacy",
    "metro_casual_dining_2026": "casual-dining",
}

# category slug -> the human label frames use in copy
SLUG_LABELS: dict[str, str] = {
    "dentists": "dental",
    "salons": "salon",
    "gyms": "gym",
    "restaurants": "restaurant",
    "pharmacies": "pharmacy",
}


def category_label(slug: str) -> str:
    """'dentists' -> 'dental' — never the raw slug inside copy."""
    return SLUG_LABELS.get(slug, slug)


# ---------------------------------------------------------------------------
# payload-enum machinery (D1): structured humanizations + the generic net
# ---------------------------------------------------------------------------

_TREND_VALUE = re.compile(r"^(.+?)_demand_([+-]\d+)$")
_SEASON_VALUE = re.compile(r"^(summer|winter|monsoon|spring|autumn|fall)(?:[_-]\d{4})?$", re.IGNORECASE)
_SEASON_HUMAN = {"summer": "this summer", "winter": "this winter", "monsoon": "this monsoon",
                 "spring": "this spring", "autumn": "this autumn", "fall": "this fall"}

# payload keys whose values the pipeline renders through a STRUCTURED
# humanizer — their prescribed human forms are blessed copy and their raw
# forms are blocked by the vocabulary. Keys NOT listed here get the generic
# net: any 2+-token surface variant appearing in a body is a leak (this is
# what survives unseen Phase-3 payload fields).
STRUCTURED_HUMANIZER_KEYS = frozenset({
    "season", "season_note", "ask_template", "intent_topic",
    "next_step_window_open", "previous_focus", "service_due", "theme",
    "verification_path", "fee", "last_topic", "likely_driver", "metric",
    "trend", "trends", "category", "category_relevance", "window",
    "their_offer", "merchant_last_message", "common_quote", "match", "venue",
    # rendered verbatim by design (spec-mandated slot labels, the competitor's
    # proper name, the merchant's own words, batch codes required by the
    # supply-alert spec, festival/city proper names)
    "available_slots", "next_session_options", "slots", "competitor_name",
    "affected_batches", "molecule", "manufacturer", "festival", "city",
})

# keys whose humanized forms are blessed copy — excludes slug-valued keys
# (payload.category = "dentists" must stay blocked)
_BLESS_HUMANIZED_KEYS = frozenset({
    "season", "trends", "fee", "verification_path", "next_step_window_open",
    "service_due", "theme", "previous_focus", "intent_topic", "last_topic",
    "likely_driver", "ask_template", "season_note", "window", "trend",
    "metric",
})


def humanize_payload_value(key: str, value: str) -> str:
    """Structured merchant-language rendering for known payload values.

    'summer_2026' -> 'this summer'; 'ORS_demand_+40' -> 'ORS demand +40%';
    slot labels and proper names render verbatim; unknown keys fall back to
    underscore->space."""
    text = str(value)
    if key in ("available_slots", "next_session_options", "slots", "competitor_name",
               "merchant_last_message", "common_quote", "match", "venue", "their_offer",
               "affected_batches", "molecule", "manufacturer", "festival", "city"):
        return text
    if key == "season":
        match = _SEASON_VALUE.match(text)
        if match:
            return _SEASON_HUMAN.get(match.group(1).lower(), match.group(1).lower())
    if key == "trends" or _TREND_VALUE.match(text):
        match = _TREND_VALUE.match(text)
        if match:
            label = match.group(1).replace("_", " ") + " demand"
            return f"{label} {match.group(2)}%"
    if key == "fee" and text == "free_for_members":
        return "free for IDA members"
    if key == "verification_path":
        return text.replace("_or_", " or a ").replace("_", " ")
    if key == "next_step_window_open":
        return re.sub(r"(\d+)day\b", r"\1-day", text.replace("_", " "))
    return text.replace("_", " ").strip()


def _iter_payload_strings(node, out: set[str]) -> None:
    if isinstance(node, dict):
        for value in node.values():
            _iter_payload_strings(value, out)
    elif isinstance(node, list):
        for item in node:
            _iter_payload_strings(item, out)
    elif isinstance(node, str):
        text = node.strip()
        if text:
            out.add(text)


def key_of_value(node, target: str) -> str | None:
    """The first payload key whose (recursed) values contain *target*."""
    if isinstance(node, dict):
        for key, value in node.items():
            found: set[str] = set()
            _iter_payload_strings(value, found)
            if target in found:
                return str(key)
    elif isinstance(node, list):
        for item in node:
            key = key_of_value(item, target)
            if key:
                return key
    return None


def parse_like_date(text: str) -> bool:
    from vera.signals.derived import parse_dt

    return parse_dt(text) is not None


def _norm_phrase(text: str) -> str:
    return re.sub(r"[^0-9a-z]+", " ", text.lower()).strip()


@lru_cache(maxsize=1)
def _sanctioned_phrases() -> frozenset[str]:
    """Phrases that are merchant-authored or prescribed rewrites — a payload
    variant that relates to one of these as substring is blessed copy."""
    phrases = {h.lower() for h in REWRITE_MAP.values() if " " in h}
    root = _expanded_dir()
    for data in _load_json_dir(root / "categories"):
        for offer in data.get("offer_catalog") or []:
            if isinstance(offer, dict) and isinstance(offer.get("title"), str):
                norm = _norm_phrase(offer["title"])
                if norm:
                    phrases.add(norm)
    for data in _load_json_dir(root / "merchants"):
        for offer in data.get("offers") or []:
            if isinstance(offer, dict) and isinstance(offer.get("title"), str):
                norm = _norm_phrase(offer["title"])
                if norm:
                    phrases.add(norm)
    for data in _load_json_dir(root / "customers"):
        relationship = data.get("relationship") or {}
        for source in ("services_received", "chronic_conditions"):
            values = relationship.get(source)
            if isinstance(values, list):
                for value in values:
                    if isinstance(value, str):
                        norm = _norm_phrase(value)
                        if norm:
                            phrases.add(norm)
    return frozenset(phrases)


def _is_sanctioned_phrase(norm: str) -> bool:
    return any(norm in p or p in norm for p in _sanctioned_phrases())


def payload_net_variants(trigger: dict | None,
                         customer: dict | None = None) -> list[str]:
    """Generic payload-enum net (D1): 2+-token surface variants of every
    string value in ``trigger.payload`` (recursed), plus 2+-token
    ``customer.preferences.*`` values. Values under known humanizer keys
    contribute only their RAW forms (the humanized rendering is blessed
    copy); unknown keys contribute every surface form — so an unseen
    Phase-3 field rendered raw always fires."""
    variants: set[str] = set()
    payload = (trigger or {}).get("payload")
    strings: set[str] = set()
    _iter_payload_strings(payload, strings)
    preferences = (customer or {}).get("preferences") or {}
    if isinstance(preferences, dict):
        for value in preferences.values():
            if isinstance(value, str):
                strings.add(value)
    for text in strings:
        if re.fullmatch(r"[\d,.\-+]+", text) or parse_like_date(text):
            continue
        key_humanized: set[str] = set()
        if isinstance(payload, dict):
            for key, value in payload.items():
                flattened: set[str] = set()
                _iter_payload_strings(value, flattened)
                if text in flattened and key in STRUCTURED_HUMANIZER_KEYS:
                    key_humanized.add(humanize_payload_value(key, text).lower())
        for pref_key, value in preferences.items():
            if isinstance(value, str) and value == text:
                key_humanized.add(humanize_payload_value(pref_key, text).lower())
        forms = {text.lower(),
                 text.lower().replace("_", " "),
                 text.lower().replace("_", "-")}
        for form in forms:
            norm = _norm_phrase(form)
            if not norm or " " not in norm:
                continue  # the net covers 2+-token variants only
            human_norms = {_norm_phrase(h) for h in key_humanized}
            if any(norm in h or h in norm for h in human_norms if h):
                continue  # equals/inside its own structured humanization
            if _is_sanctioned_phrase(norm):
                continue
            variants.add(norm)
    return sorted(variants)


def scope_display(scope: str) -> str:
    """'metro_unisex_salons_2026' -> 'metro unisex-salon' (never the raw slug).

    Unknown scopes degrade gracefully: trailing year and underscores are
    stripped so no snake_case can survive."""
    display = _SCOPE_DISPLAY.get(scope)
    if display:
        return display
    text = re.sub(r"_?\d{4}$", "", scope)
    text = text.replace("_", " ").replace("-", " ").strip()
    return text or "peer"


def rewrite_internal(text: str) -> str:
    """Apply the safe-rewrite map (and glyph-price cleanup) to *text*.

    Multi-word keys match with any [-_ ] separator mix, so the dataset's
    'high-risk adults' (hyphen+space) is rewritten exactly like
    'high_risk_adults'. One combined pass (longest-first), and already-rewritten
    renderings are protected — the function is deterministic and idempotent.
    """
    if not text:
        return text
    out = _rewrite_glyph_prices(text)
    out = _MONTH_RANGE.sub(lambda m: human_month_range(m.group(0)), out)

    # protect existing multi-word human renderings from re-matching
    # (idempotency: 'your 6-month cleaning' contains the key sequence
    # '6-month-cleaning' and would chain on a re-run). Only MULTI-WORD humans
    # need protection — a single-word insertion ('salon') can never re-match a
    # key pattern ('salons'), and case-insensitive or short-word protection
    # would corrupt natural copy like 'metro-unisex-salons-2026' or downcase
    # 'Dental Cleaning'.
    humans = sorted({h for h in REWRITE_MAP.values() if h and " " in h and len(h) >= 8},
                    key=len, reverse=True)
    sentinels: dict[str, str] = {}
    for i, human in enumerate(humans):
        pattern = re.compile(re.escape(human))
        if pattern.search(out):
            sentinel = f"\x00{i}\x00"
            out = pattern.sub(sentinel, out)
            sentinels[sentinel] = human

    # one combined pass over unique word-sequence patterns, longest first
    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for internal, human in REWRITE_MAP.items():
        if not internal:
            continue
        words = [w for w in re.split(r"[-_ ]+", internal) if w]
        if len(words) > 1:
            pat = r"[-_ ]+".join(re.escape(w) for w in words)
        else:
            pat = re.escape(internal)
        if pat in seen:
            continue
        seen.add(pat)
        entries.append((pat, human))
    entries.sort(key=lambda e: len(e[0]), reverse=True)
    combined = re.compile("|".join(f"(?:{p})" for p, _ in entries), re.IGNORECASE)

    def _repl(match: re.Match) -> str:
        matched = match.group(0)
        for pat, human in entries:
            if re.fullmatch(pat, matched, re.IGNORECASE):
                return human
        return matched

    out = combined.sub(_repl, out)
    for sentinel, human in sentinels.items():
        out = out.replace(sentinel, human)
    return re.sub(r"\s{2,}", " ", out).strip()


# ---------------------------------------------------------------------------
# vocabulary construction
# ---------------------------------------------------------------------------

def _collect_categories(data: dict, tokens: set[str]) -> None:
    slug = data.get("slug")
    if slug:
        tokens.add(str(slug))
    voice = data.get("voice") or {}
    for key in ("tone", "register", "code_mix"):
        value = voice.get(key)
        if isinstance(value, str):
            tokens.add(value)
    peer = data.get("peer_stats") or {}
    if isinstance(peer.get("scope"), str):
        tokens.add(peer["scope"])
    for offer in data.get("offer_catalog") or []:
        if isinstance(offer, dict):
            for key in ("audience", "type"):
                value = offer.get(key)
                if isinstance(value, str):
                    tokens.add(value)
    for item in data.get("digest") or []:
        if isinstance(item, dict):
            for key in ("kind", "patient_segment"):
                value = item.get(key)
                if isinstance(value, str):
                    tokens.add(value)
    for beat in data.get("seasonal_beats") or []:
        if isinstance(beat, dict):
            value = beat.get("month_range") or beat.get("month")
            if isinstance(value, str):
                tokens.add(value)


def _collect_merchant(data: dict, tokens: set[str]) -> None:
    subscription = data.get("subscription") or {}
    for key in ("status", "plan"):
        value = subscription.get(key)
        if isinstance(value, str):
            tokens.add(value)
    for raw in data.get("signals") or []:
        if isinstance(raw, str) and raw.strip():
            tokens.add(raw.strip())
            tokens.add(raw.split(":", 1)[0].strip())
    for offer in data.get("offers") or []:
        if isinstance(offer, dict) and isinstance(offer.get("status"), str):
            tokens.add(offer["status"])
    for theme in data.get("review_themes") or []:
        if isinstance(theme, dict):
            for key in ("theme", "sentiment"):
                value = theme.get(key)
                if isinstance(value, str):
                    tokens.add(value)
    aggregate = data.get("customer_aggregate") or {}
    if isinstance(aggregate, dict):
        tokens.update(str(k) for k in aggregate.keys())


def _collect_customer(data: dict, tokens: set[str]) -> None:
    if isinstance(data.get("state"), str):
        tokens.add(data["state"])
    preferences = data.get("preferences") or {}
    if isinstance(preferences, dict):
        for key in ("preferred_slots", "channel", "training_focus",
                    "health_focus", "delivery_address"):
            value = preferences.get(key)
            if isinstance(value, str):
                tokens.add(value)
    consent = data.get("consent") or {}
    scope = consent.get("scope")
    if isinstance(scope, list):
        tokens.update(str(s) for s in scope if isinstance(s, str))
    relationship = data.get("relationship") or {}
    if isinstance(relationship, dict):
        services = relationship.get("services_received")
        if isinstance(services, list):
            tokens.update(str(s) for s in services if isinstance(s, str))
        conditions = relationship.get("chronic_conditions")
        if isinstance(conditions, list):
            tokens.update(str(c) for c in conditions if isinstance(c, str))
    identity = data.get("identity") or {}
    if isinstance(identity.get("age_band"), str):
        tokens.add(identity["age_band"])


def _collect_trigger(data: dict, tokens: set[str]) -> None:
    for key in ("kind", "scope", "source"):
        value = data.get(key)
        if isinstance(value, str):
            tokens.add(value)
    # D1: every string value anywhere in the payload (recursed)
    payload_strings: set[str] = set()
    _iter_payload_strings(data.get("payload"), payload_strings)
    for text in payload_strings:
        if re.fullmatch(r"[\d,.\-+]+", text) or parse_like_date(text):
            continue
        if re.fullmatch(r"[a-z]{1,4}_\d{4}[A-Za-z0-9_]*", text.lower()):
            continue  # digest ids: blocked by the id-shape regex instead
        if re.fullmatch(r"[A-Za-z]{2,4}\d{2,4}-[A-Za-z0-9]{2,6}", text):
            continue  # batch codes: spec-required verbatim content
        if key_of_value(data.get("payload"), text) in STRUCTURED_HUMANIZER_KEYS:
            continue  # structured-humanizer values: net covers the raw forms
        tokens.add(text)


def _load_json_dir(directory: Path) -> list[dict]:
    items: list[dict] = []
    if not directory.exists():
        return items
    for path in sorted(directory.glob("*.json")):
        try:
            items.append(json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, OSError):
            continue
    return items


def build_internal_vocab(expanded_dir: Path | str) -> frozenset[str]:
    """Enumerate the machine-enum leak surface from *expanded_dir* and return
    every blocked surface form."""
    root = Path(expanded_dir)
    raw: set[str] = set()

    for data in _load_json_dir(root / "categories"):
        _collect_categories(data, raw)
    for data in _load_json_dir(root / "merchants"):
        _collect_merchant(data, raw)
    for data in _load_json_dir(root / "customers"):
        _collect_customer(data, raw)
    for data in _load_json_dir(root / "triggers"):
        _collect_trigger(data, raw)

    raw |= ANALYST_WORDS
    tokens = {t.strip().lower() for t in raw if t and len(t.strip()) >= 2
              and not t.strip().isdigit()}

    # sanctioned: words that appear inside merchant-facing offer titles or the
    # category's own allowed vocabulary, plus plan names — those are copy.
    sanctioned_words: set[str] = set()
    for data in _load_json_dir(root / "categories"):
        for offer in data.get("offer_catalog") or []:
            if isinstance(offer, dict) and isinstance(offer.get("title"), str):
                sanctioned_words.update(re.findall(r"[a-z]+", offer["title"].lower()))
        voice = data.get("voice") or {}
        for source in _SANCTION_SOURCES:
            value = voice.get(source)
            if isinstance(value, list):
                for phrase in value:
                    if isinstance(phrase, str):
                        sanctioned_words.update(re.findall(r"[a-z]+", phrase.lower()))
    for data in _load_json_dir(root / "merchants"):
        for offer in data.get("offers") or []:
            if isinstance(offer, dict) and isinstance(offer.get("title"), str):
                sanctioned_words.update(re.findall(r"[a-z]+", offer["title"].lower()))
        plan = (data.get("subscription") or {}).get("plan")
        if isinstance(plan, str):
            sanctioned_words.update(re.findall(r"[a-z]+", plan.lower()))
    sanctioned_words |= _NATURAL_WORDS
    # rewrite targets are the sanctioned renderings: their words are sanctioned
    # and the full phrases protect overlapping multi-word enum forms
    sanctioned_phrases: set[str] = set()
    for human in REWRITE_MAP.values():
        if " " in human:
            sanctioned_phrases.add(human.lower())
        sanctioned_words.update(re.findall(r"[a-z]+", human.lower()))

    # D1: payload values under structured humanizer keys are exempt in their
    # humanized form (that rendering IS the blessed copy) — but only for keys
    # whose humanization is a real transformation; generic space-forms of
    # slug-valued keys (payload.category = "dentists") stay blocked
    for data in _load_json_dir(root / "triggers"):
        payload = data.get("payload")
        if not isinstance(payload, dict):
            continue
        for key, value in payload.items():
            if key not in _BLESS_HUMANIZED_KEYS:
                continue
            flattened: set[str] = set()
            _iter_payload_strings(value, flattened)
            for text in flattened:
                human = humanize_payload_value(key, text).lower()
                norm = _norm_phrase(human)
                if norm:
                    sanctioned_phrases.add(norm)
                sanctioned_words.update(re.findall(r"[a-z]+", human))

    # service history and condition values are the merchant's own domain
    # vocabulary (they also collide head-on with natural copy: 'dine-in') —
    # exempt their exact normalised forms from blocking
    for data in _load_json_dir(root / "customers"):
        relationship = data.get("relationship") or {}
        for source in ("services_received", "chronic_conditions"):
            values = relationship.get(source)
            if isinstance(values, list):
                for value in values:
                    if isinstance(value, str) and value.strip():
                        norm = value.strip().lower().replace("_", " ").replace("-", " ")
                        norm = re.sub(r"\s+", " ", norm)
                        if norm:
                            sanctioned_phrases.add(norm)

    # offer titles are merchant-authored copy: any multi-word enum form that is
    # a substring of a title phrase cannot be jargon ('weekday lunch' inside
    # 'Weekday Lunch Thali @ Rs.149')
    for data in _load_json_dir(root / "categories"):
        for offer in data.get("offer_catalog") or []:
            if isinstance(offer, dict) and isinstance(offer.get("title"), str):
                norm = offer["title"].lower().replace("_", " ")
                norm = re.sub(r"[^0-9a-z]+", " ", norm).strip()
                if norm:
                    sanctioned_phrases.add(norm)
    for data in _load_json_dir(root / "merchants"):
        for offer in data.get("offers") or []:
            if isinstance(offer, dict) and isinstance(offer.get("title"), str):
                norm = offer["title"].lower().replace("_", " ")
                norm = re.sub(r"[^0-9a-z]+", " ", norm).strip()
                if norm:
                    sanctioned_phrases.add(norm)

    blocked: set[str] = set()
    for token in tokens:
        forms = {token,
                 token.replace("_", " "),
                 token.replace("_", "-"),
                 " ".join(w.capitalize() for w in token.replace("_", " ").split())}
        if "_" not in token and " " not in token and "-" not in token \
                and token not in sanctioned_words:
            # plural/singular variants for bare single words — but never for a
            # word that is itself sanctioned ('pro' must not produce 'pros')
            if token.endswith("s") and len(token) > 3:
                forms.add(token[:-1])
            else:
                forms.add(token + "s")
        for form in forms:
            form = form.strip().lower()
            if not form:
                continue
            if " " in form or "-" in form:
                # multi-word: subtract a blocked phrase only against sanctioned
                # PHRASES (e.g. "weekday evening" inside "weekday evenings") —
                # never against single sanctioned words, or "delivery late"
                # would be unblocked because "delivery" appears in a title.
                norm = form.replace("-", " ").replace("_", " ")
                norm = re.sub(r"\s+", " ", norm)
                if any(norm in p or p in norm for p in sanctioned_phrases):
                    continue
                blocked.add(form)
            else:
                if form in sanctioned_words:
                    continue
                blocked.add(form)
    return frozenset(blocked)


_DEFAULT_DIR = Path(__file__).resolve().parents[1] / "expanded"


def _expanded_dir() -> Path:
    env = os.environ.get("VERA_EXPANDED_DIR")
    return Path(env) if env else _DEFAULT_DIR


@lru_cache(maxsize=1)
def _cached_vocab(expanded_dir: str) -> frozenset[str]:
    return build_internal_vocab(expanded_dir)


def load_internal_vocab() -> frozenset[str]:
    """Cached blocked-vocabulary; directory from ``VERA_EXPANDED_DIR`` env or
    the repo's ``expanded/``."""
    return _cached_vocab(str(_expanded_dir()))
