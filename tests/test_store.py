from __future__ import annotations

import asyncio

from vera.store import ContextStore


def put(store: ContextStore, *args, **kwargs):
    return asyncio.run(store.put(*args, **kwargs))


def test_versions_payload_limit_and_indexes(tmp_path):
    store = ContextStore(tmp_path / "journal.jsonl")
    assert put(store, "merchant", "m1", 1, {"merchant_id": "m1", "category_slug": "salons"}).accepted
    assert put(store, "merchant", "m1", 2, {"merchant_id": "m1", "category_slug": "gyms"}).accepted
    assert put(store, "merchant", "m1", 2, {"marker": "stale"}).reason == "stale_version"
    assert put(store, "merchant", "m1", 1, {"marker": "stale"}).reason == "stale_version"
    assert store.get("merchant", "m1").payload["category_slug"] == "gyms"
    assert store.category_merchants == {"gyms": {"m1"}}
    assert store.merchant_category == {"m1": "gyms"}

    put(store, "customer", "c1", 1, {"merchant_id": "m1"})
    assert store.merchant_customers == {"m1": {"c1"}}
    put(store, "trigger", "t1", 1, {"merchant_id": "m1", "customer_id": "c1"})
    assert store.trigger_links == {"t1": ("m1", "c1")}

    large = {"blob": "x" * 500_000}
    assert put(store, "category", "large", 1, large).accepted
    assert len(store.get("category", "large").payload["blob"]) == 500_000
    assert not put(store, "other", "bad", 1, {}).accepted
    assert not put(store, "category", "bad", 0, {}).accepted
    assert not put(store, "category", "bad", 1, []).accepted


def test_255_concurrent_pushes_keep_highest_versions_and_distinct_counts(tmp_path):
    store = ContextStore(tmp_path / "journal.jsonl")

    async def run():
        tasks = []
        for index in range(252):
            tasks.append(store.put("customer", f"c{index}", 1, {"n": index}))
        tasks.extend((store.put("merchant", "one", 1, {"v": 1}),
                      store.put("merchant", "one", 2, {"v": 2}),
                      store.put("merchant", "one", 1, {"v": 1})))
        return await asyncio.gather(*tasks)

    outcomes = asyncio.run(run())
    assert len(outcomes) == 255
    assert store.counts() == {"category": 0, "merchant": 1, "customer": 252, "trigger": 0}
    assert store.get("merchant", "one").version == 2
    assert store.get("merchant", "one").payload == {"v": 2}
    assert store.get("merchant", "one").version >= max(
        record.version for record in store.snapshot("merchant"))
