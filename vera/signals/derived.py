"""Derived-signal engine: fact extraction + deterministic ranking.

This module is the grounding whitelist's source of truth. It turns the four
context layers (category, merchant, trigger, customer?) into a ranked list of
:class:`Fact` objects. Every number a composed message may contain must be
traceable to one of these facts.

Invariants:
- pure and deterministic: same inputs -> equal Fact list, no clock, no I/O
- absent source fields yield NO fact (never guess, never default to 0)
- 40 of 50 merchants are thin (no offers/signals/history/review_themes) and
  75 of 100 triggers have placeholder payloads; this module must still emit
  6+ high-salience facts for those cases (identity + performance-vs-peer
  benchmarks + momentum + cohort + tenure)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable

__all__ = [
    "Fact",
    "KINDS",
    "derive_facts",
    "rank_facts",
    "pick_primary",
    "facts_by_kind",
    "is_placeholder_trigger",
    "consent_allows",
    "permitted_intent",
    "parse_dt",
    "format_day_month",
]

KINDS = (
    "identity", "metric", "benchmark", "delta", "offer", "digest", "seasonal",
    "trend", "content", "subscription", "customer", "cohort", "review", "trigger",
)

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH_FULL = {
    1: "January", 2: "February", 3: "March", 4: "April", 5: "May", 6: "June",
    7: "July", 8: "August", 9: "September", 10: "October", 11: "November", 12: "December",
}


@dataclass(frozen=True)
class Fact:
    """One verifiable, groundable atom of context.

    ``phrase`` is human copy ready to drop into a message; ``value`` stays raw
    so the numeral whitelist can canonicalise it; ``json_path`` is provenance
    (``derived:<formula>`` only for computed ratios); ``citation`` carries a
    source string (digest items) that frames append as "— <source>".
    """

    key: str
    value: object
    phrase: str
    json_path: str
    kind: str
    salience: float
    citation: str = field(default="", compare=False)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def parse_dt(value: Any) -> datetime | None:
    """Parse ISO-ish date strings; returns None when unparseable."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        return parsed
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def format_day_month(dtm: datetime) -> str:
    """'2026-04-28' -> '28 April' (deterministic, no year in copy)."""
    return f"{dtm.day} {_MONTH_FULL[dtm.month]}"


def _humanize(token: str) -> str:
    return token.replace("_", " ").strip()


# analyst words that appear inside dataset note text — reworded at the fact
# layer so quoted notes can never leak machine vocabulary into copy
_NOTE_WORD_MAP = {"cohort": "group", "segment": "group", "metric": "number",
                  "delta": "change", "aggregate": "total"}


def _humanize_note(text: str) -> str:
    out = _humanize(text)
    for bad, good in _NOTE_WORD_MAP.items():
        out = re.sub(rf"\b{bad}\b", good, out, flags=re.IGNORECASE)
    return out


def _money(value: float) -> str:
    return f"Rs.{value:,.0f}"


def _pct(value: float, decimals: int = 1) -> str:
    return f"{value * 100:.{decimals}f}%"


def _num(value: float) -> str:
    if float(value) == int(value):
        return f"{int(value):,}"
    return f"{value:g}"


class _Collector:
    """Fact accumulator with key de-duplication (first wins)."""

    def __init__(self) -> None:
        self._facts: list[Fact] = []
        self._keys: set[str] = set()

    def add(self, key: str, value: Any, phrase: str, json_path: str,
            kind: str, salience: float, citation: str = "") -> None:
        if value is None or key in self._keys:
            return
        if isinstance(value, str) and not value.strip():
            return
        if kind not in KINDS:
            kind = "metric"
        self._keys.add(key)
        self._facts.append(
            Fact(key=key, value=value, phrase=phrase, json_path=json_path,
                 kind=kind, salience=max(0.0, min(1.0, float(salience))),
                 citation=citation)
        )

    def extend(self, facts: Iterable[Fact]) -> None:
        for fact in facts:
            if fact.key not in self._keys:
                self._keys.add(fact.key)
                self._facts.append(fact)

    def result(self) -> list[Fact]:
        return list(self._facts)


def _gap_salience(gap_pct: float) -> float:
    magnitude = abs(gap_pct)
    if magnitude >= 15:
        return min(0.98, 0.8 + magnitude / 250.0)
    return 0.5


# ---------------------------------------------------------------------------
# identity / subscription
# ---------------------------------------------------------------------------

def _identity_facts(merchant: dict | None, out: _Collector) -> None:
    identity = (merchant or {}).get("identity") or {}
    base = "merchant.identity"
    owner = identity.get("owner_first_name")
    if owner:
        out.add("owner_first_name", owner, owner, f"{base}.owner_first_name", "identity", 0.4)
    name = identity.get("name")
    if name:
        out.add("business_name", name, name, f"{base}.name", "identity", 0.4)
    if identity.get("locality"):
        out.add("locality", identity["locality"], identity["locality"],
                f"{base}.locality", "identity", 0.35)
    if identity.get("city"):
        out.add("city", identity["city"], identity["city"], f"{base}.city", "identity", 0.3)
    if "verified" in identity:
        verified = bool(identity["verified"])
        out.add(
            "gbp_verified", verified,
            "Google profile is verified" if verified else "Google profile is still unverified",
            f"{base}.verified", "identity", 0.5 if not verified else 0.3,
        )
    languages = identity.get("languages")
    if languages:
        out.add("languages", list(languages), " + ".join(str(l) for l in languages),
                f"{base}.languages", "identity", 0.3)
    established = identity.get("established_year")
    if isinstance(established, int) and 1900 < established < 2100:
        pass  # tenure needs `now`; added in derive_facts


