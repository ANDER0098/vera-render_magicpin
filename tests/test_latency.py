from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient


class SlowProvider:
    name = "slow"

    def __init__(self):
        self.release = threading.Event()

    def complete(self, system, user, **kwargs):
        self.release.wait(30)
        return "{}"


class SlowAdapter:
    name = "slow"
    model_name = "slow-test"

    def __init__(self):
        self.provider = SlowProvider()
        self.cache = type("Cache", (), {"clear": lambda self: None})()

    def complete(self, *args, **kwargs):
        return self.provider.complete(*args, **kwargs)


def test_slow_provider_cannot_hold_tick_or_reply_past_budget(root, monkeypatch):
    import app

    client = TestClient(app.app)
    suffix = str(time.monotonic_ns())
    category_id, merchant_id, trigger_id = f"dentists-{suffix}", f"m-latency-{suffix}", f"t-latency-{suffix}"
    category = json.loads((root / "dataset" / "categories" / "dentists.json").read_text(encoding="utf-8"))
    merchant = json.loads((root / "dataset" / "merchants_seed.json").read_text(encoding="utf-8"))["merchants"][0]
    merchant = dict(merchant, merchant_id=merchant_id, category_slug=category_id)
    trigger = {"id": trigger_id, "scope": "merchant", "kind": "research_digest",
               "merchant_id": merchant_id, "urgency": 3, "suppression_key": trigger_id,
               "payload": {}, "expires_at": "2000-01-01T00:00:00Z"}
    for scope, cid, payload in (("category", category_id, category), ("merchant", merchant_id, merchant),
                                ("trigger", trigger_id, trigger)):
        assert client.post("/v1/context", json={"scope": scope, "context_id": cid,
                            "version": 1, "payload": payload}).status_code == 200
    slow = SlowAdapter()
    monkeypatch.setattr(app, "adapter", slow)
    start = time.monotonic()
    response = client.post("/v1/tick", json={"now": "2026-09-26T12:00:00Z",
                                             "available_triggers": [trigger_id]})
    elapsed = time.monotonic() - start
    assert response.status_code == 200 and elapsed < 8
    assert response.json().get("actions") is not None
    start = time.monotonic()
    reply_response = client.post("/v1/reply", json={"conversation_id": f"slow-{suffix}",
        "merchant_id": merchant_id, "from_role": "merchant", "message": "Can you help?",
        "received_at": "2026-09-26T12:00:00Z", "turn_number": 2})
    assert reply_response.status_code == 200 and time.monotonic() - start < 8
    assert reply_response.json()["action"] in {"send", "wait", "end"}
    slow.provider.release.set()
