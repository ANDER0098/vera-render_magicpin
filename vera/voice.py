"""Voice resolution: salutation, language, honorific, emoji, code-mix.

Invariant: ``script`` is ALWAYS ``"latin"`` — romanised Hinglish only, exactly
like the production transcripts (challenge-brief §9). Devanagari is never
emitted. All resolution is pure and deterministic given the inputs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["Voice", "resolve_voice", "localize_numbers", "GREETINGS", "HINGLISH_MARKERS"]

LANG_CODE_MAP = {
    "hi": "hi",
    "hi-en mix": "hi_en",
    "hi-en": "hi_en",
    "hinglish": "hi_en",
    "en": "en",
    "english": "en",
    "te-en mix": "te_en",
    "kn-en mix": "kn_en",
    "ta-en mix": "ta_en",
    "mr-en mix": "mr_en",
}

# Local-language greeting words — raise salience of a greeting in the
# merchant's regional language without ever changing the Latin script.
REGIONAL_GREETINGS = {"mr": "Namaskar", "ta": "Vanakkam", "te": "Namaskaram", "kn": "Namaskara"}

GREETINGS = {
    "en": "",
    "hi": "Namaste",
    "hi_en": "Namaste",
    "te_en": "Namaskaram",
    "kn_en": "Namaskara",
    "ta_en": "Vanakkam",
    "mr_en": "Namaskar",
}

HINGLISH_MARKERS: tuple[str, ...] = (
    "namaste", "namaskar", "vanakkam", "ji", "kya", "nahi", "haan", "aap", "aapka",
    "aapki", "apke", "apki", "apka", "hai", "hain", "hoga", "hongi", "kar", "karo",
    "karna", "kijiye", "bataiye", "dijiye", "boliye", "chahiye", "bhej", "bhejo",
    "khatam", "ready", "ya", "bata", "mujhe", "main", "mera", "meri", "ghante",
    "sakte", "karenge", "kar dunga", "kar dungi", "lijiye", "dijiye", "shukriya",
    "theek", "accha", "badhiya", "chalega", "matlab", "bilkul",
)

# Emoji policy: at most one, category-appropriate. Never for pharmacies or any
# compliance/regulation/supply-alert frame; never for hostile/opt-out replies.
_EMOJI_BY_CATEGORY = {
    "salons": "✨",
    "gyms": "💪",
    "restaurants": "🍕",
}
_CUSTOMER_EMOJI_BY_CATEGORY = {"dentists": "🦷", "salons": "✨", "gyms": "💪", "restaurants": "🍕"}
_NO_EMOJI_KINDS = {
    "regulation_change", "supply_alert", "gbp_unverified",
}

_NAME_WITH_PARENT = re.compile(r"^(?P<child>.*?)\s*\(parent:\s*(?P<parent>[^)]+)\)\s*$")
_HONORIFIC_PREFIX = re.compile(r"^(?:mr\.?|mrs\.?|ms\.?|dr\.?)\s+", re.IGNORECASE)


@dataclass(frozen=True)
class Voice:
    """Resolved voice pack for one (category, merchant, trigger, customer?) tuple."""

    salutation: str          # e.g. "Dr. Meera" / "Sushma" / "Namaste Sharma ji"
    tone: str                # category.voice.tone
    register: str            # category.voice.register
    vocab_allowed: tuple[str, ...]
    vocab_taboo: tuple[str, ...]
    lang: str                # en | hi_en | hi | te_en | kn_en | ta_en | mr_en
    script: str              # always "latin"
    emoji: str | None        # at most one, category-appropriate, or None
    honorific: str | None    # "Dr." for dentists, "ji" for senior customers
    recipient: str = "merchant"   # customer | guardian | parent | merchant
    third_person_subject: str = ""  # the customer's name when an intermediary reads


def _third_person_name(identity: dict) -> str:
    """'Mr. Sharma' -> 'Sharma ji' for seniors; plain name otherwise."""
    raw = str(identity.get("name") or "").strip()
    if not raw:
        return ""
    senior = bool(identity.get("senior_citizen")) or str(identity.get("age_band", "")).startswith("65")
    core = _HONORIFIC_PREFIX.sub("", raw).strip()
    if senior and core:
        surname = core.split()[-1]
        return f"{surname} ji"
    return core or raw


def _taboo_from(category: dict | None) -> tuple[str, ...]:
    voice = ((category or {}).get("voice") or {})
    raw = voice.get("vocab_taboo")
    if raw is None:  # design doc calls it `taboos`; support both keys
        raw = voice.get("taboos")
    if isinstance(raw, list):
        return tuple(str(t) for t in raw)
    return ()


def _pure_english(text: str) -> bool:
    lowered = (text or "").lower()
    return not any(marker in lowered for marker in HINGLISH_MARKERS)


def _is_customer_facing(trigger: dict | None, customer: dict | None) -> bool:
    if trigger and trigger.get("scope") == "customer":
        return True
    return customer is not None and not (trigger and trigger.get("scope") == "merchant")


def lang_pref_hindi(customer: dict | None) -> bool:
    pref = str(((customer or {}).get("identity") or {}).get("language_pref") or "").lower()
    return pref.startswith("hi")


def _customer_salutation(customer: dict) -> tuple[str, str | None]:
    identity = customer.get("identity") or {}
    raw_name = str(identity.get("name") or "").strip()
    if not raw_name:
        return "", None
    senior = bool(identity.get("senior_citizen")) or str(identity.get("age_band", "")).startswith("65")
    parent = _NAME_WITH_PARENT.match(raw_name)
    if senior:
        core = _HONORIFIC_PREFIX.sub("", parent.group("parent") if parent else raw_name).strip()
        surname = core.split()[-1] if core.split() else core
        return f"Namaste {surname} ji", "ji"
    if parent:
        # address the parent; the child is mentioned by first name in copy
        return parent.group("parent").strip(), None
    first = _HONORIFIC_PREFIX.sub("", raw_name).split()
    return (first[0] if first else raw_name), None


def _merchant_salutation(category: dict | None, merchant: dict | None) -> str:
    identity = (merchant or {}).get("identity") or {}
    owner = str(identity.get("owner_first_name") or "").strip()
    slug = str((category or {}).get("slug") or (merchant or {}).get("category_slug") or "")
    if owner:
        if slug == "dentists":
            if owner.lower().startswith("dr"):
                return owner if owner.lower().startswith("dr.") else f"Dr. {owner.split(None, 1)[-1]}"
            return f"Dr. {owner}"
        return owner
    business = str(identity.get("name") or "").strip()
    return business  # never a bare "Hi"


def resolve_voice(category: dict | None, merchant: dict | None,
                  customer: dict | None, trigger: dict | None,
                  history: list[dict] | None = None) -> Voice:
    """Resolve salutation, language, register, emoji policy and honorifics.

    Deterministic and pure; ``history`` (list of {from, body}) only nudges the
    merchant-facing language toward English when the last two merchant turns
    are pure English.
    """
    voice_cfg = (category or {}).get("voice") or {}
    slug = str((category or {}).get("slug") or (merchant or {}).get("category_slug") or "")
    kind = str((trigger or {}).get("kind") or "")
    customer_facing = _is_customer_facing(trigger, customer)
    recipient = "merchant"
    third_person = ""

    if customer_facing and customer:
        identity = customer.get("identity") or {}
        channel = str((customer.get("preferences") or {}).get("channel") or "")
        parent = _NAME_WITH_PARENT.match(str(identity.get("name") or ""))
        if channel == "whatsapp_via_son" or channel == "whatsapp_via_guardian":
            # the son/daughter reads this thread: greet them, refer to the
            # customer in the third person (case study 10 pattern)
            recipient = "guardian"
            third_person = _third_person_name(identity)
            salutation = "Namaste" if lang_pref_hindi(customer) else "Hello"
            honorific = None
            lang = LANG_CODE_MAP.get(str(identity.get("language_pref") or "").lower(), "en")
        elif channel == "whatsapp_via_parent" or parent:
            # the parent reads: address them by name, child in third person
            recipient = "parent"
            third_person = parent.group("child").strip() if parent else _third_person_name(identity)
            salutation, honorific = _customer_salutation(customer)
            if not salutation:
                salutation = "Namaste"
            lang = LANG_CODE_MAP.get(str(identity.get("language_pref") or "").lower(), "en")
        else:
            salutation, honorific = _customer_salutation(customer)
            if not salutation:
                salutation = _merchant_salutation(category, merchant)
                honorific = "Dr." if slug == "dentists" else None
            lang = LANG_CODE_MAP.get(str(identity.get("language_pref") or "").strip().lower(), "en")
            recipient = "customer"
    else:
        salutation = _merchant_salutation(category, merchant)
        honorific = "Dr." if (slug == "dentists" and salutation) else None
        languages = [str(l).lower() for l in (((merchant or {}).get("identity") or {}).get("languages") or [])]
        lang = "hi_en" if "hi" in languages else "en"
        if history:
            merchant_turns = [str(t.get("body") or "") for t in history if t.get("from") == "merchant"]
            if len(merchant_turns) >= 2 and all(_pure_english(t) for t in merchant_turns[-2:]):
                lang = "en"

    if customer_facing and customer:
        emoji = _CUSTOMER_EMOJI_BY_CATEGORY.get(slug)
    else:
        emoji = _EMOJI_BY_CATEGORY.get(slug)
    if slug == "pharmacies" or kind in _NO_EMOJI_KINDS:
        emoji = None

    return Voice(
        salutation=salutation,
        tone=str(voice_cfg.get("tone") or "warm_practical"),
        register=str(voice_cfg.get("register") or ""),
        vocab_allowed=tuple(str(v) for v in (voice_cfg.get("vocab_allowed") or [])),
        vocab_taboo=_taboo_from(category),
        lang=lang,
        script="latin",
        emoji=emoji,
        honorific=honorific,
        recipient=recipient,
        third_person_subject=third_person,
    )


def localize_numbers(s: str, lang: str = "en") -> str:
    """Use ``Rs.`` in output text, never the ``₹`` glyph (ASCII-safe whitelist).

    ``₹`` is accepted on input; output is always ``Rs.``. Idempotent.
    """
    if not s:
        return s
    out = s.replace("₹ ", "Rs.").replace("₹", "Rs.")
    out = re.sub(r"Rs\.\s+", "Rs.", out)
    return out