def _subscription_facts(merchant: dict | None, out: _Collector) -> None:
    subscription = (merchant or {}).get("subscription") or {}
    base = "merchant.subscription"
    status = subscription.get("status")
    if status:
        out.add("subscription_status", status, f"subscription is {status}",
                f"{base}.status", "subscription", 0.35)
    plan = subscription.get("plan")
    if plan:
        out.add("subscription_plan", plan, f"{plan} plan", f"{base}.plan", "subscription", 0.3)
    days_remaining = subscription.get("days_remaining")
    if isinstance(days_remaining, (int, float)) and days_remaining > 0:
        soon = days_remaining <= 30
        out.add("subscription_days_remaining", days_remaining,
                f"{plan or 'subscription'} plan renews in {int(days_remaining)} days"
                if plan else f"subscription renews in {int(days_remaining)} days",
                f"{base}.days_remaining", "subscription", 0.75 if soon else 0.3)
    since_expiry = subscription.get("days_since_expiry")
    if isinstance(since_expiry, (int, float)) and since_expiry > 0:
        out.add("subscription_days_since_expiry", since_expiry,
                f"plan lapsed {int(since_expiry)} days ago",
                f"{base}.days_since_expiry", "subscription", 0.7)


# ---------------------------------------------------------------------------
# performance vs peers
# ---------------------------------------------------------------------------

_PERF_PAIRS = (
    ("views", "avg_views_30d", "views in 30 days"),
    ("calls", "avg_calls_30d", "calls in 30 days"),
    ("directions", "avg_directions_30d", "direction requests in 30 days"),
)


def _scope_text(peer: dict) -> str:
    """Human rendering of ``peer_stats.scope`` — never the raw slug.

    'metro_unisex_salons_2026' -> 'metro unisex-salon' (the phrase reads
    '... vs the 4.0% metro unisex-salon average')."""
    scope = peer.get("scope")
    if isinstance(scope, str) and scope:
        from harness.internal_vocab import scope_display

        return scope_display(scope)
    return "peer"


def _performance_facts(category: dict | None, merchant: dict | None,
                       out: _Collector) -> None:
    perf = (merchant or {}).get("performance") or {}
    peer = (category or {}).get("peer_stats") or {}
    base = "merchant.performance"
    scope = _scope_text(peer)

    ctr = perf.get("ctr")
    if isinstance(ctr, (int, float)) and not isinstance(ctr, bool):
        out.add("ctr", ctr, f"{_pct(ctr)} CTR on the listing", f"{base}.ctr", "metric", 0.6)
        peer_ctr = peer.get("avg_ctr")
        if isinstance(peer_ctr, (int, float)) and peer_ctr:
            gap_pct = round((ctr - peer_ctr) / peer_ctr * 100)
            out.add(
                "gap_ctr", gap_pct,
                f"{_pct(ctr)} CTR vs the {_pct(peer_ctr)} {scope} average ({gap_pct:+d}%)",
                "derived:ctr_gap_pct = (merchant.performance.ctr - category.peer_stats.avg_ctr)"
                " / category.peer_stats.avg_ctr",
                "delta", _gap_salience(gap_pct),
            )
        out.add("benchmark_avg_ctr", peer_ctr, f"{_pct(peer_ctr)} peer-average CTR",
                "category.peer_stats.avg_ctr", "benchmark", 0.3) if peer_ctr else None

    for mkey, pkey, label in _PERF_PAIRS:
        value = perf.get(mkey)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out.add(mkey, value, f"{_num(value)} {label}", f"{base}.{mkey}", "metric", 0.55)
            peer_value = peer.get(pkey)
            if isinstance(peer_value, (int, float)) and peer_value:
                gap_pct = round((value - peer_value) / peer_value * 100)
                out.add(
                    f"gap_{mkey}", gap_pct,
                    f"{_num(value)} {label} vs the {_num(peer_value)} {scope} ({gap_pct:+d}%)",
                    f"derived:{mkey}_gap_pct = (merchant.performance.{mkey} - "
                    f"category.peer_stats.{pkey}) / category.peer_stats.{pkey}",
                    "delta", _gap_salience(gap_pct),
                )
                out.add(
                    f"peer_{pkey}", peer_value, f"peer average {_num(peer_value)}",
                    f"category.peer_stats.{pkey}", "benchmark", 0.3,
                )

    for key in ("rating", "review_count"):
        value = perf.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out.add(key, value, f"{_num(value)} {key.replace('_', ' ')}",
                    f"{base}.{key}", "metric", 0.5)
            peer_key = f"avg_{key}"
            peer_value = peer.get(peer_key)
            if isinstance(peer_value, (int, float)) and peer_value:
                gap_pct = round((value - peer_value) / peer_value * 100)
                out.add(f"gap_{key}", gap_pct,
                        f"{_num(value)} {key.replace('_', ' ')} vs {_num(peer_value)} peer average ({gap_pct:+d}%)",
                        f"derived:{key}_gap_pct = (merchant.performance.{key} - category.peer_stats.{peer_key})"
                        f" / category.peer_stats.{peer_key}",
                        "delta", _gap_salience(gap_pct))

    # derived rates (json_path = "derived:<formula>")
    views = perf.get("views")
    calls = perf.get("calls")
    directions = perf.get("directions")
    leads = perf.get("leads")
    if isinstance(views, (int, float)) and views > 0:
        if isinstance(calls, (int, float)) and calls >= 0:
            rate = calls / views
            out.add("call_rate", round(rate * 100, 2),
                    f"{_pct(rate, 2)} of viewers call",
                    "derived:call_rate = calls/views", "metric", 0.4)
        if isinstance(directions, (int, float)) and directions >= 0:
            rate = directions / views
            out.add("direction_rate", round(rate * 100, 2),
                    f"{_pct(rate, 2)} ask for directions",
                    "derived:direction_rate = directions/views", "metric", 0.4)
        if isinstance(calls, (int, float)) and isinstance(directions, (int, float)):
            rate = (calls + directions) / views
            out.add("view_to_action", round(rate * 100, 2),
                    f"{_pct(rate, 2)} of viewers take an action (call or directions)",
                    "derived:view_to_action = (calls+directions)/views", "metric", 0.45)
    if isinstance(calls, (int, float)) and calls > 0 and isinstance(leads, (int, float)) and leads >= 0:
        rate = leads / calls
        out.add("lead_rate", round(rate * 100, 2),
                f"{int(leads)} of {int(calls)} calls became leads ({_pct(rate, 0)})",
                "derived:lead_rate = leads/calls", "metric", 0.45)

    delta = perf.get("delta_7d") or {}
    for key in ("views_pct", "calls_pct", "ctr_pct"):
        value = delta.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            metric = key.replace("_pct", "")
            direction = "up" if value >= 0 else "down"
            out.add(
                f"momentum_{metric}", value,
                f"{metric} {direction} {abs(round(value * 100))}% vs last week",
                f"{base}.delta_7d.{key}", "delta",
                0.75 if abs(value) >= 0.15 else 0.5,
            )


