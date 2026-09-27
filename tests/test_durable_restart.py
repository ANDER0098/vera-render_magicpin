from __future__ import annotations

import asyncio

from vera.store import ContextStore


def test_255_contexts_survive_rehydration_and_replay_is_idempotent(tmp_path):
    journal = tmp_path / "journal.jsonl"
    first = ContextStore(journal)

    async def push_all():
        tasks = []
        for index in range(5):
            tasks.append(first.put("category", f"cat{index}", 1, {"slug": f"cat{index}"}))
        for index in range(50):
            merchant = f"m{index}"
            tasks.append(first.put("merchant", merchant, 1,
                                   {"merchant_id": merchant, "category_slug": f"cat{index % 5}"}))
        for index in range(200):
            tasks.append(first.put("customer", f"c{index}", 1,
                                   {"merchant_id": f"m{index % 50}"}))
        return await asyncio.gather(*tasks)

    outcomes = asyncio.run(push_all())
    assert all(result.accepted for result in outcomes)
    before = first.counts()
    del first
    second = ContextStore(journal)
    assert second.counts() == before == {"category": 5, "merchant": 50, "customer": 200, "trigger": 0}
    duplicate = asyncio.run(second.put("merchant", "m0", 1,
                                       {"merchant_id": "m0", "category_slug": "cat0"}))
    assert not duplicate.accepted and duplicate.reason == "stale_version"
    assert second.counts() == before
