"""Deterministic-first grounded composition with a bounded optional LLM race."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import threading
from datetime import datetime
from functools import lru_cache
from typing import Any

from .config import Settings
from .fallbacks.compose import compose_fallback
from .grounding.validator import grounding_map, has_hard, repair, validate
from .observability import metrics
from .prompts.registry import PromptRegistry
from .signals.derived import derive_facts
from .signals.diagnose import diagnose
from .signals.relevance import attach_conditionals
from .text.normalize import normalize_text
from .voice import localize_numbers, resolve_voice

log = logging.getLogger("vera.composer")
_action_cache: dict[str, dict[str, Any]] = {}


@lru_cache(maxsize=1)
def _case_ngrams() -> set:
    try:
        from harness.case_corpus import load_case_ngrams
        return load_case_ngrams(8)
    except Exception:
        return set()


async def compose(item: Any, now: datetime, adapter: Any,
                  settings: Settings | None = None, *, prior_bodies: list[str] | None = None,
                  history: list[dict] | None = None, registry: PromptRegistry | None = None) -> dict[str, Any]:
    settings = settings or Settings.from_env()
    registry = registry or PromptRegistry()
    trigger, merchant, category, customer = item.trigger, item.merchant, item.category, item.customer
    facts = derive_facts(category, merchant, trigger, customer, now)
    facts = attach_conditionals(facts, category=category, merchant=merchant,
                                trigger=trigger, customer=customer, now=now)
    voice = resolve_voice(category, merchant, customer, trigger, history or [])
    diagnosis = diagnose(facts, merchant=merchant, category=category,
                         trigger=trigger, now=now)
    licensed_claims = diagnosis.licensed_claims if diagnosis else ()
    scope = str(trigger.get("scope") or "merchant")
    prior = list(prior_bodies or [])
    floor = compose_fallback(str(trigger.get("kind") or "generic"), category=category,
                             merchant=merchant, trigger=trigger, customer=customer,
                             facts=facts, voice=voice, now=now, prior_bodies=prior)
    floor_check = _validate_floor(floor, facts, voice, category, merchant, customer,
                                  trigger, scope, licensed_claims, prior)
    if has_hard(floor_check):
        # The grounding lane's deterministic floor predates its latest strict
        # checks. Try alternate grounded fact subsets before considering a send.
        for alternate_facts in _fallback_fact_variants(facts):
            alternate_diagnosis = diagnose(alternate_facts, merchant=merchant,
                                           category=category, trigger=trigger, now=now)
            alternate_floor = compose_fallback(
                str(trigger.get("kind") or "generic"), category=category,
                merchant=merchant, trigger=trigger, customer=customer,
                facts=alternate_facts, voice=voice, now=now, prior_bodies=prior)
            alternate_check = _validate_floor(
                alternate_floor, alternate_facts, voice, category, merchant,
                customer, trigger, scope,
                alternate_diagnosis.licensed_claims if alternate_diagnosis else (), prior)
            if not has_hard(alternate_check):
                facts, diagnosis, floor = alternate_facts, alternate_diagnosis, alternate_floor
                licensed_claims = diagnosis.licensed_claims if diagnosis else ()
                floor_check = alternate_check
                break
    if has_hard(floor_check):
        metrics.event("composer_floor_rejected", trigger_id=item.trigger_id,
                      codes=sorted({v.code for v in floor_check if v.severity == "hard"}))
        raise RuntimeError("No hard-violation-free fallback candidate")
    action = {
        "conversation_id": item.conversation_id,
        "merchant_id": item.merchant_id,
        "customer_id": item.customer_id,
        "send_as": floor.send_as,
        "trigger_id": item.trigger_id,
        "template_name": floor.template_name,
        "template_params": list(floor.template_params),
        "body": floor.body,
        "cta": floor.cta,
        "suppression_key": item.suppression_key,
        "rationale": _rationale(floor.body, facts, voice, diagnosis),
    }
    key = _action_key(item, now, facts, voice)
    cached_action = _action_cache.get(key)
    persistent_cache = getattr(adapter, "cache", None)
    if cached_action is None and persistent_cache is not None and hasattr(persistent_cache, "get"):
        try:
            stored = persistent_cache.get("action:" + key)
            cached_action = json.loads(stored) if stored else None
        except Exception:
            cached_action = None
    if cached_action is not None:
        _action_cache[key] = cached_action
        return json.loads(json.dumps(cached_action, ensure_ascii=False))

    draft_result: tuple[str, str, str] | None = None
    remaining = min(settings.compose_budget, settings.llm_timeout)
    if adapter and adapter.name != "deterministic-fallback" and remaining > 1.5:
        pack = registry.get(str(category.get("slug") or merchant.get("category_slug") or "generic"),
                            str(trigger.get("kind") or "generic"))
        scoped_trigger = dict(trigger)
        scoped_trigger["_merchant"] = merchant
        scoped_trigger["_cta"] = item.cta_shape
        user = pack.render(facts, voice, scoped_trigger, customer, now.isoformat())
        try:
            raw = await _daemon_call(
                adapter.complete, pack.system or "Use supplied facts only. Return JSON body, cta, send_as.",
                user, timeout=remaining, temperature=0.0, max_tokens=700, seed=0,
                prompt_version=pack.version)
            parsed = json.loads(raw)
            if isinstance(parsed, dict) and isinstance(parsed.get("body"), str):
                draft_result = (parsed["body"], str(parsed.get("cta") or floor.cta),
                                str(parsed.get("send_as") or floor.send_as))
        except (asyncio.TimeoutError, Exception) as exc:
            log.info("llm_draft_fallback error=%s", type(exc).__name__)

    if draft_result:
        body, cta, send_as = draft_result
        body = localize_numbers(body, voice.lang)
        violations = validate(body, cta, send_as, facts=facts, voice=voice,
                              category=category, merchant=merchant, customer=customer,
                              prior_bodies=prior, case_corpus_ngrams=_case_ngrams(),
                              scope=scope, trigger=trigger, licensed_claims=licensed_claims)
        if has_hard(violations):
            repaired = repair(body, violations, facts, voice)
            rechecked = validate(repaired or "", cta, send_as, facts=facts, voice=voice,
                                 category=category, merchant=merchant, customer=customer,
                                 prior_bodies=prior, case_corpus_ngrams=_case_ngrams(),
                                 scope=scope, trigger=trigger, licensed_claims=licensed_claims) if repaired else []
            if repaired and not has_hard(rechecked):
                body = repaired
            else:
                body, cta, send_as = floor.body, floor.cta, floor.send_as
        if body != floor.body:
            action.update(body=body, cta=cta, send_as=send_as,
                          template_name=floor.template_name,
                          template_params=list(floor.template_params),
                          rationale=_rationale(body, facts, voice, diagnosis))

    final = validate(action["body"], action["cta"], action["send_as"], facts=facts,
                     voice=voice, category=category, merchant=merchant, customer=customer,
                     prior_bodies=prior, case_corpus_ngrams=_case_ngrams(),
                     scope=scope, trigger=trigger, licensed_claims=licensed_claims)
    if has_hard(final):
        metrics.event("composer_invariant_saved", codes=sorted({v.code for v in final if v.severity == "hard"}))
        action.update(body=floor.body, cta=floor.cta, send_as=floor.send_as,
                      rationale=_rationale(floor.body, facts, voice, diagnosis))
        floor_check = validate(action["body"], action["cta"], action["send_as"], facts=facts,
                               voice=voice, category=category, merchant=merchant,
                               customer=customer, prior_bodies=prior,
                               case_corpus_ngrams=_case_ngrams(), scope=scope,
                               trigger=trigger, licensed_claims=licensed_claims)
        if has_hard(floor_check):
            metrics.event("composer_floor_rejected", trigger_id=item.trigger_id,
                          codes=sorted({v.code for v in floor_check if v.severity == "hard"}))
            raise RuntimeError("Fallback invariant failed after final validation")
    _action_cache[key] = json.loads(json.dumps(action, ensure_ascii=False))
    if persistent_cache is not None and hasattr(persistent_cache, "put"):
        try:
            persistent_cache.put("action:" + key, json.dumps(action, ensure_ascii=False))
        except Exception:
            log.exception("action_cache_write_failed")
    return action


def _rationale(body: str, facts: list[Any], voice: Any, diagnosis: Any) -> str:
    provenance = grounding_map(body, facts, voice)
    pieces = ["Grounding map: " + ("; ".join(provenance) if provenance else "no numeral claims") + "."]
    if diagnosis:
        pieces.append(f"Diagnosis {diagnosis.rule_id}; source paths: {', '.join(diagnosis.json_paths) or 'derived facts'}.")
    used_paths = sorted({fact.json_path for fact in facts
                         if fact.phrase and normalize_text(fact.phrase)[:28] in normalize_text(body)})
    if used_paths:
        pieces.append("Body facts: " + "; ".join(used_paths[:8]) + ".")
    return " ".join(pieces)


def _validate_floor(floor: Any, facts: list[Any], voice: Any, category: dict,
                    merchant: dict, customer: dict | None, trigger: dict,
                    scope: str, licensed_claims: Any, prior: list[str]) -> list[Any]:
    return validate(floor.body, floor.cta, floor.send_as, facts=facts, voice=voice,
                    category=category, merchant=merchant, customer=customer,
                    prior_bodies=prior, case_corpus_ngrams=_case_ngrams(),
                    scope=scope, trigger=trigger, licensed_claims=licensed_claims)


def _fallback_fact_variants(facts: list[Any]) -> list[list[Any]]:
    variants: list[list[Any]] = []
    exclusions = (
        {"metric", "benchmark"},
        {"metric", "benchmark", "delta"},
        {"metric", "benchmark", "delta", "subscription"},
    )
    for kinds in exclusions:
        selected = [fact for fact in facts if fact.kind not in kinds]
        if selected and len(selected) < len(facts):
            variants.append(selected)
    # A single-source fact-dump can sometimes be resolved by dropping only
    # the performance slice while preserving trigger, category and cohort facts.
    keys = {"views", "calls", "directions", "ctr", "peer_avg_views_30d",
            "peer_avg_calls_30d", "peer_avg_rating", "benchmark_avg_ctr"}
    selected = [fact for fact in facts if fact.key not in keys]
    if selected and len(selected) < len(facts):
        variants.append(selected)
    selected = [fact for fact in facts if fact.kind != "trigger"]
    if selected and len(selected) < len(facts):
        variants.append(selected)
    selected = [fact for fact in facts if fact.kind not in {"trigger", "metric", "benchmark", "delta"}]
    if selected and len(selected) < len(facts):
        variants.append(selected)
    return variants


def _action_key(item: Any, now: datetime, facts: list[Any], voice: Any) -> str:
    seed = {"trigger": item.trigger, "merchant": item.merchant, "category": item.category,
            "customer": item.customer, "now": now.isoformat(),
            "facts": [(f.key, f.value, f.json_path) for f in facts],
            "lang": voice.lang, "conversation_id": item.conversation_id}
    raw = json.dumps(seed, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _daemon_call(function: Any, *args: Any, timeout: float, **kwargs: Any) -> Any:
    """Run a synchronous provider in a daemon thread so a stuck SDK-free HTTP
    adapter cannot hold server shutdown after its asyncio deadline expires."""
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    def settle(result: Any = None, error: BaseException | None = None) -> None:
        if future.done():
            return
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(result)

    def worker() -> None:
        try:
            result = function(*args, **kwargs)
        except BaseException as exc:
            try:
                loop.call_soon_threadsafe(settle, None, exc)
            except RuntimeError:
                pass
        else:
            try:
                loop.call_soon_threadsafe(settle, result, None)
            except RuntimeError:
                pass

    threading.Thread(target=worker, name="vera-llm-call", daemon=True).start()
    return await asyncio.wait_for(future, timeout=max(.05, timeout))
