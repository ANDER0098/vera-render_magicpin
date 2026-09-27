from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from vera.config import Settings
from vera.conversation import ConversationManager
from vera.planner import plan
from vera.store import ContextStore


def put(store, scope, cid, payload):
    return asyncio.run(store.put(scope, cid, 1, payload))


def setup_store(tmp_path, customer=None):
    store = ContextStore(tmp_path / "journal.jsonl")
    put(store, "category", "dentists", {"slug": "dentists", "voice": {},
                                           "peer_stats": {"avg_ctr": .03}})
    put(store, "merchant", "m1", {"merchant_id": "m1", "category_slug": "dentists",
                                    "identity": {"name": "Dr. A"},
                                    "performance": {"views": 100, "calls": 5, "ctr": .02}})
    if customer:
        put(store, "customer", "c1", customer)
    return store


def test_expired_trigger_is_eligible_and_one_send_per_merchant(tmp_path):
    store = setup_store(tmp_path)
    trigger = {"id": "t-expired", "scope": "merchant", "kind": "research_digest",
               "merchant_id": "m1", "urgency": 5, "suppression_key": "s1",
               "expires_at": "2000-01-01T00:00:00Z", "payload": {}}
    put(store, "trigger", "t-expired", trigger)
    other = dict(trigger, id="t-other", suppression_key="s2", kind="perf_dip")
    put(store, "trigger", "t-other", other)
    state = ConversationManager()
    now = datetime(2026, 9, 26, tzinfo=timezone.utc)
    planned = plan(now, ["t-other", "t-expired", "t-expired"], store, state,
                   Settings(action_limit=5))
    assert len(planned) == 1
    assert planned[0].trigger_id in {"t-expired", "t-other"}
    assert "2000" not in planned[0].conversation_id


def test_suppression_optout_and_customer_consent_filtering(tmp_path):
    store = setup_store(tmp_path, {"merchant_id": "m1", "consent": {"scope": []}})
    customer_trigger = {"id": "tc", "scope": "customer", "kind": "recall_due",
                       "merchant_id": "m1", "customer_id": "c1", "suppression_key": "recall"}
    put(store, "trigger", "tc", customer_trigger)
    state = ConversationManager()
    now = datetime(2026, 9, 26, tzinfo=timezone.utc)
    assert not plan(now, ["tc"], store, state)

    merchant_trigger = {"id": "tm", "scope": "merchant", "kind": "research_digest",
                        "merchant_id": "m1", "suppression_key": "sent"}
    put(store, "trigger", "tm", merchant_trigger)
    state.suppressed_keys.add("sent")
    assert not plan(now, ["tm"], store, state)
    state.suppressed_keys.clear()
    state.opted_out_merchants.add("m1")
    assert not plan(now, ["tm"], store, state)


def test_order_and_ids_are_deterministic_for_a_fixed_tick(tmp_path):
    store = setup_store(tmp_path)
    for trigger_id in ("z-trigger", "a-trigger"):
        put(store, "trigger", trigger_id,
            {"id": trigger_id, "scope": "merchant", "kind": "research_digest",
             "merchant_id": "m1", "urgency": 2, "suppression_key": trigger_id})
    now = datetime(2026, 9, 26, tzinfo=timezone.utc)
    outputs = [plan(now, ["z-trigger", "a-trigger"], store, ConversationManager(),
                    Settings(action_limit=5)) for _ in range(100)]
    assert [[x.trigger_id for x in group] for group in outputs] == [["a-trigger"]] * 100
