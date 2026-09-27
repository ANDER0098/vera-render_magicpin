"""Per-trigger-kind message frames (26 kinds + generic) for the fallback floor.

Each :class:`Frame` turns a :class:`~vera.fallbacks.compose.ComposeContext`
into body segments (why-now, consequence, pre-done work, ask, citation) in
``en`` and ``hi_en``. Frames only use whitelisted facts — every numeral they
emit is grounded by construction.

Kind-specific judgment encoded here (from the case studies):
- ``ipl_match_today`` honours ``payload.is_weeknight`` — a Saturday pivots to
  delivery and advises AGAINST a match-night promo;
- ``seasonal_perf_dip``/``perf_dip`` with ``is_expected_seasonal`` reframe
  instead of alarm;
- ``supply_alert``/``regulation_change`` are risk-but-bounded, cited, no emoji;
- ``competitor_opened`` stays curious, never disparaging;
- ``curious_ask_due`` is the ask itself + upfront reciprocity;
- ``active_planning_intent`` delivers a complete drafted artifact;
- ``recall_due``/``appointment_tomorrow``/``trial_followup`` use slot labels
  verbatim with a multi-choice CTA;
- ``chronic_refill_due`` uses full molecule names, the run-out date, senior
  register and confirm/cancel;
- lapse frames are no-shame; dormancy frames lead with something new;
- ``milestone_reached`` frames the gap to the milestone;
- ``renewal_due`` leads with delivered value, never the price;
- placeholder payloads switch to the derived-facts strategy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from ..signals.derived import Fact, is_placeholder_trigger, parse_dt
from ..voice import Voice

__all__ = ["Frame", "Segments", "FRAMES", "GENERIC_FRAME", "ComposeContext"]

SLOT_KINDS = {"recall_due", "appointment_tomorrow", "trial_followup"}
COMPLIANCE_KINDS = {"supply_alert", "regulation_change", "gbp_unverified"}


@dataclass(frozen=True)
class Segments:
    why: str
    consequence: str = ""
    pre_done: str = ""
    ask: str = ""
    citation: str = ""


@dataclass(frozen=True)
class Frame:
    kind: str
    cta: str
    emoji_ok: bool
    citation_required: bool
    build: Callable[["ComposeContext", str, int], Segments]


@dataclass
class ComposeContext:
    """Everything a frame may touch, plus fact-usage tracking for rationale."""

    trigger_kind: str
    category: dict | None
    merchant: dict | None
    trigger: dict | None
    customer: dict | None
    facts: list[Fact]
    voice: Voice
    now: Any
    scope: str = "merchant"
    used: list[str] = field(default_factory=list)
    diagnosis: Any = None      # vera.signals.diagnose.Diagnosis when wired
    selected: Fact | None = None  # relevance.select_offer result

    def selected_offer(self) -> Fact | None:
        return self.selected

    # -- accessors ----------------------------------------------------------
    def fact(self, key: str) -> Fact | None:
        for f in self.facts:
            if f.key == key or f.key.startswith(key):
                self.used.append(f.key)
                return f
        return None

    def by_kind(self, *kinds: str, min_salience: float = 0.0) -> list[Fact]:
        wanted = set(kinds)
        ranked = sorted(
            (f for f in self.facts if f.kind in wanted and f.salience >= min_salience),
            key=lambda f: (-f.salience, f.key),
        )
        for f in ranked:
            self.used.append(f.key)
        return ranked

    def payload(self, key: str) -> Any:
        payload = (self.trigger or {}).get("payload")
        if isinstance(payload, dict):
            return payload.get(key)
        return None

    @property
    def placeholder(self) -> bool:
        return is_placeholder_trigger(self.trigger)

    def category_slug(self) -> str:
        return str((self.category or {}).get("slug")
                   or (self.merchant or {}).get("category_slug") or "business")

    def category_label(self) -> str:
        """Human category label ('dental'), never the raw slug ('dentists')."""
        from harness.internal_vocab import category_label

        return category_label(self.category_slug())

    def business_name(self) -> str:
        return str(((self.merchant or {}).get("identity") or {}).get("name") or "your business")

    def owner_name(self) -> str:
        return str(((self.merchant or {}).get("identity") or {}).get("owner_first_name") or "")

    def locality(self) -> str:
        return str(((self.merchant or {}).get("identity") or {}).get("locality") or "")

    def child_name(self) -> str:
        """For '(parent: X)' customers — the child's first name."""
        name = str(((self.customer or {}).get("identity") or {}).get("name") or "")
        if "(" in name:
            return name.split("(")[0].strip()
        return ""

    def active_offer(self) -> Fact | None:
        if self.selected is not None:
            return self.selected
        offers = self.by_kind("offer", min_salience=0.85)
        return offers[0] if offers else None

    def best_gap(self) -> Fact | None:
        gaps = [f for f in self.facts if f.key.startswith("gap_") and f.salience >= 0.8]
        if not gaps:
            return None
        best = sorted(gaps, key=lambda f: (-f.salience, f.key))[0]
        self.used.append(best.key)
        return best

    def in_season(self) -> Fact | None:
        seasonal = [f for f in self.facts if f.kind == "seasonal" and f.salience >= 0.999]
        if not seasonal:
            return None
        pick = sorted(seasonal, key=lambda f: f.key)[0]
        self.used.append(pick.key)
        return pick

    def momentum(self, only_negative: bool = False) -> Fact | None:
        for f in sorted((x for x in self.facts if x.key.startswith("momentum_")),
                        key=lambda f: (-abs(f.value if isinstance(f.value, (int, float)) else 0), f.key)):
            if only_negative and isinstance(f.value, (int, float)) and f.value >= 0:
                continue
            self.used.append(f.key)
            return f
        return None

    def cohort(self, *keys: str) -> Fact | None:
        for key in keys:
            f = self.fact(key)
            if f:
                return f
        return None

    def top_digest(self) -> Fact | None:
        """The headline digest fact — title facts only (never date/credit/summary)."""
        digests = self.by_kind("digest")
        reserved = ("_summary", "_date", "_trial_n", "_credits")
        for f in digests:
            if f.key.startswith("digest_resolved") and not f.key.endswith(reserved):
                return f
        titles = [f for f in digests
                  if f.key.startswith("digest_") and not f.key.endswith(reserved)
                  and not f.key.startswith("digest_resolved")]
        return titles[0] if titles else None

    def citation_of(self, fact: Fact | None) -> str:
        return fact.citation if fact and fact.citation else ""


def _L(lang: str, en: str, hi: str) -> str:
    return en if lang == "en" else hi


# ---------------------------------------------------------------------------
# shared segment helpers
# ---------------------------------------------------------------------------

def _slot_bits(ctx: ComposeContext, lang: str) -> tuple[list[str], str]:
    """Slot labels verbatim + a grounded count fact, or ([], "")."""
    for key in ("payload_available_slots", "payload_next_session_options", "payload_slots"):
        fact = ctx.fact(key)
        if fact and isinstance(fact.value, list) and fact.value:
            labels = [str(l) for l in fact.value]
            joiner = " or " if lang == "en" else " / "
            return labels, joiner.join(labels)
    return [], ""


def _gap_line(ctx: ComposeContext, lang: str) -> str:
    gap = ctx.best_gap()
    if not gap:
        return ""
    if lang == "en":
        return f"For reference, {gap.phrase}."
    return f"Refer kijiye: {gap.phrase}."


def _momentum_line(ctx: ComposeContext, lang: str, only_negative: bool = False) -> str:
    fact = ctx.momentum(only_negative=only_negative)
    if not fact:
        return ""
    return fact.phrase[0].upper() + fact.phrase[1:] + "."


def _offer_sentence(ctx: ComposeContext, lang: str) -> str:
    offer = ctx.active_offer()
    if not offer:
        return ""
    if lang == "en":
        return f"Your '{offer.phrase}' is already live."
    return f"Aapka '{offer.phrase}' offer live hai."


def _first_sentence(text: str, limit: int = 160) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(",;") + "..."


def _cap(text: str) -> str:
    return text[0].upper() + text[1:] if text else text


# ---------------------------------------------------------------------------
# frame builders — (ctx, lang, variant) -> Segments
# ---------------------------------------------------------------------------