# ---------------------------------------------------------------------------
# cohort (customer_aggregate) — keys read dynamically
# ---------------------------------------------------------------------------

_COHORT_LABELS = {
    "total_unique_ytd": ("unique customers this year", 0.5),
    "lapsed_180d_plus": ("customers lapsed 180+ days", 0.6),
    "lapsed_90d_plus": ("customers lapsed 90+ days", 0.6),
    "high_risk_adult_count": ("high-risk adults in the roster", 0.7),
    "total_active_members": ("active members", 0.55),
    "chronic_rx_count": ("chronic-prescription customers", 0.65),
    "delivery_orders_30d": ("delivery orders in 30 days", 0.4),
    "dine_in_orders_30d": ("dine-in orders in 30 days", 0.4),
}

_PCT_LABELS = {
    "retention_6mo_pct": "6-month retention",
    "retention_3mo_pct": "3-month retention",
    "retention_30d_pct": "30-day retention",
    "monthly_churn_pct": "monthly churn",
    "trial_to_paid_pct": "trial to paid conversion",
    "delivery_share_pct": "delivery share of orders",
    "repeat_customer_pct": "repeat-customer rate",
}


def _cohort_facts(category: dict | None, merchant: dict | None,
                  out: _Collector) -> None:
    aggregate = (merchant or {}).get("customer_aggregate") or {}
    peer = (category or {}).get("peer_stats") or {}
    base = "merchant.customer_aggregate"
    total = aggregate.get("total_unique_ytd")

    for key, value in aggregate.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        if key in _PCT_LABELS:
            label = _PCT_LABELS[key]
            out.add(key, round(value * 100), f"{label} at {_pct(value, 0)}",
                    f"{base}.{key}", "cohort", 0.55)
            peer_value = peer.get(key)
            if isinstance(peer_value, (int, float)):
                gap_pct = round((value - peer_value) / peer_value * 100)
                out.add(
                    f"gap_{key}", gap_pct,
                    f"{label} {_pct(value, 0)} vs {_pct(peer_value, 0)} peer average ({gap_pct:+d}%)",
                    f"derived:{key}_gap_pct = (merchant.customer_aggregate.{key} - "
                    f"category.peer_stats.{key}) / category.peer_stats.{key}",
                    "delta", _gap_salience(gap_pct),
                )
            continue
        if key in _COHORT_LABELS:
            label, salience = _COHORT_LABELS[key]
            out.add(key, value, f"{_num(value)} {label}", f"{base}.{key}", "cohort", salience)
            if key in ("lapsed_180d_plus", "lapsed_90d_plus") and isinstance(total, (int, float)) and total:
                share = round(value / total * 100)
                out.add(f"{key}_share", share,
                        f"{_num(value)} of {_num(total)} customers ({share}%) {label}",
                        f"derived:lapsed_share_pct = {key} / total_unique_ytd",
                        "cohort", 0.65 if share >= 12 else 0.45)

    # category-specific peer ratios worth surfacing even without a merchant side
    for key in ("avg_post_freq_days", "avg_photos", "avg_rating", "avg_review_count"):
        peer_value = peer.get(key)
        if isinstance(peer_value, (int, float)) and peer_value:
            if key == "avg_post_freq_days":
                phrase = f"peer businesses post every {int(peer_value)} days"
            elif key == "avg_photos":
                phrase = f"peers average {int(peer_value)} photos on the listing"
            elif key == "avg_rating":
                phrase = f"peer-average rating {_num(peer_value)}"
            else:
                phrase = f"peers average {int(peer_value)} reviews"
            out.add(f"peer_{key}", peer_value, phrase,
                    f"category.peer_stats.{key}", "benchmark", 0.3)


# ---------------------------------------------------------------------------
# category assets
# ---------------------------------------------------------------------------

def _month_in_range(month_range: str, month: int) -> bool:
    """Parse 'Nov-Feb', 'Oct-Dec', 'Apr-Jun', 'Jan', 'Feb 14' incl. wrap-around."""
    text = (month_range or "").strip().lower()
    if not text:
        return False
    parts = [p.strip() for p in text.split("-")]
    months: list[int] = []
    for part in parts:
        token = part.split()[0] if part else ""
        key = token[:3]
        if key in _MONTHS:
            months.append(_MONTHS[key])
    if not months:
        return False
    if len(months) == 1:
        return month == months[0]
    start, end = months[0], months[-1]
    if start <= end:
        return start <= month <= end
    return month >= start or month <= end  # wrap-around, e.g. Nov-Feb


