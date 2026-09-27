"""Numeral extraction + the grounding whitelist.

Invariant: a numeral found in a composed body is *grounded* iff one of its
canonical variants appears in ``build_whitelist(facts, voice)`` — the set of
every canonical variant of every numeral reachable from the fact list — or it
is an allowed effort/CTA constant in a valid grammatical context.

All matching is ASCII-safe: composed text uses ``Rs.`` (never the ``₹``
glyph), though ``₹`` is accepted on input here so LLM output containing it can
still be validated rather than rejected outright.
"""

from __future__ import annotations

import re

from ..signals.derived import Fact
from ..voice import Voice

__all__ = [
    "NUMERAL_RE",
    "ALLOWED_CONSTANTS",
    "EFFORT_NOUNS",
    "extract_numerals",
    "build_whitelist",
    "numeral_variants",
    "is_allowed_constant",
    "is_reply_token_numeral",
]

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH_NAMES = {
    1: "jan", 2: "feb", 3: "mar", 4: "apr", 5: "may", 6: "jun",
    7: "jul", 8: "aug", 9: "sep", 10: "oct", 11: "nov", 12: "dec",
}
_MON_RE = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t)?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"

# Master numeral pattern — one alternation, tried left to right so ISO dates
# are not shredded into bare integers and batch codes stay atomic.
NUMERAL_RE: re.Pattern = re.compile(
    rf"""
    (?P<iso>\d{{4}}-\d{{2}}-\d{{2}})
  | (?P<batch>\b[A-Za-z]{{2,4}}\d{{2,4}}-\d{{2,6}}\b)
  | (?P<date_dm>(?:\d{{1,2}})(?:st|nd|rd|th)?\s+{_MON_RE})
  | (?P<date_md>{_MON_RE}\s+\d{{1,2}}(?:st|nd|rd|th)?\b)
  | (?P<weekday_date>(?:mon|tues?|wed|thur?s?|fri|sat|sun)[a-z]*\s+\d{{1,2}}\s+{_MON_RE})
  | (?P<currency>(?:rs\.?|₹)\s?\d[\d,]*(?:\.\d+)?)
  | (?P<pct>[+-]?\d[\d,]*(?:\.\d+)?\s?%)
  | (?P<clock_range>\b\d{{1,2}}:\d{{2}}\s?-\s?\d{{1,2}}(?::\d{{2}})?\s?(?:am|pm)?\b)
  | (?P<time>\b\d{{1,2}}(?::\d{{2}})?\s?(?:am|pm)\b)
  | (?P<clock>\b\d{{1,2}}:\d{{2}}\b)
  | (?P<km>\b\d+(?:\.\d+)?\s?km\b)
  | (?P<duration>\b\d+\s?-(?:\s?)?(?:month|week|day|hour|year|min|sec|h)\b
            |\b\d+\s?(?:months?|weeks?|days?|hours?|years?|min(?:ute)?s?|sec(?:ond)?s?|hrs?|ghante|ghanta|h)\b)
  | (?P<num>[+-]?\d[\d,]*(?:\.\d+)?)
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Effort/time anchors: these digits may appear without a backing fact, but
# only when the adjacent token is one of these nouns (context-sensitive rule
# from the spec — bare "10" with no noun is still Tier-1 and must be grounded).
ALLOWED_CONSTANTS: frozenset[str] = frozenset(
    {"1", "2", "3", "5", "10", "15", "20", "30", "45", "60", "90",
     "24", "48", "7", "300", "4", "6", "8", "9", "12"}
)
EFFORT_NOUNS: frozenset[str] = frozenset(
    {
        "min", "mins", "minute", "minutes", "sec", "secs", "second", "seconds",
        "hour", "hours", "hr", "hrs", "h", "ghante", "ghanta", "ghantay",
        "page", "pages", "line", "lines", "day", "days", "week", "weeks",
        "month", "months", "year", "years",
    }
)

_REPLY_RE = re.compile(
    r"\b(?:reply|press|type|dial|tap)\s+(\d+|yes|no|stop|confirm|cancel)\b",
    re.IGNORECASE,
)


def extract_numerals(body: str) -> list[str]:
    """Every numeral-ish token in *body* (raw matched substrings, in order)."""
    if not body:
        return []
    return [m.group(0).strip() for m in NUMERAL_RE.finditer(body)]


def _strip_num(raw: str) -> str:
    """Bare number: drop separators/sign/currency, collapse leading zeros."""
    cleaned = raw.strip().replace(",", "")
    cleaned = re.sub(r"^(?:rs\.?|₹)\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.replace("₹", "").replace("%", "").replace("+", "").strip()
    neg = cleaned.startswith("-")
    if neg:
        cleaned = cleaned[1:]
    if "." in cleaned:
        head, _, tail = cleaned.partition(".")
        cleaned = (head.lstrip("0") or "0") + "." + tail.rstrip("0")
        if cleaned.endswith("."):
            cleaned = cleaned[:-1]
    else:
        cleaned = cleaned.lstrip("0") or "0"
    return ("-" + cleaned) if neg and cleaned != "0" else cleaned


def _variants_date(d: int, m: int, y: int | None) -> set[str]:
    mon3 = _MONTH_NAMES[m]
    full = {f"{mon3} {d}".lower(), f"{d} {mon3}".lower(), f"{d}-{mon3}", f"{mon3}-{d}"}
    if y:
        full |= {
            f"{y:04d}-{m:02d}-{d:02d}",
            f"{d:02d} {mon3} {y}",
            f"{mon3} {d} {y}".lower(),
        }
    return full


def numeral_variants(raw: str) -> set[str]:
    """Canonical variant set for one extracted token.

    A token is grounded iff ``numeral_variants(tok) & whitelist`` is non-empty,
    so both directions of every representation (2.1% vs 0.021, 6pm vs 18:00,
    ISO vs written dates) interconvert here.
    """
    token = raw.strip()
    low = token.lower()
    out: set[str] = set()

    iso = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", low)
    if iso:
        y, m, d = int(iso.group(1)), int(iso.group(2)), int(iso.group(3))
        if 1 <= m <= 12:
            out |= _variants_date(d, m, y)
            out.add(low)
            return out
        out.add(low)
        return out

    if re.fullmatch(r"[a-z]{2,4}\d{2,4}-\d{2,6}", low):
        out.add(low.replace(" ", ""))
        return out

    wd = re.fullmatch(
        rf"(?:mon|tues?|wed|thur?s?|fri|sat|sun)[a-z]*\s+(\d{{1,2}})\s+({_MON_RE})", low
    )
    target = wd
    if target is None:
        target = re.fullmatch(rf"(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MON_RE})", low)
    if target is not None:
        d = int(target.group(1))
        mon = target.group(2)[:3]
        if mon in _MONTHS:
            out |= _variants_date(d, _MONTHS[mon], None)
            return out

    md = re.fullmatch(rf"({_MON_RE})\s+(\d{{1,2}})(?:st|nd|rd|th)?", low)
    if md is not None:
        mon = md.group(1)[:3]
        d = int(md.group(2))
        if mon in _MONTHS:
            out |= _variants_date(d, _MONTHS[mon], None)
            return out

    # clocks and 12h times -> 24h + original
    clock = re.fullmatch(r"(\d{1,2}):(\d{2})", low)
    time12 = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s?(am|pm)", low)
    if clock is not None:
        h, mi = int(clock.group(1)) % 24, clock.group(2)
        out |= {low, f"{h:02d}:{mi}"}
        return out
    if time12 is not None:
        h = int(time12.group(1)) % 12
        if time12.group(3) == "pm":
            h += 12
        mi = time12.group(2) or "00"
        compact = f"{h:02d}:{mi}"
        out |= {low.replace(" ", ""), compact, f"{h}", str(h)}
        return out

    rng = re.fullmatch(r"(\d{1,2}):(\d{2})\s?-\s?(\d{1,2})(?::(\d{2}))?\s?(am|pm)?", low)
    if rng is not None:
        h1 = int(rng.group(1)) % 12
        if rng.group(5) == "pm":
            h1 += 12
        h2 = int(rng.group(3)) % 12
        if rng.group(5) == "pm":
            h2 += 12
        out |= {f"{h1:02d}:{rng.group(2)}", f"{h2:02d}:{rng.group(4) or '00'}"}
        return out

    km = re.fullmatch(r"(\d+(?:\.\d+)?)\s?km", low)
    if km is not None:
        out |= {_strip_num(km.group(1)), low.replace(" ", "")}
        return out

    dur = re.fullmatch(
        r"(\d+)\s?-?\s?(months?|weeks?|days?|hours?|years?|min(?:ute)?s?|sec(?:ond)?s?|ghant[ae]|h)\b.*",
        low,
    )
    if dur is not None:
        out |= {_strip_num(dur.group(1))}
        return out

    # plain number / currency / percent
    num = _strip_num(low)
    out.add(num)
    if num.startswith("-"):
        out.add(num[1:])  # "30" grounds "-30%"
    if "." in num:
        out.add(num.lstrip("-"))
    return out


def _decimal_pct_crossovers(num: str) -> set[str]:
    """0.021 <-> 2.1% both ways: add *100 for |x| < 1, /100 for percent tokens."""
    out: set[str] = set()
    try:
        val = float(num)
    except ValueError:
        return out
    sign = "-" if num.startswith("-") else ""
    if 0 < abs(val) < 1:
        pct = val * 100
        txt = f"{pct:.4f}".rstrip("0").rstrip(".")
        out.add(sign + txt)
    return out


def _fact_source_strings(fact: Fact) -> list[str]:
    sources: list[str] = []
    value = fact.value
    if isinstance(value, bool):
        sources.append("")
    elif isinstance(value, (int, float)):
        sources.append(f"{value:g}")
        if isinstance(value, float) and 0 < abs(value) < 1:
            sources.append(f"{value * 100:g}")
    elif isinstance(value, str):
        sources.append(value)
    elif isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, str):
                sources.append(item)
            elif isinstance(item, dict):
                for v in item.values():
                    if isinstance(v, (str, int, float)) and not isinstance(v, bool):
                        sources.append(f"{v:g}" if isinstance(v, float) else str(v))
    elif isinstance(value, dict):
        for v in value.values():
            if isinstance(v, (str, int, float)) and not isinstance(v, bool):
                sources.append(f"{v:g}" if isinstance(v, float) else str(v))
    if fact.phrase:
        sources.append(fact.phrase)
    if fact.citation:
        sources.append(fact.citation)
    return [s for s in sources if s]


def build_whitelist(facts: list[Fact], voice: Voice) -> set[str]:
    """Canonical numeral set reachable from the fact list (+ voice salutation).

    Every numeral that appears in a ``Fact.value``, ``Fact.phrase`` or
    ``Fact.citation`` contributes all of its canonical variants; decimal
    ratios additionally contribute their percentage form and vice versa.
    """
    whitelist: set[str] = set()
    for fact in facts:
        for source in _fact_source_strings(fact):
            for token in extract_numerals(str(source)):
                variants = numeral_variants(token)
                whitelist |= variants
                for variant in variants:
                    whitelist |= _decimal_pct_crossovers(variant)
    if voice and voice.salutation:
        for token in extract_numerals(voice.salutation):
            whitelist |= numeral_variants(token)
    return whitelist


def is_reply_token_numeral(body: str, numeral_raw: str) -> bool:
    """True when the numeral is the digit of a Reply-style CTA token."""
    for match in _REPLY_RE.finditer(body):
        if match.group(1).isdigit():
            if _strip_num(match.group(1)) == _strip_num(numeral_raw):
                return True
    return False


def is_allowed_constant(body: str, numeral_raw: str) -> bool:
    """Context-sensitive effort-anchor allowance.

    The numeral is allowed only when it is an ALLOWED_CONSTANTS value whose
    immediately neighbouring token is an effort/time noun (``min``, ``hour``,
    ``ghante``, ``page``, ...) — or it is the digit of a Reply CTA token.
    """
    stripped = _strip_num(numeral_raw)
    if stripped not in ALLOWED_CONSTANTS:
        return is_reply_token_numeral(body, numeral_raw)
    for match in re.finditer(re.escape(numeral_raw), body):
        start, end = match.start(), match.end()
        before = body[max(0, start - 24) : start].lower().replace(",", " ")
        after = body[end : end + 24].lower().replace(",", " ")
        prev_word = re.findall(r"[a-z]+", before.split()[-1] if before.split() else "")
        next_word = re.findall(r"[a-z]+", after.split()[0] if after.split() else "")
        prev = prev_word[-1] if prev_word else ""
        nxt = next_word[0] if next_word else ""
        # "2-min", "10 min", "min ... 10": check both sides, incl. hyphen glue
        glued = re.findall(rf"([a-z]+)[-–]?$", before.replace(" ", ""))
        glued_next = re.findall(r"^[-–]?([a-z]+)", after.lstrip())
        candidates = {prev, nxt}
        if glued:
            candidates.add(glued[-1])
        if glued_next:
            candidates.add(glued_next[0])
        if candidates & EFFORT_NOUNS:
            return True
    return is_reply_token_numeral(body, numeral_raw)