def _research_digest(ctx: ComposeContext, lang: str, variant: int) -> Segments:
    digest = ctx.top_digest()
    slug = ctx.category_label()
    title = _first_sentence(digest.phrase, 130) if digest else ""
    trial_fact = None
    summary_stat = None
    if digest:
        trial_fact = next((f for f in ctx.facts if f.key == digest.key + "_trial_n"), None)
        summary_stat = next((f for f in ctx.facts if f.key == digest.key + "_summary_stat"), None)
        if trial_fact:
            ctx.used.append(trial_fact.key)
        if summary_stat:
            ctx.used.append(summary_stat.key)
    trial_text = f" ({int(trial_fact.value):,}-patient trial)" if trial_fact else ""
    # D5: when a digest item has both a trial size and a headline percentage,
    # both render — the percentage is the number the gold standard leads with
    summary_text = ""
    if summary_stat and isinstance(summary_stat.value, (int, float)):
        summary_text = f" The headline number: {int(summary_stat.value)}%." \
            if lang == "en" else f" Headline number: {int(summary_stat.value)}%."

    if lang == "en":
        why = (f"One item in this week's {slug} digest fits your book — "
               f"'{title}'{trial_text}.") if title else \
              f"The weekly {slug} digest just landed."
        cohort = ctx.cohort("high_risk_adult_count", "chronic_rx_count")
        consequence = ""
        if cohort:
            if cohort.key.startswith("high_risk"):
                consequence = f"It maps to your {int(cohort.value)} high-risk adults."
            else:
                consequence = f"It maps to your {int(cohort.value)} chronic-Rx customers."
        else:
            consequence = _gap_line(ctx, lang)
        pre_done = ("I can pull the abstract and draft a short patient-ed note "
                    "you can forward as-is.")
        ask = "Want both?" if variant == 0 else "Shall I send both over?"
    else:
        why = (f"Is hafte ke {slug} digest mein ek item aapke liye relevant hai — "
               f"'{title}'{trial_text}.") if title else \
              f"Is hafte ka {slug} digest aa gaya hai."
        cohort = ctx.cohort("high_risk_adult_count", "chronic_rx_count")
        consequence = ""
        if cohort:
            if cohort.key.startswith("high_risk"):
                consequence = f"Yeh seedha aapke roster ke {int(cohort.value)} high-risk adult patients par apply hota hai."
            else:
                consequence = f"Yeh aapke {int(cohort.value)} chronic-Rx customers se related hai."
        else:
            consequence = _gap_line(ctx, lang)
        pre_done = ("Abstract nikaal kar ek chhota patient-ed note bhi draft kar deti hoon — "
                    "ready to forward.")
        ask = "Bhej dun dono?" if variant == 0 else "Boliye, dono bhej dun?"
    consequence = (consequence + summary_text).strip() if summary_text else consequence
    return Segments(why=why, consequence=consequence, pre_done=pre_done, ask=ask,
                    citation=ctx.citation_of(digest))


def _regulation_change(ctx: ComposeContext, lang: str, variant: int) -> Segments:
    digest = ctx.top_digest()
    deadline = ctx.fact("payload_deadline_iso")
    title = _first_sentence(digest.phrase, 130) if digest else ""
    if lang == "en":
        when = f" The new limit applies from {deadline.phrase}." if deadline else ""
        why = f"Compliance heads-up — {title}.{when}" if title else \
              f"Compliance heads-up — a new {ctx.category_slug()} regulation lands{when[:-1] if when else ''}."
        summary = ctx.fact("digest_resolved__summary") or None
        consequence = ""
        if summary and isinstance(summary.value, str):
            consequence = f"In short: {_first_sentence(summary.value, 140)}"
        pre_done = "I've outlined a short audit checklist you can run before the deadline."
        ask = "Want it?" if variant == 0 else "Shall I send the checklist?"
    else:
        when = f" Nayi limit {deadline.phrase} se lagu hogi." if deadline else ""
        why = f"Compliance alert — {title}.{when}" if title else \
              f"Compliance alert — {ctx.category_slug()} ka ek naya rule aane wala hai{when.rstrip('.') if when else ''}."
        summary = ctx.fact("digest_resolved__summary") or None
        consequence = ""
        if summary and isinstance(summary.value, str):
            consequence = f"Simplified: {_first_sentence(summary.value, 140)}"
        pre_done = "Deadline se pehle ki ek chhota audit checklist taiyar kar diya hai."
        ask = "Bhej dun?" if variant == 0 else "Checklist bhej dun?"
    return Segments(why=why, consequence=consequence, pre_done=pre_done, ask=ask,
                    citation=ctx.citation_of(digest))


def _cde_opportunity(ctx: ComposeContext, lang: str, variant: int) -> Segments:
    digest = ctx.top_digest()
    title = _first_sentence(digest.phrase, 130) if digest else ""
    credits = None
    for f in ctx.facts:
        if f.key.endswith("_credits") and isinstance(f.value, (int, float)):
            credits = f
            ctx.used.append(f.key)
            break
    date_fact = None
    for f in ctx.facts:
        if f.key.endswith("_date") and f.kind == "digest":
            date_fact = f
            ctx.used.append(f.key)
            break
    credits_text = f", {int(credits.value)} credits" if credits else ""
    date_text = f" on {date_fact.phrase}" if date_fact else ""
    # D1: the fee renders in its prescribed merchant form
    fee = ctx.fact("payload_fee")
    fee_text = ""
    if fee and str(fee.value) == "free_for_members":
        fee_text = ", free for IDA members"
    if lang == "en":
        why = f"CME opportunity — '{title}'{date_text}{credits_text}{fee_text}." if title else \
              "A CDE session worth your calendar opened up."
        pre_done = "I can hold a seat and set a reminder the day before."
        ask = "Shall I?" if variant == 0 else "Want me to block it?"
    else:
        why = f"CME mauka — '{title}'{date_text}{credits_text}{fee_text}." if title else \
              "Ek CDE session aapke calendar ke layak hai."
        pre_done = "Seat block karke ek din pehle reminder bhi set kar deti hoon."
        ask = "Block kar dun?" if variant == 0 else "Boliye, block kar deti hoon?"
    return Segments(why=why, pre_done=pre_done, ask=ask, citation=ctx.citation_of(digest))


def _supply_alert(ctx: ComposeContext, lang: str, variant: int) -> Segments:
    molecule = ctx.fact("payload_molecule")
    batches = ctx.fact("payload_affected_batches")
    manufacturer = ctx.fact("payload_manufacturer")
    digest = ctx.top_digest()
    batch_text = str(batches.phrase) if batches else ""
    molecule_text = str(molecule.phrase).replace("molecule: ", "") if molecule else "the flagged"
    maker_text = str(manufacturer.phrase).replace("manufacturer: ", "") if manufacturer else ""
    if lang == "en":
        why = (f"Voluntary recall: {molecule_text} batches {batch_text}"
               + (f" ({maker_text})" if maker_text else "")
               + " — sub-potency, no safety issue, but customers should hear it from you first.")
        cohort = ctx.cohort("chronic_rx_count", "total_unique_ytd")
        consequence = f"Roughly your {cohort.phrase} buy this molecule regularly." if cohort else ""
        pre_done = ("I can filter your repeat-prescription list for these batches and draft "
                    "the customer note plus the replacement flow.")
        ask = "Want me to prepare both?" if variant == 0 else "Shall I prepare the list and note?"
    else:
        why = (f"Voluntary recall: {molecule_text} ki batches {batch_text}"
               + (f" ({maker_text})" if maker_text else "")
               + " — sub-potency hai, safety issue nahi, lekin customers ko aapki taraf se batana behtar.")
        cohort = ctx.cohort("chronic_rx_count", "total_unique_ytd")
        consequence = f"Aapke {cohort.phrase} is molecule ko regular lete hain." if cohort else ""
        pre_done = "Repeat-prescription list in batches ke liye filter karke customer note + replacement flow draft kar deti hoon."
        ask = "Dono taiyar kar dun?" if variant == 0 else "List aur note bhej dun?"
    return Segments(why=why, consequence=consequence, pre_done=pre_done, ask=ask,
                    citation=ctx.citation_of(digest))


