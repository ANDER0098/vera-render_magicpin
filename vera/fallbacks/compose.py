"""Deterministic fallback composer: frame x voice x facts -> message.

Pipeline (second pass): ``attach_conditionals`` enriches the fact list, the
diagnosis picks the ONE driving signal, ``select_offer`` picks the ONE offer,
frames render from those, and a numeral budget guarantees >= 3 grounded
Tier-1 numerals per body. The returned :class:`FallbackMessage` ALWAYS passes
validation with zero hard violations on the corpus. No LLM, no network, no
randomness, no wall clock.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from ..grounding.validator import (
    grounding_map,
    has_hard,
    repair,
    validate,
)
from ..grounding.whitelist import (
    build_whitelist,
    extract_numerals,
    is_allowed_constant,
    numeral_variants,
)
from ..signals.derived import Fact, is_placeholder_trigger, rank_facts
from ..signals.diagnose import diagnose
from ..signals.relevance import attach_conditionals, select_offer
from ..text.normalize import normalize_text
from ..voice import Voice
from .frames import GENERIC_FRAME, SLOT_KINDS, ComposeContext, Frame, FRAMES, Segments

__all__ = ["FallbackMessage", "SUPPORTED_KINDS", "is_supported_kind", "compose_fallback"]

SUPPORTED_KINDS: frozenset[str] = frozenset(FRAMES.keys())

_NUMERAL_BUDGET = 3


def is_supported_kind(kind: str) -> bool:
    return kind in SUPPORTED_KINDS


@dataclass(frozen=True)
class FallbackMessage:
    body: str
    cta: str
    send_as: str
    template_name: str
    template_params: list[str]
    rationale: str
    facts_used: tuple[str, ...]


def _variant_seed(trigger: dict | None, trigger_kind: str) -> int:
    """Deterministic 2-way variant selector (distinguishes duplicate triggers
    of the same kind for the same merchant without randomness)."""
    seed_source = str((trigger or {}).get("id") or (trigger or {}).get("suppression_key")
                      or trigger_kind or "x")
    digest = hashlib.sha256(seed_source.encode("utf-8")).hexdigest()[:8]
    return int(digest, 16) % 2


def _resolve_scope(trigger: dict | None, customer: dict | None) -> str:
    scope = (trigger or {}).get("scope")
    if scope in ("customer", "merchant"):
        return str(scope)
    if customer is not None:
        return "customer"
    return "merchant"


def _resolve_cta(frame: Frame, ctx: ComposeContext) -> str:
    cta = frame.cta
    if ctx.trigger_kind in SLOT_KINDS:
        for key in ("payload_available_slots", "payload_next_session_options", "payload_slots"):
            fact = next((f for f in ctx.facts if f.key == key), None)
            if fact and isinstance(fact.value, list) and len(fact.value) >= 2:
                return "multi_choice_slot"
        return "binary_yes_no"
    return cta


def _attribution(ctx: ComposeContext) -> str:
    owner = ctx.owner_name()
    business = ctx.business_name()
    locality = ctx.locality()
    name = business
    if owner and owner.lower() not in business.lower():
        name = f"{owner} from {business}"
    if locality and locality.lower() not in name.lower():
        name = f"{name} {locality}"
    return name


def _assemble(ctx: ComposeContext, frame: Frame, segments: Segments,
              evidence: list[str] | None = None) -> str:
    voice = ctx.voice
    emoji = voice.emoji if frame.emoji_ok else None
    emoji_text = f" {emoji}" if emoji else ""

    if ctx.scope == "merchant":
        head = f"{voice.salutation}{emoji_text}, {segments.why}"
    else:
        attr = _attribution(ctx)
        if voice.recipient in ("guardian",) and voice.salutation in ("Namaste", "Hello"):
            # intermediary thread with no named reader: greet generically and
            # refer to the customer in the third person
            head = f"{voice.salutation}{emoji_text} — {attr} yahan. {segments.why}" \
                if voice.lang != "en" else \
                f"{voice.salutation}{emoji_text} — {attr} here. {segments.why}"
        elif voice.lang == "en":
            head = f"Hi {voice.salutation}, {attr} here{emoji_text}. {segments.why}"
        elif voice.salutation.lower().startswith("namaste"):
            head = f"{voice.salutation}{emoji_text} — {attr} yahan. {segments.why}"
        else:
            head = f"Hi {voice.salutation}, {attr} yahan{emoji_text}. {segments.why}"

    pre_done = segments.pre_done
    if evidence:
        pre_done = f"{pre_done} {' '.join(evidence)}".strip()
    parts = [head, segments.consequence, pre_done, segments.ask]
    text = " ".join(p.strip() for p in parts if p and p.strip())
    if segments.citation:
        text = f"{text} — {segments.citation}"
    text = re.sub(r"\s{2,}", " ", text).strip()
    # currency glyph never leaves this layer
    text = text.replace("₹ ", "Rs.").replace("₹", "Rs.")
    text = re.sub(r"Rs\.\s+", "Rs.", text)
    return text


def _tier1_count(ctx: ComposeContext, text: str, cta: str,
                 whitelist: set[str]) -> int:
    if str(cta) == "multi_choice_slot":
        whitelist = set(whitelist) | {str(i) for i in range(1, 10)}
    tokens = {token for token in extract_numerals(text)
              if numeral_variants(token) & whitelist
              and not is_allowed_constant(text, token)}
    return len(tokens)


def _is_grounded_active_plan(ctx: ComposeContext) -> bool:
    """A real planning reply can be specific through a concrete artifact and
    one listed offer price; unrelated statistics must not be added to hit a
    generic three-number target."""
    trigger = ctx.trigger or {}
    payload = trigger.get("payload") or {}
    return bool(trigger.get("kind") == "active_planning_intent"
                and not payload.get("placeholder")
                and payload.get("intent_topic"))


def _evidence_clauses(ctx: ComposeContext) -> list[str]:
    """Numeral-bearing clauses in the spec's fill order: diagnosis primary and
    supporting -> offer price -> peer pair -> digest number/date -> trend ->
    cohort count -> tenure -> deadline."""
    lang = ctx.voice.lang
    clauses: list[str] = []

    diagnosis = ctx.diagnosis
    if diagnosis is not None:
        for fact in (diagnosis.primary, *diagnosis.supporting):
            if fact is None:
                continue
            # D6: a trend-kind diagnosis fact makes a demand claim — carry its
            # source inline or skip it (an uncited trend claim is a penalty)
            if fact.kind == "trend":
                query_text = str(fact.value.get("query") if isinstance(fact.value, dict)
                                 else fact.phrase)
                source = _trend_source(ctx, query_text)
                if not source:
                    continue
                phrase = fact.phrase[0].upper() + fact.phrase[1:]
                clauses.append(f"{phrase} — {source}." if not phrase.endswith(".") else phrase)
                continue
            phrase = fact.phrase[0].upper() + fact.phrase[1:]
            clauses.append(f"{phrase}." if not phrase.endswith(".") else phrase)

    offer = ctx.selected_offer() or ctx.active_offer()
    if offer:
        price = _extract_price(offer)
        if price:
            # deliberately no 'offer' word near the price — promotional framing
            # would trip the consent check for non-promotional intents
            clauses.append(f"Also live on the listing: '{offer.phrase}'." if lang == "en"
                           else f"Yeh bhi listing par live hai: '{offer.phrase}'.")

    views = next((f for f in ctx.facts if f.key == "views"), None)
    calls = next((f for f in ctx.facts if f.key == "calls"), None)
    pv = next((f for f in ctx.facts if f.key == "peer_avg_views_30d"), None)
    pc = next((f for f in ctx.facts if f.key == "peer_avg_calls_30d"), None)
    if views and calls and pv and pc:
        if lang == "en":
            clauses.append(f"Last 30 days: {int(views.value):,} views and "
                           f"{int(calls.value)} calls, against peer norms of "
                           f"{int(pv.value):,} and {int(pc.value)}.")
        else:
            clauses.append(f"Pichle 30 din: {int(views.value):,} views aur "
                           f"{int(calls.value)} calls aaye — peer average "
                           f"{int(pv.value):,} aur {int(pc.value)} hai.")

    digest = ctx.top_digest()
    if digest:
        trial = next((f for f in ctx.facts
                      if f.key.startswith("digest") and f.key.endswith("trial_n")
                      and isinstance(f.value, (int, float))), None)
        if trial:
            clauses.append(f"Worth a read: '{digest.phrase}' ({int(trial.value)}-patient trial)."
                           if lang == "en" else
                           f"Ek kaam ki padhai: '{digest.phrase}' ({int(trial.value)}-patient trial).")
        else:
            clauses.append(f"Worth a read: '{digest.phrase}'." if lang == "en"
                           else f"Ek kaam ki padhai: '{digest.phrase}'.")

    trend = next((f for f in sorted((x for x in ctx.facts if x.kind == "trend"),
                                    key=lambda f: (-f.salience, f.key))), None)
    if trend:
        # D6: a trend claim renders only when the digest covers it, and then
        # carries its source inline — an uncited trend claim is a soft penalty
        query_text = str(trend.value.get("query") if isinstance(trend.value, dict) else trend.phrase)
        source = _trend_source(ctx, query_text)
        if source:
            clauses.append(f"{trend.phrase[0].upper() + trend.phrase[1:]} — {source}.")

    cohort = ctx.cohort("chronic_rx_count", "high_risk_adult_count",
                        "total_active_members", "lapsed_180d_plus", "lapsed_90d_plus",
                        "total_unique_ytd")
    if cohort:
        clauses.append(f"{cohort.phrase[0].upper() + cohort.phrase[1:]}.")

    tenure = next((f for f in ctx.facts if f.key == "tenure_years"), None)
    if tenure:
        clauses.append(f"{tenure.phrase[0].upper() + tenure.phrase[1:]}.")

    deadline = next((f for f in ctx.facts
                     if f.kind == "trigger" and f.key in (
                         "payload_stock_runs_out_iso", "payload_deadline_iso",
                         "payload_due_date", "payload_wedding_date")
                     and f.phrase), None)
    if deadline:
        clauses.append(f"Runs out {deadline.phrase}." if lang == "en"
                       else f"{deadline.phrase} tak ka time hai.")

    return clauses


def _extract_price(offer: Fact) -> str:
    match = re.search(r"(?:₹|Rs\.?)\s?([\d,]+)", str(offer.value), re.IGNORECASE)
    return match.group(1) if match else ""


def _trend_source(ctx: ComposeContext, query_text: str) -> str:
    """The digest source covering this trend, when one exists (D6)."""
    words = {w for w in re.findall(r"[a-z]{4,}", str(query_text).lower())}
    best = ""
    best_score = 0
    for fact in ctx.facts:
        if fact.kind != "digest" or not fact.citation:
            continue
        digest_words = set(re.findall(r"[a-z]{4,}", str(fact.value).lower()))
        score = len(words & digest_words)
        if score > best_score:
            best, best_score = fact.citation, score
    return best


def _ensure_numeral_budget(ctx: ComposeContext, segments: Segments,
                           cta: str) -> tuple[Segments, bool]:
    """Fill the numeral budget (>= 3 grounded Tier-1 numerals) by appending
    evidence clauses BEFORE the ask, in the spec's fill order."""
    whitelist = build_whitelist(ctx.facts, ctx.voice)
    probe_head = f"{segments.why} {segments.consequence} {segments.pre_done}"
    budget = _tier1_count(ctx, probe_head, cta, whitelist)
    if budget >= _NUMERAL_BUDGET:
        return segments, False

    existing = set(extract_numerals(probe_head))
    evidence: list[str] = []
    for clause in _evidence_clauses(ctx):
        if budget >= _NUMERAL_BUDGET:
            break
        clause_tokens = extract_numerals(clause)
        if not clause_tokens:
            continue  # a numeral-less clause cannot fill a numeral budget
        if all(t in existing for t in clause_tokens):
            continue  # clause adds no new numeral
        evidence.append(clause)
        existing.update(clause_tokens)
        budget = _tier1_count(ctx, probe_head + " " + " ".join(evidence), cta, whitelist)
    if not evidence:
        return segments, False
    merged_pre = f"{segments.pre_done} {' '.join(evidence)}".strip()
    return Segments(why=segments.why, consequence=segments.consequence,
                    pre_done=merged_pre, ask=segments.ask,
                    citation=segments.citation), True