def _category_asset_facts(category: dict | None, now: datetime,
                          out: _Collector) -> None:
    if not category:
        return
    slug = category.get("slug", "category")

    for i, offer in enumerate(category.get("offer_catalog") or []):
        if not isinstance(offer, dict):
            continue
        title = offer.get("title") or ""
        if not title:
            continue
        audience = offer.get("audience", "")
        from harness.internal_vocab import humanize_title

        out.add(
            f"catalog_offer_{offer.get('id', i)}", title, humanize_title(title),
            f"category.offer_catalog[{i}]", "offer", 0.45,
            citation=f"{slug} offer catalog" if audience == "repeat_user" else "",
        )

    for i, item in enumerate(category.get("digest") or []):
        if not isinstance(item, dict):
            continue
        title = item.get("title") or ""
        if not title:
            continue
        digest_id = item.get("id", f"digest_{i}")
        source = item.get("source") or ""
        out.add(f"digest_{digest_id}", title, title,
                f"category.digest[{i}]", "digest", 0.4, citation=source)
        summary = item.get("summary")
        if summary:
            out.add(f"digest_{digest_id}_summary", summary, str(summary),
                    f"category.digest[{i}].summary", "digest", 0.3, citation=source)
            # D5: the percentage in a digest summary is whitelisted explicitly
            # so frames can lead with it (the 50/50 case study does)
            stat = re.search(r"(\d{1,3})\s?%", str(summary))
            if stat:
                pct_value = int(stat.group(1))
                out.add(f"digest_{digest_id}_summary_stat", pct_value,
                        f"the summary cites {pct_value}%",
                        f"category.digest[{i}].summary", "digest", 0.6, citation=source)
        for numeric in ("trial_n", "credits"):
            value = item.get(numeric)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                out.add(f"digest_{digest_id}_{numeric}", value,
                        f"{int(value)} {numeric}",
                        f"category.digest[{i}].{numeric}", "digest", 0.55, citation=source)
        item_date = parse_dt(item.get("date"))
        if item_date:
            out.add(f"digest_{digest_id}_date", item_date.date().isoformat(),
                    format_day_month(item_date),
                    f"category.digest[{i}].date", "digest", 0.55, citation=source)

    for i, beat in enumerate(category.get("seasonal_beats") or []):
        if not isinstance(beat, dict):
            continue
        note = beat.get("note") or ""
        month_range = beat.get("month_range") or beat.get("month") or ""
        if not note and not month_range:
            continue
        in_season = _month_in_range(str(month_range), now.month)
        from harness.internal_vocab import human_month_range

        note = _humanize_note(note)
        phrase = f"{note} ({human_month_range(str(month_range))})" if note \
            else human_month_range(str(month_range))
        out.add(
            f"seasonal_beat_{i}", note or str(month_range), phrase,
            f"category.seasonal_beats[{i}]", "seasonal",
            1.0 if in_season else 0.25,
        )

    for i, trend in enumerate(category.get("trend_signals") or []):
        if not isinstance(trend, dict):
            continue
        query = trend.get("query") or ""
        if not query:
            continue
        delta = trend.get("delta_yoy")
        segment = trend.get("segment_age", "")
        skew = trend.get("skew", "")
        phrase = f"'{query}' searches {(_pct(delta, 0) + ' YoY') if isinstance(delta, (int, float)) else ''}"
        extras = [str(segment), f"{skew}-skewed" if skew else ""]
        extras = [e for e in extras if e and e != "balanced"]
        if extras:
            phrase += f" ({', '.join(extras)})"
        out.add(
            f"trend_{i}", {"query": query, "delta_yoy": delta}, phrase,
            f"category.trend_signals[{i}]", "trend",
            0.7 if isinstance(delta, (int, float)) and delta >= 0.4 else 0.5,
        )

    for i, item in enumerate(category.get("patient_content_library") or []):
        if not isinstance(item, dict):
            continue
        title = item.get("title") or ""
        if not title:
            continue
        channel = item.get("channel", "whatsapp")
        length = item.get("length_seconds")
        phrase = f"ready-to-forward asset '{title}' ({channel}"
        phrase += f", {int(length)}s" if isinstance(length, (int, float)) else ""
        phrase += ")"
        out.add(f"content_{item.get('id', i)}", title, phrase,
                f"category.patient_content_library[{i}]", "content", 0.45)

    authorities = category.get("regulatory_authorities")
    if authorities:
        out.add("regulatory_authorities", list(authorities),
                "regulators: " + ", ".join(str(a) for a in authorities),
                "category.regulatory_authorities", "content", 0.2)
    journals = category.get("professional_journals")
    if journals:
        out.add("professional_journals", list(journals),
                "journals: " + ", ".join(str(j) for j in journals),
                "category.professional_journals", "content", 0.2)


# ---------------------------------------------------------------------------
# merchant offers, signals, review themes
# ---------------------------------------------------------------------------

def _offer_facts(merchant: dict | None, out: _Collector) -> None:
    from harness.internal_vocab import humanize_title

    offers = (merchant or {}).get("offers") or []
    active = [o for o in offers if isinstance(o, dict) and o.get("status") == "active"]
    expired = [o for o in offers if isinstance(o, dict) and o.get("status") == "expired"]
    for i, offer in enumerate(active):
        title = offer.get("title") or ""
        if not title:
            continue
        out.add(f"offer_active_{offer.get('id', i)}", title, humanize_title(title),
                f"merchant.offers[{i}]", "offer", 0.9)
    for i, offer in enumerate(expired):
        title = offer.get("title") or ""
        if not title:
            continue
        ended = offer.get("ended") or ""
        phrase = f"{humanize_title(title)} (ran out" + (f" {ended}" if ended else "") + " — worth re-running)"
        out.add(f"offer_expired_{offer.get('id', i)}", title, phrase,
                f"merchant.offers[{i}]", "offer", 0.55)