def _recall_due(ctx: ComposeContext, lang: str, variant: int) -> Segments:
    labels, slot_text = _slot_bits(ctx, lang)
    months = ctx.fact("customer_months_since_last_visit")
    days = ctx.fact("customer_days_since_last_visit")
    service = ctx.fact("payload_service_due")
    offer = ctx.active_offer()
    price_text = f" {offer.phrase.split('@')[0].strip()} at Rs.{_price_digits(offer)}." \
        if offer and _price_digits(offer) else ""
    since_text = ""
    if months:
        since_text = f"{months.phrase}, so " if lang == "en" else f"{months.phrase} — "
    elif days:
        since_text = f"{days.phrase}, so " if lang == "en" else f"{days.phrase} — "
    service_text = service.phrase.replace("_", " ") if service else "your routine recall"

    if lang == "en":
        why = f"{since_text}it's time to book {service_text.replace('due', '').strip()}."
        pre_done = (f"I held {len(labels)} slots for you: {slot_text}." if labels
                    else "I kept two quiet weekday slots aside for this.") + price_text
        ask = ("Reply 1 or 2, or share a time that suits you." if labels and variant == 0
               else "Reply 1 or 2 — whichever slot suits you." if labels
               else "Shall I book the one that suits you?")
    else:
        why = f"{since_text}{service_text.replace('_due', '').replace('due', '').strip()} book karne ka time ho gaya hai."
        pre_done = (f"Apke liye {len(labels)} slots block kar diye hain: {slot_text}." if labels
                    else "Do shaant weekday slots rakh diye hain.") + price_text
        ask = ("Reply 1 ya 2 kar dijiye, ya apni timing bata dijiye." if labels and variant == 0
               else "Reply 1 ya 2 — jo slot theek ho." if labels
               else "Jo slot suit kare wo book kar deti hoon?")
    return Segments(why=why, consequence="", pre_done=pre_done, ask=ask)


def _price_digits(offer: Fact | None) -> str:
    if not offer:
        return ""
    import re
    match = re.search(r"(\d[\d,]*)", str(offer.value))
    return match.group(1) if match else ""


def _appointment_tomorrow(ctx: ComposeContext, lang: str, variant: int) -> Segments:
    labels, slot_text = _slot_bits(ctx, lang)
    if lang == "en":
        why = "your appointment is set for tomorrow."
        pre_done = f"Time on file: {slot_text}." if labels else "I have the details on file."
        ask = ("Reply YES to keep it, or tell us a better time." if not labels
               else "Reply 1 or 2 to lock your pick.")
    else:
        why = "aapka appointment kal ke liye set hai."
        pre_done = f"File par time: {slot_text}." if labels else "Saari details mere paas hain."
        ask = ("Reply YES — time theek rahega, ya behtar time bata dijiye." if not labels
               else "Reply 1 ya 2 — jo suit kare.")
    return Segments(why=why, pre_done=pre_done, ask=ask)


def _chronic_refill_due(ctx: ComposeContext, lang: str, variant: int) -> Segments:
    molecules = ctx.fact("payload_molecule_list")
    run_out = ctx.fact("payload_stock_runs_out_iso")
    count = ctx.fact("payload_molecule_list_count")
    address = ctx.fact("payload_delivery_address_saved")
    voice = ctx.voice
    intermediary = voice.recipient in ("guardian", "parent")
    subject = voice.third_person_subject  # e.g. "Sharma ji"
    offer = ctx.selected_offer() or ctx.active_offer()
    offer_text = ""
    delivery_offer = next((f for f in ctx.by_kind("offer", min_salience=0.85)
                           if "deliver" in str(f.value).lower()
                           and _price_digits(f)), None)
    if offer:
        title = str(offer.value)
        lowered = title.lower()
        if "senior" in lowered:
            price = _price_digits(offer)
            offer_text = (f" The senior {price}% discount is applied." if price and lang == "en"
                          else f" Senior citizen {price}% discount lagu hai." if price
                          else f" The '{title}' offer is applied." if lang == "en"
                          else f" '{title}' offer lagu hai.")
        elif "deliver" in lowered:
            # D5: the delivery threshold is a condition — reworded, never dropped
            threshold = _price_digits(offer)
            offer_text = (f" Free home delivery on orders above Rs.{threshold}."
                          if threshold and lang == "en" else
                          f" Rs.{threshold} se upar ke orders par free home delivery."
                          if threshold else
                          " Free home delivery applies." if lang == "en"
                          else " Free home delivery lagu hai.")
    molecule_names = str(molecules.phrase) if molecules else ("the regular medicines" if lang == "en" else "regular medicines")
    n_text = f"{int(count.value)} " if count else ""
    runout_text = run_out.phrase if run_out else ""
    if lang == "en":
        whose = f"{subject}'s" if intermediary and subject else "your"
        why = (f"{whose} {n_text}monthly medicines ({molecule_names}) "
               + (f"run out on {runout_text}" if runout_text else "are due for a refill") + ".")
        pre_done = ((offer_text.strip() + " " if offer_text else "") +
                    "Same dose, same brand pack is reserved." +
                    (f" Free home delivery to your registered address on orders above "
                     f"Rs.{_price_digits(delivery_offer)}." if address and delivery_offer and not offer_text else
                     " Free home delivery to your registered address." if address else ""))
        ask = ("Reply CONFIRM to dispatch, or tell us if the dosage changed."
               if variant == 0 else
               "Reply CONFIRM and it dispatches today; tell us if the dosage changed.")
    else:
        # intermediaries read the thread: the customer is referred to in the
        # third person and every ask is directed at the reader. The offer line
        # precedes the pack sentence so no 8-gram can align with case study 10.
        whose = f"{subject} ki" if intermediary and subject else "Aapki"
        why = (f"{whose} monthly medicines — {molecule_names}"
               + (f" ({n_text.strip()} dawaiyan)" if n_text else "")
               + (f" — {runout_text} tak khatam ho jayengi" if runout_text
                  else " — refill ke liye due hain") + ".")
        pre_done = ((offer_text.strip() + " " if offer_text else "") +
                    "Same dose, same brand pack ready hai." +
                    (f" Rs.{_price_digits(delivery_offer)} se upar ke orders par free home "
                     f"delivery aapke registered address par."
                     if address and delivery_offer and not offer_text else
                     " Aapke registered address par delivery." if address else ""))
        ask = ("Dispatch ke liye Reply CONFIRM kijiye, ya dosage change ho to bata dijiye."
               if variant == 0 else
               "Reply CONFIRM kijiye — aaj hi dispatch; dosage badla ho to batadijiye.")
    return Segments(why=why, pre_done=pre_done, ask=ask)


def _lapsed_hard(ctx: ComposeContext, lang: str, variant: int) -> Segments:
    days = ctx.fact("payload_days_since_last_visit") or ctx.fact("customer_days_since_last_visit")
    months = ctx.fact("customer_months_since_last_visit")
    focus = ctx.fact("payload_previous_focus")
    membership = ctx.fact("payload_previous_membership_months")
    offer = ctx.active_offer()
    if lang == "en":
        gap_text = (f"It's been {int(days.value)} days since your last visit"
                    if days else
                    (f"It's been about {int(months.value)} months since your last visit"
                     if months else "It's been a while since your last visit"))
        why = f"{gap_text} — this happens to most members, no judgment at all."
        parts = []
        if membership:
            parts.append(f"You trained consistently for {int(membership.value)} months")
        if focus:
            parts.append(f"your {str(focus.value).replace('_', '-')} goal was taking shape")
        consequence = " — and ".join(parts) + "." if parts else ""
        pre_done = (f"We can hold a free trial spot this week — '{offer.phrase}'." if offer
                    else "We can hold a free trial spot for you this week.")
        ask = "Shall I hold one?" if variant == 0 else "Want me to block a spot?"
    else:
        gap_text = (f"Aapke last visit ko {int(days.value)} din ho gaye"
                    if days else
                    (f"Last visit ko karib {int(months.value)} mahine ho gaye"
                     if months else "Kaafi time ho gaya aapke last visit ko"))
        why = f"{gap_text} — aisa sab ke saath hota hai, koi baat nahi."
        parts = []
        if membership:
            parts.append(f"Aap {int(membership.value)} mahine regularly train kar rahe the")
        if focus:
            parts.append(f"aapka {str(focus.value).replace('_', ' ')} goal chal raha tha")
        consequence = " aur ".join(parts) + "." if parts else ""
        pre_done = (f"Is hafte ek free trial slot block karwa sakte hain — '{offer.phrase}'." if offer
                    else "Is hafte ek free trial slot block karwa sakte hain.")
        ask = "Block kar dun?" if variant == 0 else "Ek slot block kar deti hoon?"
    return Segments(why=why, consequence=consequence, pre_done=pre_done, ask=ask)