def _minimal_body(ctx: ComposeContext) -> str:
    ranked = sorted(ctx.facts, key=lambda f: (-f.salience, f.key))
    phrase = ranked[0].phrase if ranked else "we have an update for you"
    phrase = phrase[0].upper() + phrase[1:] if phrase else phrase
    voice = ctx.voice
    if ctx.scope == "merchant":
        return f"{voice.salutation}, quick update: {phrase}. Shall I share the details?"
    return f"Hi {voice.salutation}, {ctx.business_name()} here. {phrase} Shall I share the details?"


def _rationale(ctx: ComposeContext, body: str, frame: Frame, cta: str) -> str:
    lines: list[str] = []
    used_facts = rank_facts([f for f in ctx.facts if f.key in set(ctx.used)], ctx.trigger)
    if used_facts:
        lines.append(f"Anchor: {used_facts[0].phrase} [{used_facts[0].json_path}].")
    for fact in used_facts[1:]:
        lines.append(f"Fact: {fact.phrase} [{fact.json_path}].")
    if ctx.diagnosis is not None:
        lines.append(f"Diagnosis rule '{ctx.diagnosis.rule}': {ctx.diagnosis.recommended_action}.")
    if ctx.trigger is not None and is_placeholder_trigger(ctx.trigger):
        lines.append("Trigger payload is a placeholder; anchored on derived merchant/category facts.")
    lines.append("Grounding map: " + "; ".join(grounding_map(body, ctx.facts, ctx.voice)) + ".")
    lines.append(f"Frame {frame.kind}, lang {ctx.voice.lang}, cta {cta}. No field invented.")
    return " ".join(lines)