_SIGNAL_LABELS = {
    "stale_posts": "last Google post",
    "ctr_below_peer_median": "CTR below the peer median",
    "high_risk_adult_cohort": "high-risk adult cohort flagged",
    "engaged_in_last": "engaged with Vera in the last",
    "renewal_due_soon": "renewal due soon",
    "perf_dip_severe": "sharp performance dip flagged",
    "unverified_gbp": "Google profile unverified",
    "dormant_with_vera": "no message to Vera for",
    "no_active_offers": "no active offer on the listing",
    "winback_eligible": "winback-eligible",
    "perf_dip_post_expiry": "performance dipped after plan expiry",
    "trial_ending_soon": "trial ending soon",
    "new_merchant": "new on the platform",
    "ipl_eligible_locality": "locality sees match-night footfall",
}


def _signal_facts(merchant: dict | None, out: _Collector) -> None:
    import re

    signals = (merchant or {}).get("signals") or []
    for i, raw in enumerate(signals):
        if not isinstance(raw, str) or not raw.strip():
            continue
        name, value, unit = raw, None, ""
        if ":" in raw:
            name, _, tail = raw.partition(":")
            match = re.fullmatch(r"(\d+)\s*([dhm]?)", tail.strip())
            if match:
                value, unit = int(match.group(1)), match.group(2)
        else:
            match = re.search(r"(\d+)\s*([dhm])?$", raw)
            if match and match.start(1) > 0:
                name = raw[: match.start(1)].rstrip("_:")
                value, unit = int(match.group(1)), match.group(2) or ""
        label = _SIGNAL_LABELS.get(name, _humanize(name))
        if value is not None:
            unit_text = {"d": " days", "h": " hours", "m": " minutes", "": ""}[unit]
            if name == "stale_posts":
                phrase = f"last Google post was {value} days ago"
            elif name.startswith("engaged_in_last"):
                phrase = f"engaged in the last {value}{unit_text.strip()}"
            elif name.startswith("dormant_with_vera"):
                phrase = f"no message to Vera for {value} days"
            else:
                phrase = f"{label}{unit_text}: {value}" if unit_text else f"{label} ({value})"
            out.add(f"signal_{name}_{value}", value, phrase,
                    f"merchant.signals[{i}]", "metric",
                    0.6 if name in ("stale_posts", "unverified_gbp", "dormant_with_vera") else 0.45)
        else:
            out.add(f"signal_{name}", raw, label, f"merchant.signals[{i}]", "metric", 0.45)


def _review_facts(merchant: dict | None, out: _Collector) -> None:
    themes = (merchant or {}).get("review_themes") or []
    for i, theme in enumerate(themes):
        if not isinstance(theme, dict):
            continue
        name = theme.get("theme") or ""
        if not name:
            continue
        occurrences = theme.get("occurrences_30d")
        sentiment = theme.get("sentiment", "")
        label = _humanize(name)
        count_text = ""
        if isinstance(occurrences, (int, float)):
            count_text = f"{int(occurrences)} reviews this month mention {label}"
        else:
            count_text = f"recent reviews mention {label}"
        if sentiment == "neg":
            count_text += " (critical)"
        elif sentiment == "pos":
            count_text += " (praise)"
        out.add(f"review_theme_{name}", occurrences if occurrences is not None else name,
                count_text, f"merchant.review_themes[{i}]", "review", 0.6)
        quote = theme.get("common_quote")
        if quote:
            out.add(f"review_quote_{name}", quote, f'customers say: "{quote}"',
                    f"merchant.review_themes[{i}].common_quote", "review", 1.0)


# ---------------------------------------------------------------------------
# trigger payload
# ---------------------------------------------------------------------------

_DATE_KEYS = {
    "due_date", "wedding_date", "stock_runs_out_iso", "deadline_iso",
    "opened_date", "trial_date", "last_refill", "last_service_date",
    "match_time_iso", "date", "trial_completed",
}
_COUNT_PHRASES = {
    "days_until": "{v} days out",
    "days_to_wedding": "{v} days to the wedding",
    "days_since_last_visit": "{v} days since the last visit",
    "days_since_expiry": "{v} days since expiry",
    "days_since_last_merchant_message": "{v} days since the last message",
    "occurrences_30d": "{v} mentions in 30 days",
    "value_now": "currently at {v}",
    "milestone_value": "milestone at {v}",
    "trial_n": "{v}-patient trial",
    "distance_km": "{v} km away",
    "credits": "{v} CME credits",
    "estimated_uplift_pct": "estimated {_uplift}",
    "delta_pct": "{_delta}",
    "perf_dip_pct": "{_delta}",
    "renewal_amount": "{_money} renewal",
}
_LIST_KEYS = {
    "available_slots", "next_session_options", "affected_batches",
    "molecule_list", "trends", "services_due", "slots",
}
_BOOL_PHRASES = {
    "is_weeknight": ("a weeknight", "a weekend"),
    "is_expected_seasonal": ("the seasonal window, not a real dip", "not the seasonal window"),
    "is_imminent": ("imminent", "not imminent"),
    "shelf_action_recommended": ("shelf action recommended this week", ""),
    "delivery_address_saved": ("delivery address saved", ""),
    "verified": ("profile verified", "profile unverified"),
}
_PLAIN_LABELS = {
    "competitor_name": "new competitor: {v}",
    "festival": "{v} window",
    "match": "match: {v}",
    "venue": "venue: {v}",
    "molecule": "molecule: {v}",
    "manufacturer": "manufacturer: {v}",
    "intent_topic": "{v}",
    "merchant_last_message": "merchant said: \"{v}\"",
    "ask_template": "{v}",
    "theme": "theme: {v}",
    "common_quote": "customers say: \"{v}\"",
    "metric": "metric: {v}",
    "window": "window: {v}",
    "plan": "{v} plan",
    "service_due": "{v} due",
    "season": "{v}",
    "their_offer": "their offer: {v}",
    "verification_path": "verification via {v}",
    "last_topic": "last topic: {v}",
    "likely_driver": "likely driver: {v}",
    "trend": "trend: {v}",
    "previous_focus": "previous focus: {v}",
    "season_note": "{v}",
    "fee": "{v}",
}