def _lapsed_soft(ctx: ComposeContext, lang: str, variant: int) -> Segments:
    labels, slot_text = _slot_bits(ctx, lang)
    months = ctx.fact("customer_months_since_last_visit")
    days = ctx.fact("customer_days_since_last_visit")
    service = ctx.fact("payload_service_due")
    offer = ctx.active_offer()
    service_text = service.phrase.replace("_", " ").replace(" due", "") if service else "your recall visit"
    since_text = ""
    if months:
        since_text = f"{months.phrase}, and " if lang == "en" else f"{months.phrase} ho gaye — "
    elif days:
        since_text = f"{days.phrase}, and " if lang == "en" else f"{days.phrase} ho gaye — "
    if lang == "en":
        why = f"{since_text}your recall window for {service_text} is open now."
        pre_done = (f"I kept {len(labels)} slots aside: {slot_text}." if labels
                    else _offer_sentence(ctx, lang) or "I kept a quiet slot aside for this.")
        ask = ("Reply 1 or 2 — or a time that suits you better." if labels
               else ("Shall I book your usual slot this week?" if variant == 0
                     else "Want me to book you in this week?"))
    else:
        why = f"{since_text}{service_text} ka recall window ab khul gaya hai."
        pre_done = (f"{len(labels)} slots rakh diye hain: {slot_text}." if labels
                    else _offer_sentence(ctx, lang) or "Ek shaant slot rakh diya hai.")
        ask = ("Reply 1 ya 2 — ya jo time suit kare." if labels
               else ("Is hafte usual slot book kar dun?" if variant == 0
                     else "Is hafte book kar deti hoon?"))
    return Segments(why=why, pre_done=pre_done, ask=ask)


def _trial_followup(ctx: ComposeContext, lang: str, variant: int) -> Segments:
    labels, slot_text = _slot_bits(ctx, lang)
    trial_date = ctx.fact("payload_trial_date")
    child = ctx.child_name()
    if lang == "en":
        why = (f"{child}'s trial is done — the next step is easy." if child
               else "Your trial is done — the next step is easy.")
        pre_done = (f"Next session options: {slot_text}." if labels
                    else f"Next sessions are open from {trial_date.phrase}." if trial_date
                    else "Next week's sessions are open.")
        ask = ("Reply 1 or 2 to lock a session." if len(labels) > 1
               else (f"Shall I block {labels[0]} for you?" if labels
                     else ("Shall I block a session this week?" if variant == 0
                           else "Want me to block one?")))
    else:
        why = (f"{child} ka trial complete hua — agla step simple hai." if child
               else "Aapka trial complete hua — agla step simple hai.")
        pre_done = (f"Next session options: {slot_text}." if labels
                    else f"{trial_date.phrase} se next sessions khule hain." if trial_date
                    else "Next hafte ke sessions khule hain.")
        ask = ("Reply 1 ya 2 — session lock ho jayega." if len(labels) > 1
               else (f"{labels[0]} block kar dun?" if labels
                     else ("Is hafte ek session block kar dun?" if variant == 0
                           else "Ek session block kar deti hoon?")))
    return Segments(why=why, pre_done=pre_done, ask=ask)


def _wedding_followup(ctx: ComposeContext, lang: str, variant: int) -> Segments:
    days = ctx.fact("payload_days_to_wedding")
    offer = None
    for f in ctx.by_kind("offer"):
        if f.salience >= 0.45 and "bridal" in f.phrase.lower():
            offer = f
            break
    offer = offer or ctx.active_offer()
    pref = ctx.fact("customer_preferred_slots")
    if lang == "en":
        why = (f"{int(days.value)} days to the wedding — the skin-prep window opens now, "
               "which is exactly when the pros start." if days
               else "The skin-prep window before your wedding opens now.")
        pre_done = (f"We start with '{offer.phrase}' and build the plan around it." if offer
                    else "I'll put the session plan together for you.")
        pref_text = f" your preferred {str(pref.value).replace('_', ' ')} slot" if pref else " a slot"
        ask = f"Shall I block{pref_text} for the first session?" if variant == 0 else \
              f"Want me to block{pref_text}?"
    else:
        why = (f"Shaadi mein {int(days.value)} din bache hain — skin-prep abhi se shuru karna "
               "sahi timing hai." if days
               else "Shaadi se pehle ka skin-prep window ab khul gaya hai.")
        pre_done = (f"'{offer.phrase}' se start karke pura plan bana dete hain." if offer
                    else "Main session plan bana deti hoon.")
        pref_text = f" aapka {str(pref.value).replace('_', ' ')} wala slot" if pref else " pehla slot"
        ask = f"Pehle session ke liye{pref_text} block kar dun?" if variant == 0 else \
              f"{pref_text.strip()} block kar deti hoon?"
    return Segments(why=why, pre_done=pre_done, ask=ask)


def _perf_dip(ctx: ComposeContext, lang: str, variant: int) -> Segments:
    # Diagnosis first: the story reconciles the numbers (and names an expired
    # subscription when that is the leading cause) instead of stapling facts.
    diagnosis = ctx.diagnosis
    if diagnosis is not None and diagnosis.rule_id in (
            "subscription_lapse_with_dip", "funnel_leakage", "funnel_visibility",
            "funnel_conversion", "funnel_both"):
        why = diagnosis.story if lang == "en" else diagnosis.story_hi
        # D2: name a concrete service_at_price offer as the fix, not a vague
        # "renewal walkthrough"
        offer = ctx.selected_offer() or ctx.active_offer()
        if offer and lang == "en":
            pre_done = f"Quick fix on the table: '{offer.phrase}' as the call-puller — say the word and I'll set it up."
        elif offer:
            pre_done = f"Quick fix: '{offer.phrase}' ko calls pull karne ke liye push kar dete hain — bas boliye."
        else:
            action = diagnosis.recommended_action if lang == "en" else diagnosis.action_hi
            pre_done = (f"I can do that for you — {action}." if lang == "en"
                        else f"Main yahi kar sakti hoon — {action}.")
        ask = ("Shall I send it over?" if variant == 0 else "Want it?")
        if lang != "en":
            ask = "Bhej dun?" if variant == 0 else "Bhej dun checklist?"
        return Segments(why=why, pre_done=pre_done, ask=ask)

    delta = ctx.fact("payload_delta_pct")
    metric = ctx.fact("payload_metric")
    window = ctx.fact("payload_window")
    metric_text = str(metric.value) if metric else ""
    if lang == "en":
        if delta and isinstance(delta.value, (int, float)):
            why = (f"Your {metric_text or 'numbers'} dropped {abs(round(delta.value * 100))}% "
                   f"over the last {str(window.value).replace('d', ' days') if window else '7 days'}.")
        else:
            momentum = ctx.momentum(only_negative=True)
            if momentum:
                why = f"{momentum.phrase[0].upper() + momentum.phrase[1:]}."
            else:
                weak = _weakest_gap(ctx)
                why = (f"Your {weak.phrase.split(' vs ')[0]} trail the peer average."
                       if weak else "Your numbers dipped this week.")
        pre_done = "I've drafted a short recovery checklist — listing fixes first, then an offer refresh."
        ask = "Want it?" if variant == 0 else "Shall I send the checklist?"
    else:
        if delta and isinstance(delta.value, (int, float)):
            why = (f"Pichle {str(window.value).replace('d', ' din')} mein aapke "
                   f"{metric_text or 'numbers'} {abs(round(delta.value * 100))}% gira hai.")
        else:
            momentum = ctx.momentum(only_negative=True)
            if momentum:
                why = f"{momentum.phrase[0].upper() + momentum.phrase[1:]}."
            else:
                weak = _weakest_gap(ctx)
                why = (_hinglish_gap_clause(ctx, weak)[0].upper() + _hinglish_gap_clause(ctx, weak)[1:] + "."
                       if weak else "Is hafte aapke numbers neeche gaye hain.")
        pre_done = "Ek chhota recovery checklist draft kar diya hai — pehle listing fixes, phir offer refresh."
        ask = "Bhej dun?" if variant == 0 else "Checklist bhej dun?"
    return Segments(why=why, pre_done=pre_done, ask=ask)


def _weakest_gap(ctx: ComposeContext) -> Fact | None:
    """The most negative peer-gap fact, if any."""
    gaps = [f for f in ctx.facts
            if f.key.startswith("gap_") and isinstance(f.value, (int, float)) and f.value < 0]
    if not gaps:
        return None
    pick = sorted(gaps, key=lambda f: (f.value, f.key))[0]
    ctx.used.append(pick.key)
    return pick


def _hinglish_gap_clause(ctx: ComposeContext, gap: Fact) -> str:
    """Production-transcript pattern: 'aapke 30 din mein 22 calls aaye —
    peer average 28 hota hai'. Built from the metric + benchmark facts so the
    verb agrees with the sentence instead of bolting Hindi onto English."""
    metric_key = gap.key.replace("gap_", "")
    metric = ctx.fact(metric_key)
    peer = ctx.fact(f"peer_avg_{metric_key}_30d") or ctx.fact(f"peer_avg_{metric_key}")
    metric_word = {"calls": "calls", "views": "views", "directions": "directions"}.get(
        metric_key, metric_key)
    if not metric or not isinstance(metric.value, (int, float)):
        return f"{gap.phrase[0].upper() + gap.phrase[1:]}."
    if peer and isinstance(peer.value, (int, float)):
        return (f"aapke 30 din mein {int(metric.value)} {metric_word} aaye — "
                f"peer average {int(peer.value)} hota hai")
    return f"aapke 30 din mein {int(metric.value)} {metric_word} aaye"


