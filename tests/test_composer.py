from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from vera.composer import compose
from vera.config import Settings
from vera.fallbacks.compose import compose_fallback
from vera.grounding.validator import has_hard, validate
from vera.llm.adapter import LLMAdapter
from vera.llm.cache import ResponseCache
from vera.llm.providers import NoopProvider
from vera.signals.derived import derive_facts
from vera.signals.relevance import attach_conditionals
from vera.voice import resolve_voice


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


class FakeProvider:
    name = "fake"
    model = "fake-model"

    def __init__(self, value):
        self.value = value
        self.calls = 0

    def complete(self, system, user, *, temperature=0.0, max_tokens=900, timeout=6.0, seed=0):
        self.calls += 1
        return self.value


def make_item(root: Path, trigger):
    merchant = load(root / "expanded" / "merchants" / f"{trigger['merchant_id']}.json")
    category = load(root / "expanded" / "categories" / f"{merchant['category_slug']}.json")
    customer = None
    if trigger.get("customer_id"):
        customer = load(root / "expanded" / "customers" / f"{trigger['customer_id']}.json")
    return SimpleNamespace(trigger=trigger, trigger_id=trigger["id"], merchant=merchant,
                           merchant_id=merchant["merchant_id"], category=category, customer=customer,
                           customer_id=trigger.get("customer_id"), conversation_id=f"conv_test_{trigger['id']}",
                           suppression_key=trigger.get("suppression_key") or trigger["id"],
                           cta_shape="open_ended")


def test_noop_provider_composes_all_100_expanded_triggers(root, tmp_path):
    categories = list((root / "expanded" / "triggers").glob("*.json"))
    now = datetime(2026, 4, 26, 10, 30, tzinfo=timezone.utc)
    adapter = LLMAdapter([NoopProvider()], ResponseCache(tmp_path / "noop-cache.sqlite3"))
    settings = Settings(compose_budget=.2, llm_timeout=.2)
    assert len(categories) == 100
    for path in categories:
        trigger = load(path)
        item = make_item(root, trigger)
        action = asyncio.run(compose(item, now, adapter, settings))
        assert set(action) >= {"conversation_id", "merchant_id", "customer_id", "send_as", "trigger_id",
                               "template_name", "template_params", "body", "cta", "suppression_key", "rationale"}
        assert action["body"].strip()
        facts = attach_conditionals(
            derive_facts(item.category, item.merchant, trigger, item.customer, now),
            category=item.category, merchant=item.merchant, trigger=trigger,
            customer=item.customer, now=now)
        voice = resolve_voice(item.category, item.merchant, item.customer, trigger)
        check = validate(action["body"], action["cta"], action["send_as"], facts=facts, voice=voice,
                         category=item.category, merchant=item.merchant, customer=item.customer,
                         scope=trigger.get("scope"), trigger=trigger)
        assert not has_hard(check), f"{trigger['id']}: {[v.code for v in check if v.severity == 'hard']}"


def test_fake_provider_good_bad_and_cache(root, tmp_path):
    trigger = load(next((root / "expanded" / "triggers").glob("*.json")))
    item = make_item(root, trigger)
    now = datetime(2026, 4, 26, 10, 30, tzinfo=timezone.utc)
    facts = derive_facts(item.category, item.merchant, trigger, item.customer, now)
    voice = resolve_voice(item.category, item.merchant, item.customer, trigger)
    floor = compose_fallback(trigger["kind"], category=item.category, merchant=item.merchant,
                             trigger=trigger, customer=item.customer, facts=facts, voice=voice, now=now)
    provider = FakeProvider(json.dumps({"body": floor.body, "cta": floor.cta, "send_as": floor.send_as}))
    adapter = LLMAdapter([provider], ResponseCache(tmp_path / "cache.sqlite3"), timeout=2)
    import vera.composer as module
    module._action_cache.clear()
    good = asyncio.run(compose(item, now, adapter, Settings(compose_budget=2, llm_timeout=2)))
    assert good["body"] == floor.body and provider.calls == 1
    again = asyncio.run(compose(item, now, adapter, Settings(compose_budget=2, llm_timeout=2)))
    assert again == good and provider.calls == 1

    for invalid in (
        json.dumps({"body": floor.body + " Rs.199", "cta": floor.cta, "send_as": floor.send_as}),
        json.dumps({"body": floor.body + " Read https://example.com", "cta": floor.cta, "send_as": floor.send_as}),
        json.dumps({"body": floor.body + " Shall I send? Would you like it?", "cta": floor.cta, "send_as": floor.send_as}),
        "not-json",
        json.dumps({"body": "", "cta": floor.cta, "send_as": floor.send_as}),
    ):
        module._action_cache.clear()
        adapter.cache.clear()
        provider.value = invalid
        result = asyncio.run(compose(item, now, adapter, Settings(compose_budget=2, llm_timeout=2)))
        assert result["body"]
        result_facts = derive_facts(item.category, item.merchant, trigger, item.customer, now)
        result_voice = resolve_voice(item.category, item.merchant, item.customer, trigger)
        violations = validate(result["body"], result["cta"], result["send_as"], facts=result_facts,
                              voice=result_voice, category=item.category, merchant=item.merchant,
                              customer=item.customer, scope=trigger.get("scope"), trigger=trigger)
        assert not has_hard(violations)