def _slot_labels(value: Any) -> list[str]:
    labels: list[str] = []
    if isinstance(value, dict):
        value = [value]
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                label = item.get("label")
                if isinstance(label, str) and label.strip():
                    labels.append(label.strip())
            elif isinstance(item, str) and item.strip():
                labels.append(item.strip())
    return labels


def _payload_fact(key: str, value: Any, payload: dict, index_note: str) -> Fact | None:
    json_path = f"trigger.payload.{key}" + (index_note or "")
    if isinstance(value, bool):
        if key in _BOOL_PHRASES:
            truthy, falsy = _BOOL_PHRASES[key]
            phrase = truthy if value else (falsy or f"{_humanize(key)}: no")
        else:
            phrase = f"{_humanize(key)}: {'yes' if value else 'no'}"
        return Fact(key=f"payload_{key}", value=value, phrase=phrase,
                    json_path=json_path, kind="trigger", salience=1.0)
    if isinstance(value, (int, float)):
        template = _COUNT_PHRASES.get(key, "{v}")
        substitutions = {"v": _num(value) if float(value) == int(value) else f"{value:g}"}
        if key in ("estimated_uplift_pct", "delta_pct", "perf_dip_pct"):
            direction = "down" if value < 0 else "up"
            substitutions["_delta"] = f"{_humanize(payload.get('metric', 'the metric'))} {direction} {abs(round(value * 100))}%"
        if key == "estimated_uplift_pct":
            substitutions["_uplift"] = f"{abs(round(value * 100))}%"
        if key == "renewal_amount":
            substitutions["_money"] = _money(value)
        phrase = template.format(**substitutions)
        return Fact(key=f"payload_{key}", value=value, phrase=phrase,
                    json_path=json_path, kind="trigger", salience=1.0)
    if isinstance(value, str):
        parsed = parse_dt(value)
        if key in _DATE_KEYS and parsed:
            phrase = format_day_month(parsed)
        elif key in _PLAIN_LABELS:
            from harness.internal_vocab import humanize_payload_value

            phrase = _PLAIN_LABELS[key].format(v=humanize_payload_value(key, value))
        else:
            phrase = _humanize(key) + ": " + _humanize(value)
        return Fact(key=f"payload_{key}", value=value, phrase=phrase,
                    json_path=json_path, kind="trigger", salience=1.0)
    if isinstance(value, list):
        if key in ("available_slots", "next_session_options", "slots"):
            labels = _slot_labels(value)
            if labels:
                return Fact(
                    key=f"payload_{key}", value=labels,
                    phrase=" / ".join(labels),
                    json_path=json_path, kind="trigger", salience=1.0,
                )
            return None
        texts = []
        for item in value:
            if isinstance(item, (str, int, float)):
                text = str(item)
                if key == "trends":
                    from harness.internal_vocab import humanize_payload_value

                    text = humanize_payload_value(key, text)
                texts.append(text)
        if texts:
            return Fact(key=f"payload_{key}", value=texts,
                        phrase=", ".join(texts), json_path=json_path,
                        kind="trigger", salience=1.0)
        return None
    return None


def _resolve_digest_item(category: dict | None, item_id: Any) -> tuple[int, dict] | None:
    if not isinstance(item_id, str) or not category:
        return None
    for i, item in enumerate(category.get("digest") or []):
        if isinstance(item, dict) and item.get("id") == item_id:
            return i, item
    return None


def _trigger_facts(category: dict | None, trigger: dict | None,
                   out: _Collector) -> None:
    if not trigger:
        return
    payload = trigger.get("payload")
    if not isinstance(payload, dict) or payload.get("placeholder"):
        return
    for key, value in payload.items():
        if key == "placeholder":
            continue
        if isinstance(value, list):
            fact = _payload_fact(key, value, payload, "[]")
        else:
            fact = _payload_fact(key, value, payload, "")
        if fact:
            out.extend([fact])
            if key in ("available_slots", "next_session_options", "slots", "molecule_list"):
                labels = _slot_labels(value)
                if len(labels) > 1:
                    out.add(
                        f"payload_{key}_count", len(labels),
                        f"{len(labels)} options on the table",
                        f"derived:len(trigger.payload.{key})", "trigger", 1.0,
                    )
            if key == "value_now" and isinstance(value, (int, float)) \
                    and isinstance(payload.get("milestone_value"), (int, float)):
                gap_value = int(payload["milestone_value"] - value)
                out.add(
                    "payload_milestone_gap", gap_value,
                    f"{gap_value} away from the milestone",
                    "derived:milestone_value - value_now", "trigger", 1.0,
                )
        elif isinstance(value, list):
            for j, item in enumerate(value):
                sub = _payload_fact(key, item, payload, f"[{j}]")
                if sub:
                    out.extend([sub])

    # indirection: top_item_id / digest_item_id / alert_id -> category.digest[]
    for id_key in ("top_item_id", "digest_item_id", "alert_id"):
        item_id = payload.get(id_key)
        resolved = _resolve_digest_item(category, item_id)
        if not resolved:
            continue
        i, item = resolved
        title = item.get("title") or ""
        if not title:
            continue
        source = item.get("source") or ""
        out.add(f"digest_resolved_{item_id}", title, title,
                f"category.digest[{i}]", "digest", 1.0, citation=source)
        summary = item.get("summary")
        if summary:
            out.add(f"digest_resolved_{item_id}_summary", summary, str(summary),
                    f"category.digest[{i}].summary", "digest", 0.85, citation=source)
            stat = re.search(r"(\d{1,3})\s?%", str(summary))
            if stat:
                pct_value = int(stat.group(1))
                out.add(f"digest_resolved_{item_id}_summary_stat", pct_value,
                        f"the summary cites {pct_value}%",
                        f"category.digest[{i}].summary", "digest", 0.85, citation=source)
        for numeric in ("trial_n", "credits"):
            num_value = item.get(numeric)
            if isinstance(num_value, (int, float)) and not isinstance(num_value, bool):
                out.add(f"digest_resolved_{item_id}_{numeric}", num_value,
                        f"{int(num_value)} {numeric}",
                        f"category.digest[{i}].{numeric}", "digest", 0.7, citation=source)
        item_date = parse_dt(item.get("date"))
        if item_date:
            out.add(f"digest_resolved_{item_id}_date", item_date.date().isoformat(),
                    format_day_month(item_date),
                    f"category.digest[{i}].date", "digest", 0.7, citation=source)