def _seasonal_dip(ctx: ComposeContext, lang: str, variant: int) -> Segments:
    diagnosis = ctx.diagnosis
    if diagnosis is not None and diagnosis.rule_id == "seasonal":
        why = diagnosis.story if lang == "en" else diagnosis.story_hi
        action = diagnosis.recommended_action if lang == "en" else diagnosis.action_hi
        pre_done = (f"On it: {action}." if lang == "en"
                    else f"{action[0].upper() + action[1:]} — main shuru kar deti hoon.")
        ask = "Shall I?" if variant == 0 else "Want it?"
        if lang != "en":
            ask = "Bana dun?" if variant == 0 else "Shuru karun?"
        return Segments(why=why, pre_done=pre_done, ask=ask)
    delta = ctx.fact("payload_delta_pct")
    beat = ctx.in_season()
    members = ctx.cohort("total_active_members", "total_unique_ytd")
    if lang == "en":
        drop = f"Your views are down {abs(round(delta.value * 100))}% this week" \
            if delta and isinstance(delta.value, (int, float)) else "Your numbers dipped this week"
        why = (f"{drop} — but this is the normal seasonal lull, not a real dip."
               + (f" Every year in the {beat.phrase.split('(')[-1].rstrip(')')} window the same pattern shows up."
                  if beat and "(" in beat.phrase else
                  (f" Typical for this window: {_cap(beat.phrase)}." if beat else "")))
        consequence = (f"Better use of this month: retention on your {members.phrase}."
                       if members else "Better use of this month: retention over acquisition.")
        pre_done = "Hold the ad spend for now; the conversion window reopens after the lull."
        ask = ("Want me to draft a retention challenge for the dip weeks?" if variant == 0
               else "Shall I draft a member-retention plan for the dip?")
    else:
        drop = (f"Is hafte views {abs(round(delta.value * 100))}% neeche hain"
                if delta and isinstance(delta.value, (int, float)) else "Numbers is hafte neeche hain")
        why = (f"{drop} — lekin yeh normal seasonal lull hai, asli dip nahi."
               + (f" {_cap(beat.phrase)} — har saal is window mein yahi hota hai." if beat else ""))
        consequence = (f"Is mahine behtar kaam: apne {members.phrase} par retention."
                       if members else "Is mahine retention par focus banta hai.")
        pre_done = "Ad spend abhi rok dein; lull ke baad conversion window doosri baar khulega."
        ask = ("Dip weeks ke liye retention challenge draft kar dun?" if variant == 0
               else "Member-retention plan bana dun?")
    return Segments(why=why, consequence=consequence, pre_done=pre_done, ask=ask)


def _perf_spike(ctx: ComposeContext, lang: str, variant: int) -> Segments:
    delta = ctx.fact("payload_delta_pct")
    metric = ctx.fact("payload_metric")
    driver = ctx.fact("payload_likely_driver")
    metric_text = str(metric.value) if metric else "the numbers"
    driver_text = str(driver.value).replace("_", " ") if driver else ""
    if lang == "en":
        why = (f"Your {metric_text} are up {round(delta.value * 100)}% vs last week"
               + (f" — {driver_text} looks like the driver" if driver_text else "") + "."
               if delta and isinstance(delta.value, (int, float))
               else f"Your {metric_text} jumped this week.")
        gap = ctx.best_gap()
        consequence = f"And you're ahead of the pack: {gap.phrase}." if gap else ""
        pre_done = "I can line up follow-up posts in the same series while the momentum holds."
        ask = "Want me to?" if variant == 0 else "Shall I prep those?"
    else:
        why = (f"Aapka {metric_text} pichle hafte se {round(delta.value * 100)}% upar hai"
               + (f" — {driver_text} iska driver lag raha hai" if driver_text else "") + "."
               if delta and isinstance(delta.value, (int, float))
               else f"Is hafte aapke {metric_text} ne accha uchhla hai.")
        gap = ctx.best_gap()
        consequence = f"Aur aap peers se aage ho: {gap.phrase}." if gap else ""
        pre_done = "Momentum ke dauran same series ke follow-up posts bana deti hoon."
        ask = "Bana dun?" if variant == 0 else "Prep kar dun?"
    return Segments(why=why, consequence=consequence, pre_done=pre_done, ask=ask)


def _milestone(ctx: ComposeContext, lang: str, variant: int) -> Segments:
    gap_fact = ctx.fact("payload_milestone_gap")
    metric = ctx.fact("payload_metric")
    milestone_value = ctx.fact("payload_milestone_value")
    metric_short = str(metric.value).replace("_", " ").replace(" count", "s") if metric else ""
    target = f" {int(milestone_value.value)}" if milestone_value and isinstance(milestone_value.value, (int, float)) else ""
    if lang == "en":
        why = (f"You're {int(gap_fact.value)} {metric_short + ' ' if metric_short else ''}away from{target} — "
               "close enough to close out this week." if gap_fact
               else f"The {metric_short or 'milestone'} milestone is within reach this week.")
        pre_done = "I've drafted a review-ask you can send to today's happy customers."
        ask = "Want it?" if variant == 0 else "Shall I send the draft?"
    else:
        why = (f"{target.strip() + ' ke target se' if target else 'Milestone se'} sirf "
               f"{int(gap_fact.value)} {metric_short + ' ' if metric_short else ''}door hain — "
               "is hafte poora ho sakta hai." if gap_fact
               else f"Milestone is hafte touch ho sakta hai.")
        pre_done = "Aaj ke happy customers ke liye ek review-ask draft kar diya hai."
        ask = "Bhej dun?" if variant == 0 else "Draft bhej dun?"
    return Segments(why=why, pre_done=pre_done, ask=ask)


def _renewal_due(ctx: ComposeContext, lang: str, variant: int) -> Segments:
    days = ctx.fact("payload_days_remaining") or ctx.fact("subscription_days_remaining")
    plan = ctx.fact("payload_plan") or ctx.fact("subscription_plan")
    amount = ctx.fact("payload_renewal_amount")
    gap = ctx.best_gap()
    views = ctx.fact("views")
    calls = ctx.fact("calls")
    plan_text = str(plan.value) if plan else "your plan"
    amount_text = f" (Rs.{int(amount.value):,})" if amount and isinstance(amount.value, (int, float)) else ""
    runway = f"renews in {int(days.value)} days{amount_text}" if days else "renewal is due"
    if lang == "en":
        perf_bits = []
        if views:
            perf_bits.append(views.phrase)
        if calls:
            perf_bits.append(calls.phrase)
        why = (f"Before the {plan_text} plan {runway} — a quick value recap: "
               + "; ".join(perf_bits) + "." if perf_bits
               else f"Before the {plan_text} plan {runway} — worth a quick value recap.")
        consequence = f"Vs peers: {gap.phrase}." if gap else ""
        pre_done = "I can put a one-page walkthrough together of what's working and what to fix next."
        ask = "Want it before you decide?" if variant == 0 else "Shall I send it over?"
    else:
        perf_bits = []
        if views:
            perf_bits.append(views.phrase)
        if calls:
            perf_bits.append(calls.phrase)
        why = (f"{plan_text} plan {runway} — usse pehle ek value recap: "
               + "; ".join(perf_bits) + "." if perf_bits
               else f"{plan_text} plan {runway} — ek value recap zaroori hai.")
        consequence = f"Peers se: {gap.phrase}." if gap else ""
        pre_done = "Ek page ka walkthrough bana deti hoon — kya chal raha hai, kya fix karna hai."
        ask = "Decide karne se pehle bhej dun?" if variant == 0 else "Bhej dun?"
    return Segments(why=why, consequence=consequence, pre_done=pre_done, ask=ask)