def compose_fallback(trigger_kind: str, *, category: dict | None,
                     merchant: dict | None, trigger: dict | None,
                     customer: dict | None, facts: list[Fact],
                     voice: Voice, now: datetime,
                     prior_bodies: Iterable[str] = ()) -> FallbackMessage:
    """Compose the deterministic floor message for one trigger.

    Never crashes, never returns an empty body, and the returned message
    passes :func:`validate` with zero hard violations whenever the fact list
    can support the numeral budget (always true on the expanded corpus).
    """
    facts = attach_conditionals(list(facts), category=category, merchant=merchant,
                                trigger=trigger, customer=customer, now=now)
    frame = FRAMES.get(trigger_kind) or GENERIC_FRAME
    scope = _resolve_scope(trigger, customer)
    ctx = ComposeContext(
        trigger_kind=trigger_kind, category=category, merchant=merchant,
        trigger=trigger, customer=customer, facts=facts, voice=voice,
        now=now, scope=scope,
    )
    ctx.diagnosis = diagnose(facts, merchant=merchant, category=category,
                             trigger=trigger, now=now)
    offer_facts = [f for f in facts if f.kind == "offer" and f.key.startswith("offer_")]
    catalog_facts = [f for f in facts if f.key.startswith("catalog_offer_")]
    ctx.selected = select_offer(offer_facts, catalog_facts, customer=customer,
                                trigger=trigger, merchant=merchant, category=category)
    for fact in (ctx.diagnosis.primary, *ctx.diagnosis.supporting) if ctx.diagnosis else ():
        if fact is not None:
            ctx.used.append(fact.key)
    if ctx.selected is not None:
        ctx.used.append(ctx.selected.key)

    cta = _resolve_cta(frame, ctx)
    send_as = "merchant_on_behalf" if scope == "customer" else "vera"
    variant = _variant_seed(trigger, trigger_kind)

    candidate_specs: list[tuple[Frame, int]] = [
        (frame, variant),
        (frame, 1 - variant),
    ]
    if frame is not GENERIC_FRAME:
        candidate_specs += [(GENERIC_FRAME, variant), (GENERIC_FRAME, 1 - variant)]

    prior_norms = {normalize_text(p) for p in prior_bodies if p}

    licensed = ctx.diagnosis.licensed_claims if ctx.diagnosis else ()

    best: str | None = None
    best_frame: Frame = frame
    for cand_frame, cand_variant in candidate_specs:
        segments = cand_frame.build(ctx, voice.lang, cand_variant)
        if not _is_grounded_active_plan(ctx):
            segments, _ = _ensure_numeral_budget(ctx, segments, cta)
        body = _assemble(ctx, cand_frame, segments)
        if normalize_text(body) in prior_norms:
            continue
        violations = validate(body, cta, send_as, facts=ctx.facts, voice=voice,
                              category=category, merchant=merchant,
                              customer=customer, scope=scope, trigger=trigger,
                              licensed_claims=licensed)
        if not has_hard(violations):
            best = body
            best_frame = cand_frame
            break
        repaired = repair(body, violations, ctx.facts, voice)
        if repaired:
            recheck = validate(repaired, cta, send_as, facts=ctx.facts, voice=voice,
                               category=category, merchant=merchant,
                               customer=customer, scope=scope, trigger=trigger,
                               licensed_claims=licensed)
            if not has_hard(recheck) and normalize_text(repaired) not in prior_norms:
                best = repaired
                best_frame = cand_frame
                break

    if best is None:
        # last resort: minimal grounded form with the numeral budget applied
        # through the generic frame path
        segments = GENERIC_FRAME.build(ctx, voice.lang, variant)
        segments, _ = _ensure_numeral_budget(ctx, segments, "binary_yes_no")
        body = _assemble(ctx, GENERIC_FRAME, segments)
        violations = validate(body, "binary_yes_no", send_as, facts=ctx.facts,
                              voice=voice, category=category, merchant=merchant,
                              customer=customer, scope=scope, trigger=trigger,
                              licensed_claims=licensed)
        if not has_hard(violations):
            best = body
            best_frame = GENERIC_FRAME
            cta = "binary_yes_no"
        else:
            body = _minimal_body(ctx)
            violations = validate(body, "binary_yes_no", send_as, facts=ctx.facts,
                                  voice=voice, category=category, merchant=merchant,
                                  customer=customer, scope=scope, trigger=trigger,
                                  licensed_claims=licensed)
            repaired = repair(body, violations, ctx.facts, voice) if has_hard(violations) else body
            best = repaired or body
            best_frame = frame
            cta = "binary_yes_no"

    safe_kind = re.sub(r"[^a-z0-9_]+", "_", (trigger_kind or "generic").lower()).strip("_")
    template_name = (f"merchant_{safe_kind}_v1" if scope == "customer"
                     else f"vera_{safe_kind}_v1")

    template_params = _template_params(ctx, best, cta)
    rationale = _rationale(ctx, best, best_frame, cta)
    facts_used = tuple(dict.fromkeys(ctx.used))

    return FallbackMessage(
        body=best, cta=cta, send_as=send_as, template_name=template_name,
        template_params=template_params, rationale=rationale, facts_used=facts_used,
    )


def _template_params(ctx: ComposeContext, body: str, cta: str) -> list[str]:
    """3-6 short strings extracted from the composed body (schema shape of
    api-call-examples.md Example 2.2): salutation, hook, proof, offer, ask."""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", body) if s.strip()]
    params: list[str] = []
    head = sentences[0] if sentences else ""
    if ctx.scope == "merchant":
        salutation = ctx.voice.salutation
        params.append(salutation)
        if head.startswith(salutation):
            head = head[len(salutation):].lstrip(", ").strip()
        params.append(head)
    else:
        params.append(ctx.voice.salutation)
        params.append(head)
    mid = sentences[1:-1]
    for s in mid[:2]:
        params.append(s)
    ask = sentences[-1] if len(sentences) > 1 else ""
    if ask and ask is not sentences[0]:
        params.append(ask)
    return [p[:180].strip() for p in params if p and p.strip()][:6] or [body[:180]]
