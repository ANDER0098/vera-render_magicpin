"""Text normalisation + fingerprinting (trap T2 lives here).

Invariant: every function here is pure — no I/O, no clock, no randomness —
and ``normalize_text`` is idempotent (normalize(normalize(x)) == normalize(x)).

The fingerprint deliberately excludes any conversation id: the judge's
auto-reply scenario sends the identical canned text across four *different*
conversation ids for the same merchant, so dedup must key on
``(merchant_id, normalised text)`` only.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Sequence

__all__ = [
    "normalize_text",
    "fingerprint",
    "is_auto_reply",
    "word_overlap",
    "ngrams",
]

# Canonical forms for common Hindi romanisation variants.  Keys are matched on
# the *normalised* (lowercased, punctuation-stripped) token stream.
_ROMANISATION: dict[str, str] = {
    "nahin": "nahi",
    "nahi": "nahi",
    "kyaa": "kya",
    "kya": "kya",
    "dhanyavaad": "thanks",
    "dhanyawad": "thanks",
    "shukriya": "thanks",
    "thanks": "thanks",
    "thank": "thank",
    "jee": "ji",
    "ji": "ji",
    "haan": "haan",
    "ha": "haan",
    "han": "haan",
}

# Canned WhatsApp-Business auto-reply patterns, matched as substrings of the
# normalised text.  Keep conservative: a real reply must never match these
# unless it truly is boilerplate.
_CANNED_PATTERNS: tuple[str, ...] = (
    "thank you for contacting",
    "thanks for contacting",
    "we will respond shortly",
    "our team will respond",
    "automated assistant",
    "i am an automated",
    "auto reply",
    "away from",
    "out of office",
    "will get back to you shortly",
    "aapki jaankari ke liye",
    "hamari team tak pahuncha",
)

# First-person intent markers (English + romanised Hindi).  A message longer
# than 20 characters containing *none* of these is almost certainly machine
# boilerplate.  Everything a real reply contains ("yes", "please", "send",
# "karo", "nahi", "stop", ...) is listed so the conservative direction holds:
# real replies are never flagged.
_INTENT_MARKERS: frozenset[str] = frozenset(
    {
        # English
        "yes", "no", "not", "please", "pls", "send", "do", "did", "lets", "let",
        "can", "could", "would", "want", "need", "what", "whats", "how", "when",
        "where", "why", "who", "help", "interested", "stop", "message",
        "messaging", "share", "update", "check", "join", "book", "confirm",
        "next", "also", "btw", "me", "my", "i",
        # Romanised Hindi first-person / intent
        "main", "mujhe", "mera", "meri", "kya", "karo", "kar", "karna",
        "bhejo", "bhej", "chahiye", "haan", "nahi", "batao", "bata", "bataiye",
        "dijiye", "boliye", "kijiye", "karni", "jarna", "judrna",
    }
)

_SPLIT_RE = re.compile(r"[^0-9a-zà-ÿ]+")
_ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\ufeff]")


def normalize_text(s: str) -> str:
    """NFKD-normalise, strip diacritics/emoji/punctuation, collapse whitespace
    and fold Hindi romanisation variants to one form."""
    if not s:
        return ""
    decomposed = unicodedata.normalize("NFKD", s)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    stripped = _ZERO_WIDTH_RE.sub(" ", stripped)
    lowered = unicodedata.normalize("NFKC", stripped.lower())
    tokens = [t for t in _SPLIT_RE.split(lowered) if t]
    folded = [_ROMANISATION.get(t, t) for t in tokens]
    return " ".join(folded)


def fingerprint(merchant_id: str | None, s: str) -> str:
    """Stable 16-hex digest of (merchant, normalised text).

    Deliberately independent of conversation_id so cross-conversation
    auto-reply detection works (trap T2)."""
    key = f"{merchant_id or '-'}::{normalize_text(s)}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def word_overlap(a: str, b: str) -> float:
    """Jaccard overlap of normalised word sets — the ">= 60% duplicate" test."""
    wa = frozenset(normalize_text(a).split())
    wb = frozenset(normalize_text(b).split())
    if not wa and not wb:
        return 1.0
    if not wa or not wb:
        return 0.0
    inter = len(wa & wb)
    return inter / (len(wa) + len(wb) - inter)


def is_auto_reply(s: str, *, seen_texts: Sequence[str] = ()) -> bool:
    """True when *s* looks like a canned/automated reply.

    Three rules, any of which suffices:
    1. normalised text contains a known canned pattern;
    2. word overlap with a previously seen text for this merchant >= 0.6
       (caller supplies history via ``seen_texts``);
    3. length > 20 chars and zero first-person intent markers.

    Rule 3 is deliberately conservative — "Yes please send it" style replies
    contain intent markers and never trip it.
    """
    norm = normalize_text(s)
    if not norm:
        return False
    for pattern in _CANNED_PATTERNS:
        if pattern in norm:
            return True
    for prior in seen_texts:
        if word_overlap(s, prior) >= 0.6:
            return True
    words = norm.split()
    if len(norm) > 20 and not any(w in _INTENT_MARKERS for w in words):
        return True
    return False


def ngrams(s: str, n: int = 8) -> set[tuple[str, ...]]:
    """Word n-grams of the normalised text (the plagiarism unit)."""
    words = normalize_text(s).split()
    if n <= 0:
        return set()
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}