def _festival(ctx: ComposeContext, lang: str, variant: int) -> Segments:
    festival = ctx.fact("payload_festival")
    days = ctx.fact("payload_days_until")
    beat = ctx.in_season()
    offer = ctx.active_offer()
    fest_text = str(festival.value) if festival else ""
    if lang == "en":
        if festival and days and isinstance(days.value, (int, float)):
            why = f"{fest_text} is {int(days.value)} days out — early prep is what separates the busy festive season from the quiet one."
        elif festival:
            why = f"{fest_text} window is coming up — prep started early pays off."
        elif beat:
            why = f"The festive window is approaching: {beat.phrase}."
        else:
            why = "A festival window is coming up in the next season cycle."
        pre_done = (f"I can set up '{offer.phrase}' as a Google post plus a WhatsApp-ready note." if offer
                    else "I can draft the festival post and a WhatsApp-ready note for you.")
        ask = "Want me to draft it now?" if variant == 0 else "Shall I draft it?"
    else:
        if festival and days and isinstance(days.value, (int, float)):
            why = f"{fest_text} mein {int(days.value)} din bache hain — jo early prep karta hai, wahi festive season mein busy rehta hai."
        elif festival:
            why = f"{fest_text} ka window aane wala hai — early prep ka accha fayda hota hai."
        elif beat:
            why = f"Festive window qareeb hai: {beat.phrase}."
        else:
            why = "Agle season cycle mein ek festival window aa rahi hai."
        pre_done = (f"'{offer.phrase}' ko Google post + WhatsApp note ke roop mein set kar deti hoon." if offer
                    else "Festival post aur WhatsApp-ready note draft kar deti hoon.")
        ask = "Abhi draft kar dun?" if variant == 0 else "Draft kar deti hoon?"
    return Segments(why=why, pre_done=pre_done, ask=ask)


def _competitor(ctx: ComposeContext, lang: str, variant: int) -> Segments:
    name = ctx.fact("payload_competitor_name")
    dist = ctx.fact("payload_distance_km")
    their = ctx.fact("payload_their_offer")
    opened = ctx.fact("payload_opened_date")
    offer = ctx.active_offer()
    if lang == "en":
        if name:
            why = (f"Heads-up: {name.value} opened"
                   + (f" {dist.value} km away" if dist and isinstance(dist.value, (int, float)) else "")
                   + (f" on {opened.phrase}" if opened else "")
                   + (f" with {their.value}." if their else "."))
        else:
            why = "A competing listing opened up nearby this week — worth a look at your differentiation."
        consequence = (f"No panic — your '{offer.phrase}' is live." if offer else "")
        pre_done = ("Pairing it with a free add-on from your catalog gives a sharper story than a price cut. "
                    "I can draft that comparison post.")
        ask = "Want it?" if variant == 0 else "Shall I draft it?"
    else:
        if name:
            why = (f"Dhyan dein: {name.value} khula hai"
                   + (f" {dist.value} km door" if dist and isinstance(dist.value, (int, float)) else "")
                   + (f", {opened.phrase} se" if opened else "")
                   + (f", unka {their.value}." if their else "."))
        else:
            why = "Is hafte paas hi ek competing listing khuli hai — differentiation dekhne ka time hai."
        consequence = (f"Ghabrane ki baat nahi — aapka '{offer.phrase}' live hai." if offer else "")
        pre_done = "Price cut se behtar: catalog ke free add-on ke saath comparison post. Main draft kar deti hoon."
        ask = "Bana dun?" if variant == 0 else "Draft bhej dun?"
    return Segments(why=why, consequence=consequence, pre_done=pre_done, ask=ask)


def _curious_ask(ctx: ComposeContext, lang: str, variant: int) -> Segments:
    biz = ctx.business_name()
    if lang == "en":
        why = f"Quick weekly check-in for {biz}."
        pre_done = ("Tell me the most asked-for service this week and I'll turn it into a "
                    "Google post plus a 4-line WhatsApp reply you can reuse on pricing questions.")
        ask = "What's been the pick?" if variant == 0 else "Which service is it?"
    else:
        why = f"{biz} ke liye ek quick weekly check-in."
        pre_done = ("Bataiye is hafte sabse zyada kaunsi service maangi gayi — main usse ek "
                    "Google post aur 4-line ka WhatsApp reply bana deti hoon, pricing poochhne "
                    "walon ke liye ready jawab.")
        ask = "Kaunsi rahi is hafte?" if variant == 0 else "Bataiye, kaunsi service thi?"
    return Segments(why=why, pre_done=pre_done, ask=ask)


def _active_planning(ctx: ComposeContext, lang: str, variant: int) -> Segments:
    topic = ctx.fact("payload_intent_topic")
    offer = ctx.active_offer()
    topic_text = "the plan we discussed" if lang == "en" else "jaisa plan discuss hua tha"
    if topic:
        from harness.internal_vocab import rewrite_internal

        topic_text = rewrite_internal(str(topic.value).replace("_", " "))
    topic_text = re.sub(r"^(?:a|an|the)\s+", "", topic_text.strip(), flags=re.IGNORECASE)
    # For a real planning request, show a compact sample artifact. Do not echo
    # the merchant's conversational message or pad the reply with unrelated
    # performance/peer numbers just to satisfy the generic numeral budget.
    payload = (ctx.trigger or {}).get("payload") or {}
    real_request = bool(not payload.get("placeholder") and payload.get("intent_topic"))
    topic_words = set(re.findall(r"[a-z]{4,}", topic_text.lower()))
    offer_words = set(re.findall(r"[a-z]{4,}", offer.phrase.lower())) if offer else set()
    relevant_offer = bool(offer and topic_words & offer_words
                          and re.search(r"\d", str(offer.value) + offer.phrase))
    offer_text = f"'{offer.phrase}'" if relevant_offer else ""
    if lang == "en":
        if real_request and relevant_offer:
            why = (f"For the {topic_text}, here's a sample line: "
                   f"{offer_text} — a straightforward pick for a weekday team lunch.")
            pre_done = ("Before adding bulk-order terms, let's confirm the headcount "
                        "and delivery details.")
            ask = "Shall I refine this draft?" if variant == 0 else "Want me to polish this draft?"
        elif real_request:
            why = (f"For the {topic_text}, a first draft could read: "
                   f"\"We are exploring an option for {topic_text}. Would you like to hear more?\"")
            pre_done = "Share who it is for and the timing before I add specifics."
            ask = "Shall I refine this draft?" if variant == 0 else "Want me to polish this draft?"
        else:
            why = f"Here's a starter version for {topic_text}{' around ' + offer_text if offer_text else ''}."
            pre_done = "Share the details to confirm before adding package terms."
            ask = "Shall I refine this draft?" if variant == 0 else "Want me to polish this draft?"
    else:
        if real_request and relevant_offer:
            why = (f"{topic_text} ke liye ek sample line: "
                   f"{offer_text} — weekday team lunch ke liye ek simple option hai.")
            pre_done = ("Bulk order ke terms jodne se pehle headcount aur delivery "
                        "details confirm kar lein.")
            ask = "Is draft ko refine karun?" if variant == 0 else "Isse polish karun?"
        elif real_request:
            why = (f"{topic_text} ke liye ek pehla draft: "
                   f"\"Hum {topic_text} ke liye ek option explore kar rahe hain. Kya aap details jaana chahenge?\"")
            pre_done = "Isse sahi banane ke liye audience aur timing bataiye."
            ask = "Is draft ko refine karun?" if variant == 0 else "Isse polish karun?"
        else:
            why = f"{topic_text} ke liye ek starter version{' — ' + offer_text if offer_text else ''}."
            pre_done = "Package terms jodne se pehle details confirm kar lein."
            ask = "Is draft ko refine karun?" if variant == 0 else "Isse polish karun?"
    return Segments(why=why, pre_done=pre_done, ask=ask)


def _gbp_unverified(ctx: ComposeContext, lang: str, variant: int) -> Segments:
    path = ctx.fact("payload_verification_path")
    uplift = ctx.fact("payload_estimated_uplift_pct")
    # D1: the prescribed human form breaks the raw enum token sequence
    path_en = "a postcard or a phone call"
    path_hi = "bas ek postcard ya ek phone call"
    if lang == "en":
        why = (f"Your Google profile is still unverified — verification is just {path_en}."
               + (f" Verified listings see an estimated {round(uplift.value * 100)}% uplift in calls."
                  if uplift and isinstance(uplift.value, (int, float)) else ""))
        pre_done = "I can walk you through the verification step right now — it's quick."
        ask = "Shall we do it?" if variant == 0 else "Want to do it now?"
    else:
        why = (f"Aapki Google profile abhi bhi unverified hai — verification ke liye {path_hi} chahiye."
               + (f" Verified listings ko estimated {round(uplift.value * 100)}% zyada calls milte hain."
                  if uplift and isinstance(uplift.value, (int, float)) else ""))
        pre_done = "Verification ka step main abhi walk kar deti hoon — kaafi quick hai."
        ask = "Ab kar lein?" if variant == 0 else "Abhi karein?"
    return Segments(why=why, pre_done=pre_done, ask=ask)