# ---------------------------------------------------------------------------
# customer
# ---------------------------------------------------------------------------

_SCOPE_INTENTS: dict[str, set[str]] = {
    "refill": {"refill_reminders", "delivery_notifications"},
    "recall": {"recall_reminders", "recall_alerts", "treatment_followup"},
    "appointment": {
        "appointment_reminders", "bridal_package_followup", "program_updates",
        "kids_program_updates", "stylist_specific", "appointment", "trial_followup",
    },
    "promotional": {
        "promotional_offers", "winback_offers", "match_night_specials",
        "lunch_thali_updates", "seasonal_health_content", "health_content",
    },
}
_INTENT_PRIORITY = ("refill", "recall", "appointment", "promotional")


def consent_allows(scope: list[str] | None, intent: str) -> bool:
    """True when the consent scope list covers *intent*."""
    if not scope:
        return False
    allowed = _SCOPE_INTENTS.get(intent, set())
    return bool(allowed & set(scope))


def permitted_intent(customer: dict | None) -> str:
    """The strongest outreach intent this customer consented to.

    Returns one of ``refill|recall|appointment|promotional|none``. A customer
    scoped only to recall/appointment reminders must never receive a
    promotional offer frame.
    """
    scope = ((customer or {}).get("consent") or {}).get("scope") or []
    if not isinstance(scope, list):
        return "none"
    for intent in _INTENT_PRIORITY:
        if consent_allows(scope, intent):
            return intent
    return "none"


def _customer_facts(merchant: dict | None, customer: dict | None,
                    now: datetime, out: _Collector) -> None:
    if not customer:
        return
    base = "customer"
    identity = customer.get("identity") or {}
    name = identity.get("name")
    if name:
        out.add("customer_name", name, name, f"{base}.identity.name", "customer", 0.9)
    pref = identity.get("language_pref")
    if pref:
        out.add("customer_language", pref, f"prefers {pref}", f"{base}.identity.language_pref",
                "customer", 0.5)
    age_band = identity.get("age_band")
    if age_band:
        out.add("customer_age_band", age_band, f"age band {age_band}",
                f"{base}.identity.age_band", "customer", 0.4)
    if identity.get("senior_citizen"):
        out.add("customer_senior", True, "senior citizen", f"{base}.identity.senior_citizen",
                "customer", 0.6)
    state = customer.get("state")
    if state:
        out.add("customer_state", state, f"currently {state}", f"{base}.state", "customer", 0.6)

    relationship = customer.get("relationship") or {}
    visits = relationship.get("visits_total")
    if isinstance(visits, (int, float)):
        out.add("customer_visits_total", visits, f"{int(visits)} visits so far",
                f"{base}.relationship.visits_total", "customer", 0.4)
    ltv = relationship.get("lifetime_value")
    if isinstance(ltv, (int, float)) and ltv > 0:
        out.add("customer_lifetime_value", ltv, f"{_money(ltv)} lifetime value",
                f"{base}.relationship.lifetime_value", "customer", 0.4)
    conditions = relationship.get("chronic_conditions")
    if isinstance(conditions, list) and conditions:
        out.add("customer_chronic_conditions", conditions,
                ", ".join(str(c) for c in conditions),
                f"{base}.relationship.chronic_conditions", "customer", 0.6)
    last_visit = parse_dt(relationship.get("last_visit"))
    if last_visit:
        days = (now.date() - last_visit.date()).days
        if days >= 0:
            out.add("customer_days_since_last_visit", days,
                    f"{days} days since the last visit",
                    "derived:days_since(customer.relationship.last_visit, now)",
                    "customer", 0.7)
            months = days // 30
            if months >= 1:
                out.add("customer_months_since_last_visit", months,
                        f"about {months} months since the last visit",
                        "derived:months_since(customer.relationship.last_visit, now)",
                        "customer", 0.75)
        else:
            out.add("customer_last_visit", relationship.get("last_visit"),
                    f"last visit {format_day_month(last_visit)}",
                    f"{base}.relationship.last_visit", "customer", 0.5)

    preferences = customer.get("preferences") or {}
    slots = preferences.get("preferred_slots")
    if slots:
        out.add("customer_preferred_slots", slots, f"prefers {_humanize(str(slots))}",
                f"{base}.preferences.preferred_slots", "customer", 0.5)
    channel = preferences.get("channel")
    if channel and channel not in ("none_recorded",):
        out.add("customer_channel", channel, f"channel: {_humanize(str(channel))}",
                f"{base}.preferences.channel", "customer", 0.5)
    if "reminder_opt_in" in preferences:
        opted = bool(preferences["reminder_opt_in"])
        out.add("customer_reminder_opt_in", opted,
                "reminders opted in" if opted else "reminders not opted in",
                f"{base}.preferences.reminder_opt_in", "customer", 0.4)
    if preferences.get("delivery_address"):
        out.add("customer_delivery_address", preferences["delivery_address"],
                "delivery address saved", f"{base}.preferences.delivery_address",
                "customer", 0.5)
    # cheapest personalisation points in the dataset — always worth surfacing
    stylist = preferences.get("preferred_stylist")
    if stylist:
        out.add("customer_preferred_stylist", stylist,
                f"prefers stylist {stylist}", f"{base}.preferences.preferred_stylist",
                "customer", 0.6)
    for key in ("family_size", "household_size"):
        value = preferences.get(key)
        if isinstance(value, (int, float)):
            out.add(f"customer_{key}", value, f"household of {int(value)}",
                    f"{base}.preferences.{key}", "customer", 0.45)
    if preferences.get("office_nearby"):
        out.add("customer_office_nearby", True, "works nearby",
                f"{base}.preferences.office_nearby", "customer", 0.5)
    pref_wedding = parse_dt(preferences.get("wedding_date"))
    if pref_wedding:
        out.add("customer_wedding_date", preferences["wedding_date"],
                f"wedding on {format_day_month(pref_wedding)}",
                f"{base}.preferences.wedding_date", "customer", 0.8)

    consent = customer.get("consent") or {}
    scope = consent.get("scope")
    if isinstance(scope, list):
        out.add("consent_scope", scope, "consent: " + ", ".join(str(s) for s in scope),
                f"{base}.consent.scope", "customer", 0.5)