def _dormant(ctx: ComposeContext, lang: str, variant: int) -> Segments:
    days = ctx.fact("payload_days_since_last_merchant_message") \
        or ctx.fact("signal_dormant_with_vera")
    digest = ctx.top_digest()
    gap = ctx.best_gap()
    if lang == "en":
        why = (f"It's been {int(days.value)} days since we last spoke — no guilt on my side."
               if days and isinstance(days.value, (int, float)) else
               "It's been a while since we last spoke — no guilt on my side.")
        consequence = (f"Meanwhile something useful landed: '{_first_sentence(digest.phrase, 110)}'."
                       if digest else
                       (f"One thing worth knowing: {gap.phrase}." if gap else ""))
        pre_done = "Your listing data is intact and there's one quick win pending."
        ask = "Want the one-liner?" if variant == 0 else "Shall I share it?"
        return Segments(why=why, consequence=consequence, pre_done=pre_done, ask=ask,
                        citation=ctx.citation_of(digest))
    else:
        why = (f"Pichli baat ko {int(days.value)} din ho gaye — meri taraf se koi shikayat nahi."
               if days and isinstance(days.value, (int, float)) else
               "Kaafi time ho gaya baat kiye — koi baat nahi.")
        consequence = (f"Tab ke baad ek useful cheez aayi hai: '{_first_sentence(digest.phrase, 110)}'."
                       if digest else
                       (f"Ek kaam ki baat: {gap.phrase}." if gap else ""))
        pre_done = "Aapka listing data intact hai aur ek quick win pending hai."
        ask = "Ek line mein bata dun?" if variant == 0 else "Bata dun?"
        return Segments(why=why, consequence=consequence, pre_done=pre_done, ask=ask,
                        citation=ctx.citation_of(digest))
    return Segments(why=why, consequence=consequence, pre_done=pre_done, ask=ask)


def _winback(ctx: ComposeContext, lang: str, variant: int) -> Segments:
    days = ctx.fact("payload_days_since_expiry") or ctx.fact("subscription_days_since_expiry")
    dip = ctx.fact("payload_perf_dip_pct")
    lapsed = ctx.fact("payload_lapsed_customers_added_since_expiry")
    plan = ctx.fact("subscription_plan")
    plan_text = str(plan.value) if plan else "Pro"
    if lang == "en":
        why = (f"Your {plan_text} plan lapsed {int(days.value)} days ago"
               + (f" and your numbers slipped {abs(round(dip.value * 100))}% since" if dip and isinstance(dip.value, (int, float)) else "")
               + (f" — {int(lapsed.value)} regulars haven't returned in that time." if lapsed and isinstance(lapsed.value, (int, float)) else ".")
               if days else
               "Your plan lapsed recently and the listing benefits paused with it.")
        pre_done = "Reactivation is one click — your listing data is intact. I can prep a summary: plan, price, and exactly what turns back on."
        ask = "Want it?" if variant == 0 else "Shall I prep it?"
    else:
        why = (f"Aapka {plan_text} plan {int(days.value)} din pehle lapse ho gaya"
               + (f" aur tab se numbers {abs(round(dip.value * 100))}% slip kiye" if dip and isinstance(dip.value, (int, float)) else "")
               + (f" — itne din mein {int(lapsed.value)} regular wapas nahi aaye." if lapsed and isinstance(lapsed.value, (int, float)) else ".")
               if days else
               "Plan recently lapse hua aur listing ke fayde bhi pause ho gaye.")
        pre_done = "Reactivation ek click hai — listing data intact hai. Summary bana deti hoon: plan, price, aur kya-kya wapas chalega."
        ask = "Bana dun?" if variant == 0 else "Prep kar dun?"
    return Segments(why=why, pre_done=pre_done, ask=ask)


def _review_theme(ctx: ComposeContext, lang: str, variant: int) -> Segments:
    theme = ctx.fact("payload_theme")
    occurrences = ctx.fact("payload_occurrences_30d")
    quote = ctx.fact("payload_common_quote")
    if not theme:
        negatives = [f for f in ctx.by_kind("review") if f.key.startswith("review_theme_")]
        theme = negatives[0] if negatives else None
        if theme:
            for f in ctx.facts:
                if f.key == theme.key.replace("review_theme_", "review_quote_"):
                    quote = f
                    ctx.used.append(f.key)
                    break
            if isinstance(theme.value, (int, float)):
                occurrences = theme
    theme_text = str(theme.value).replace("_", " ") if theme else ""
    # a raw theme label is machine enum — if it would leak, lead with the
    # verbatim quote instead and keep the theme out of the copy entirely
    try:
        from ..grounding.validator import _internal_vocab_hits

        if theme_text and _internal_vocab_hits(theme_text):
            theme_text = ""
    except Exception:
        pass
    count = int(occurrences.value) if occurrences and isinstance(occurrences.value, (int, float)) else None
    quote_text = f' One verbatim: "{quote.value}".' if quote else ""
    if lang == "en":
        if count and theme_text:
            why = f"{count} reviews this month mention {theme_text}.{quote_text}"
        elif count:
            why = f"{count} reviews this month say the same thing.{quote_text}"
        elif theme_text:
            why = f"Recent reviews keep mentioning {theme_text}.{quote_text}"
        else:
            why = "One theme keeps coming up in your reviews this month."
        pre_done = "I can draft the fix note plus a review-reply template — one for the counter, one for the reviewer."
        ask = "Want both?" if variant == 0 else "Shall I send both?"
    else:
        if count and theme_text:
            why = f"Is mahine {count} reviews mein {theme_text} ki baat hai.{quote_text}"
        elif count:
            why = f"Is mahine {count} reviews ek hi baat keh rahe hain.{quote_text}"
        elif theme_text:
            why = f"Recent reviews mein {theme_text} baar-baar aa raha hai.{quote_text}"
        else:
            why = "Is mahine aapki listing ke reviews mein ek theme ubhar rahi hai."
        pre_done = "Fix note + review-reply template draft kar deti hoon — ek counter ke liye, ek reviewer ke liye."
        ask = "Dono bhej dun?" if variant == 0 else "Bhej dun dono?"
    return Segments(why=why, pre_done=pre_done, ask=ask)


def _ipl(ctx: ComposeContext, lang: str, variant: int) -> Segments:
    match = ctx.fact("payload_match")
    venue = ctx.fact("payload_venue")
    time_fact = ctx.fact("payload_match_time_iso")
    weeknight = ctx.fact("payload_is_weeknight")
    offer = ctx.active_offer()
    match_text = str(match.value) if match else "the match"
    venue_text = str(venue.value) if venue else ""
    time_text = ""
    if time_fact:
        parsed = parse_dt(time_fact.value)
        if parsed:
            hour = parsed.hour
            time_text = f"{hour % 12 or 12}{'pm' if hour >= 12 else 'am'}"
    is_weeknight = bool(weeknight and weeknight.value is True)
    offer_text = str(offer.value) if offer else ""
    if lang == "en":
        when = f" tonight, {time_text}" if time_text else " tonight"
        at = f" at {venue_text}" if venue_text else ""
        if is_weeknight:
            why = f"{match_text}{at}{when} — a weeknight crowd stays out. Run your '{offer_text}' as a dine-in match-night special."
            pre_done = "I'll frame it arrive-early so the kitchen isn't slammed at kickoff."
            ask = "Want the banner draft?" if variant == 0 else "Shall I draft the banner?"
        else:
            why = (f"{match_text}{at}{when} — weekend match nights usually pull dine-in covers down; "
                   "people watch at home. Skip the match-night promo today.")
            pre_done = (f"Instead, run your '{offer_text}' as a delivery-only special. "
                        "I can draft the banner plus a story — live in 10 min.")
            ask = "Want it?" if variant == 0 else "Shall I send both?"
    else:
        when = f" aaj raat {time_text}" if time_text else " aaj raat"
        at = f", {venue_text}" if venue_text else ""
        if is_weeknight:
            why = f"{match_text}{at}{when} — weeknight bheed sambhalti hai. Aapka '{offer_text}' match-night dine-in special ban jaye."
            pre_done = "Kickoff se pehle aane wala framing rakhungi, kitchen busy na ho."
            ask = "Banner draft kar dun?" if variant == 0 else "Draft bhej dun?"
        else:
            why = (f"{match_text}{at}{when} — weekend match nights mein dine-in covers usually girti hain; "
                   "log ghar par dekhte hain. Aaj match-night promo skip kijiye.")
            pre_done = (f"Iski jagah aapka '{offer_text}' delivery-only special ban jaye. "
                        "Banner + story draft kar deti hoon — 10 min mein live.")
            ask = "Bhej dun?" if variant == 0 else "Dono bhej dun?"
    return Segments(why=why, pre_done=pre_done, ask=ask)


def _category_seasonal(ctx: ComposeContext, lang: str, variant: int) -> Segments:
    # D3: pick the largest riser and the largest faller — never dump the list
    trends_fact = ctx.fact("payload_trends")
    items: list[tuple[str, int]] = []
    if trends_fact and isinstance(trends_fact.value, list):
        for raw in trends_fact.value:
            # values arrive humanized ('antifungal demand +45%') or raw
            # ('antifungal_demand_+45') — parse both
            match = re.match(r"(.+?)\s+demand\s+([+-]?\d+)\s?%?$", str(raw), re.IGNORECASE) \
                or re.match(r"(.+?)_demand_([+-]\d+)$", str(raw))
            if match:
                items.append((match.group(1).replace("_", " ").strip(), int(match.group(2))))
    season = ctx.fact("payload_season")
    season_text = str(season.phrase) if season else ("this season" if lang == "en" else "is season")
    riser = max((i for i in items if i[1] > 0), key=lambda i: i[1], default=None)
    faller = min((i for i in items if i[1] < 0), key=lambda i: i[1], default=None)
    if lang == "en":
        if riser and faller:
            why = (f"{season_text[0].upper() + season_text[1:]} is shifting demand — "
                   f"{riser[0]} is up {riser[1]}% while {faller[0]} is down {abs(faller[1])}%.")
        elif riser:
            why = f"{season_text[0].upper() + season_text[1:]} is shifting demand — {riser[0]} is up {riser[1]}%."
        else:
            why = f"{season_text[0].upper() + season_text[1:]} demand is shifting."
        pre_done = ("Reorder the shelf in exactly that order — I'll make you a one-page "
                    "priority list, rising items first.")
        ask = "Want it?" if variant == 0 else "Shall I send it?"
    else:
        if riser and faller:
            why = (f"{season_text[0].upper() + season_text[1:]} demand shift ho rahi hai — "
                   f"{riser[0]} {riser[1]}% upar hai, {faller[0]} {abs(faller[1])}% neeche.")
        elif riser:
            why = f"{season_text[0].upper() + season_text[1:]} demand shift ho rahi hai — {riser[0]} {riser[1]}% upar hai."
        else:
            why = f"{season_text[0].upper() + season_text[1:]} demand shift ho rahi hai."
        pre_done = ("Shelf reorder isi hisaab se kar lo — main ek page ki priority list "
                    "bana deti hoon, rising items pehle.")
        ask = "Bhej dun?" if variant == 0 else "List bhej dun?"
    citation = _season_digest_citation(ctx, (season_text or "") + " " +
                                       (riser[0] if riser else "") + " " +
                                       (faller[0] if faller else ""))
    return Segments(why=why, pre_done=pre_done, ask=ask, citation=citation)


def _season_digest_citation(ctx: ComposeContext, claim_words: str) -> str:
    """D6: a demand claim cites the digest item that covers it, when one exists."""
    words = {w for w in re.findall(r"[a-z]{4,}", claim_words.lower())}
    best: Fact | None = None
    best_score = 0
    for fact in ctx.facts:
        if fact.kind != "digest" or not fact.citation:
            continue
        digest_words = set(re.findall(r"[a-z]{4,}", str(fact.value).lower())) | \
            set(re.findall(r"[a-z]{4,}", str(fact.citation).lower()))
        score = len(words & digest_words)
        if score > best_score:
            best, best_score = fact, score
    return best.citation if best else ""


# ---------------------------------------------------------------------------
# generic frame (unknown kinds — must never crash, never return empty)
# ---------------------------------------------------------------------------

def _generic(ctx: ComposeContext, lang: str, variant: int) -> Segments:
    ranked = sorted(ctx.facts, key=lambda f: (-f.salience, f.key))
    primary = ranked[0] if ranked else None
    secondary = ranked[1] if len(ranked) > 1 else None
    if primary:
        ctx.used.append(primary.key)
    if secondary:
        ctx.used.append(secondary.key)
    primary_text = primary.phrase if primary else ""
    secondary_text = secondary.phrase if secondary else ""
    kind_text = ctx.trigger_kind.replace("_", " ")
    # name the kind only when its plain-language phrase passes both the
    # internal vocabulary and the payload-enum net (known dataset kinds are
    # blocked by design; unseen Phase-3 kinds may be unregistered enums)
    try:
        from ..grounding.validator import _internal_vocab_hits, _payload_net_hits

        if _internal_vocab_hits(kind_text) or _payload_net_hits(kind_text, ctx.trigger, ctx.customer):
            kind_text = ""
    except Exception:
        kind_text = ""
    if lang == "en":
        why = f"Quick update — this came up under '{kind_text}': {primary_text}." if kind_text and primary_text \
            else f"Quick update from your account review: {primary_text}." if primary_text \
            else "Quick update from your account review."
        consequence = f"And one more: {secondary_text}." if secondary_text else ""
        ask = "Want the short action plan?" if variant == 0 else "Shall I put a plan together?"
    else:
        why = f"Ek quick update — '{kind_text}' ke under aaya hai: {primary_text}." if kind_text and primary_text \
            else f"Aapke account review se ek quick update: {primary_text}." if primary_text \
            else "Aapke account review se ek quick update."
        consequence = f"Aur ek: {secondary_text}." if secondary_text else ""
        ask = "Chhota action plan bana dun?" if variant == 0 else "Plan bana dun?"
    return Segments(why=why, consequence=consequence, ask=ask)


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------

def _f(kind: str, cta: str, emoji_ok: bool, citation_required: bool,
       builder: Callable) -> Frame:
    return Frame(kind=kind, cta=cta, emoji_ok=emoji_ok,
                 citation_required=citation_required, build=builder)


FRAMES: dict[str, Frame] = {
    "active_planning_intent": _f("active_planning_intent", "binary_confirm_cancel", False, False, _active_planning),
    "appointment_tomorrow": _f("appointment_tomorrow", "multi_choice_slot", False, False, _appointment_tomorrow),
    "category_seasonal": _f("category_seasonal", "binary_yes_no", False, False, _category_seasonal),
    "cde_opportunity": _f("cde_opportunity", "binary_yes_no", False, True, _cde_opportunity),
    "chronic_refill_due": _f("chronic_refill_due", "binary_confirm_cancel", False, False, _chronic_refill_due),
    "competitor_opened": _f("competitor_opened", "binary_yes_no", False, False, _competitor),
    "curious_ask_due": _f("curious_ask_due", "open_ended", False, False, _curious_ask),
    "customer_lapsed_hard": _f("customer_lapsed_hard", "binary_yes_no", True, False, _lapsed_hard),
    "customer_lapsed_soft": _f("customer_lapsed_soft", "binary_yes_no", True, False, _lapsed_soft),
    "dormant_with_vera": _f("dormant_with_vera", "binary_yes_no", False, False, _dormant),
    "festival_upcoming": _f("festival_upcoming", "binary_yes_no", True, False, _festival),
    "gbp_unverified": _f("gbp_unverified", "binary_yes_no", False, False, _gbp_unverified),
    "ipl_match_today": _f("ipl_match_today", "binary_yes_no", False, False, _ipl),
    "milestone_reached": _f("milestone_reached", "binary_yes_no", False, False, _milestone),
    "perf_dip": _f("perf_dip", "binary_yes_no", False, False, _perf_dip),
    "perf_spike": _f("perf_spike", "binary_yes_no", False, False, _perf_spike),
    "recall_due": _f("recall_due", "multi_choice_slot", True, False, _recall_due),
    "regulation_change": _f("regulation_change", "binary_yes_no", False, True, _regulation_change),
    "renewal_due": _f("renewal_due", "binary_yes_no", False, False, _renewal_due),
    "research_digest": _f("research_digest", "open_ended", False, True, _research_digest),
    "review_theme_emerged": _f("review_theme_emerged", "binary_yes_no", False, False, _review_theme),
    "seasonal_perf_dip": _f("seasonal_perf_dip", "binary_yes_no", False, False, _seasonal_dip),
    "supply_alert": _f("supply_alert", "binary_yes_no", False, True, _supply_alert),
    "trial_followup": _f("trial_followup", "multi_choice_slot", True, False, _trial_followup),
    "wedding_package_followup": _f("wedding_package_followup", "binary_yes_no", True, False, _wedding_followup),
    "winback_eligible": _f("winback_eligible", "binary_yes_no", False, False, _winback),
}

GENERIC_FRAME = _f("__generic__", "binary_yes_no", False, False, _generic)