# ---------------------------------------------------------------------------
# main entry points
# ---------------------------------------------------------------------------

def is_placeholder_trigger(trigger: dict | None) -> bool:
    """True when the trigger payload carries no real data (placeholder flag)."""
    payload = (trigger or {}).get("payload")
    return isinstance(payload, dict) and bool(payload.get("placeholder"))


def derive_facts(category: dict | None, merchant: dict | None,
                 trigger: dict | None, customer: dict | None,
                 now: datetime) -> list[Fact]:
    """Extract every computable Fact from the four context layers.

    Absent source fields are skipped silently. Deterministic; pure.
    """
    out = _Collector()
    _identity_facts(merchant, out)
    established = ((merchant or {}).get("identity") or {}).get("established_year")
    if isinstance(established, int) and 1900 < established <= now.year:
        tenure = now.year - established
        if tenure >= 1:
            locality = ((merchant or {}).get("identity") or {}).get("locality") or ""
            phrase = f"{tenure} years in {locality}" if locality else f"{tenure} years in business"
            out.add("tenure_years", tenure, phrase,
                    "derived:now.year - merchant.identity.established_year",
                    "identity", 0.35)
    _subscription_facts(merchant, out)
    _performance_facts(category, merchant, out)
    _cohort_facts(category, merchant, out)
    _category_asset_facts(category, now, out)
    _offer_facts(merchant, out)
    _signal_facts(merchant, out)
    _review_facts(merchant, out)
    _trigger_facts(category, trigger, out)
    _customer_facts(merchant, customer, now, out)
    return out.result()


_BUCKET_ORDER = {"trigger": 0, "digest": 0, "seasonal": 1, "delta": 2,
                 "offer": 3, "review": 4, "cohort": 4, "metric": 4,
                 "customer": 4, "trend": 5, "content": 5, "benchmark": 5,
                 "identity": 5, "subscription": 5}


def rank_facts(facts: list[Fact], trigger: dict | None) -> list[Fact]:
    """Deterministic ranking (no randomness; ties break by key alphabetically).

    1. trigger payload facts + resolved digest facts (salience 1.0) — dates and
       numbers first, then lists, then strings, then booleans
    2. in-season seasonal beats
    3. material peer gaps (|gap| >= 15%)
    4. offers (active before expired), cohort, reviews, momentum
    5. trends, content, benchmarks, identity, subscription
    """
    def trigger_subrank(fact: Fact) -> int:
        if not fact.kind == "trigger":
            return 0
        if isinstance(fact.value, bool):
            return 3
        base_key = fact.key.replace("payload_", "").split("_count")[0]
        if isinstance(fact.value, (int, float)) or base_key in _DATE_KEYS:
            return 0
        if isinstance(fact.value, list):
            return 1
        return 2

    def bucket(fact: Fact) -> int:
        base = _BUCKET_ORDER.get(fact.kind, 5)
        if fact.kind == "digest" and fact.salience < 0.999:
            base = max(base, 3)
        if fact.kind == "seasonal" and fact.salience < 0.999:
            base = 5
        if fact.key.startswith("gap_") and fact.salience < 0.8:
            base = max(base, 4)
        if fact.key.startswith("momentum_"):
            base = max(base, 3)
        return base

    return sorted(
        facts,
        key=lambda f: (bucket(f), trigger_subrank(f), -f.salience, f.key),
    )


def pick_primary(facts: list[Fact], trigger: dict | None, *,
                 merchant: dict | None = None, category: dict | None = None,
                 now: datetime | None = None) -> Fact | None:
    """The single highest-ranked fact — the judge's 'Decision quality' anchor.

    Delegates to :func:`vera.signals.diagnose.diagnose(...).primary` so the
    anchor is the ONE signal a diagnosis picked, not merely the top-ranked
    fact. Falls back to ranked order when diagnosis cannot run.
    """
    try:
        from .diagnose import diagnose

        diagnosis = diagnose(facts, merchant=merchant, category=category,
                             trigger=trigger, now=now)
        if diagnosis is not None and diagnosis.primary is not None:
            return diagnosis.primary
    except Exception:
        pass
    ranked = rank_facts(facts, trigger)
    return ranked[0] if ranked else None


def facts_by_kind(facts: list[Fact], *kinds: str) -> list[Fact]:
    """All facts whose kind is in *kinds*, ranked order."""
    wanted = set(kinds)
    return [f for f in rank_facts(facts, trigger=None) if f.kind in wanted]
