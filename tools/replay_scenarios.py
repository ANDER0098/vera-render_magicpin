"""Keyless HTTP replay of the judge's warmup and three reply scenarios."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _post(client: httpx.Client, path: str, payload: dict | None = None) -> httpx.Response:
    return client.post(path, json=payload) if payload is not None else client.post(path)


def _push(client: httpx.Client, scope: str, cid: str, payload: dict,
          version: int = 1, delivered_at: str = "2026-09-26T10:00:00Z") -> httpx.Response:
    return _post(client, "/v1/context", {"scope": scope, "context_id": cid,
            "version": version, "payload": payload, "delivered_at": delivered_at})


def run_warmup(client: httpx.Client) -> None:
    _post(client, "/v1/teardown")
    health = client.get("/v1/healthz")
    assert health.status_code == 200 and health.json().get("status") == "ok"
    metadata = client.get("/v1/metadata")
    assert metadata.status_code == 200 and metadata.json().get("model")
    category_paths = sorted((ROOT / "dataset" / "categories").glob("*.json"))
    assert len(category_paths) == 5
    for path in category_paths:
        payload = load(path)
        response = _push(client, "category", payload.get("slug") or path.stem, payload)
        assert response.status_code == 200 and response.json().get("accepted") is True
    merchants = load(ROOT / "dataset" / "merchants_seed.json")["merchants"][:5]
    for payload in merchants:
        mid = payload["merchant_id"]
        response = _push(client, "merchant", mid, payload)
        assert response.status_code == 200 and response.json().get("accepted") is True
    duplicate = _push(client, "merchant", merchants[0]["merchant_id"], merchants[0])
    assert duplicate.status_code == 409 and duplicate.json().get("reason") == "stale_version"
    counts = client.get("/v1/healthz").json()["contexts_loaded"]
    assert counts == {"category": 5, "merchant": 5, "customer": 0, "trigger": 0}, counts


def run_replays(client: httpx.Client, latencies: list[float] | None = None) -> dict[str, str]:
    outcomes: dict[str, str] = {}
    canned = "Thank you for contacting us! Our team will respond shortly."
    actions = []
    for index in range(1, 5):
        started = time.monotonic()
        response = _post(client, "/v1/reply", {"conversation_id": f"conv_auto_{index}",
            "merchant_id": "m_auto_replay", "from_role": "merchant", "message": canned,
            "received_at": f"2026-09-26T10:0{index}:00Z", "turn_number": index + 1})
        assert response.status_code == 200, response.text
        if latencies is not None:
            latencies.append(time.monotonic() - started)
        actions.append(response.json().get("action"))
    assert actions[0:3] == ["send", "wait", "end"] and actions[3] == "end", actions
    outcomes["auto_reply_hell"] = "PASS: nudge -> wait >= 86400s -> end by turn 4"
    started = time.monotonic()
    response = _post(client, "/v1/reply", {"conversation_id": "conv_intent_1",
        "merchant_id": "m_intent_replay", "from_role": "merchant",
        "message": "Ok lets do it. Whats next?", "received_at": "2026-09-26T10:10:00Z",
        "turn_number": 2})
    assert response.status_code == 200
    if latencies is not None:
        latencies.append(time.monotonic() - started)
    data = response.json()
    assert data.get("action") == "send" and data.get("body")
    body = data["body"].lower()
    action_tokens = ("done", "sending", "draft", "here", "confirm", "proceed", "next")
    qualifying = ("would you", "do you", "can you tell", "what if", "how about")
    assert any(word in body for word in action_tokens) and not any(word in body for word in qualifying), body
    outcomes["intent_transition"] = "PASS: cold commit moved to action without re-qualification"
    started = time.monotonic()
    response = _post(client, "/v1/reply", {"conversation_id": "conv_hostile",
        "merchant_id": "m_hostile_replay", "from_role": "merchant",
        "message": "Stop messaging me. This is useless spam.",
        "received_at": "2026-09-26T10:11:00Z", "turn_number": 2})
    assert response.status_code == 200
    if latencies is not None:
        latencies.append(time.monotonic() - started)
    data = response.json()
    assert data.get("action") == "end" or (data.get("action") == "send" and
           any(word in data.get("body", "").lower() for word in ("sorry", "apolog", "won't")))
    outcomes["hostile"] = "PASS: hostile opt-out ended and suppressed"
    return outcomes


def run_edge_ticks(client: httpx.Client) -> None:
    category = load(ROOT / "dataset" / "categories" / "dentists.json")
    merchant = load(ROOT / "dataset" / "merchants_seed.json")["merchants"][0]
    merchant_a = dict(merchant, merchant_id="m_replay_missing_customer", category_slug="dentists")
    assert _push(client, "category", "dentists", category, version=1).status_code in (200, 409)
    assert _push(client, "merchant", merchant_a["merchant_id"], merchant_a).status_code == 200
    missing_id = "trg_replay_missing_customer"
    trigger = {"id": missing_id, "scope": "customer", "kind": "recall_due",
               "merchant_id": merchant_a["merchant_id"], "customer_id": "c_never_pushed",
               "urgency": 4, "suppression_key": "replay:missing-customer", "payload": {
                   "service_due": "six-month cleaning recall", "available_slots": ["Wed 6pm", "Thu 5pm"]}}
    assert _push(client, "trigger", missing_id, trigger).status_code == 200
    data = _post(client, "/v1/tick", {"now": "2026-09-26T12:00:00Z",
                                      "available_triggers": [missing_id]}).json()
    actions = data.get("actions") or []
    found = next((action for action in actions if action["trigger_id"] == missing_id), None)
    assert found and found["body"] and found["customer_id"] == "c_never_pushed"
    assert found["send_as"] == "merchant_on_behalf"

    expired_id = "trg_replay_expired"
    merchant_b = dict(merchant, merchant_id="m_replay_expired", category_slug="dentists")
    assert _push(client, "merchant", merchant_b["merchant_id"], merchant_b).status_code == 200
    expired = {"id": expired_id, "scope": "merchant", "kind": "research_digest",
               "merchant_id": merchant_b["merchant_id"], "urgency": 5,
               "suppression_key": "replay:expired", "expires_at": "2000-01-01T00:00:00Z",
               "payload": {}}
    assert _push(client, "trigger", expired_id, expired).status_code == 200
    data = _post(client, "/v1/tick", {"now": "2026-09-26T12:00:00Z",
                                      "available_triggers": [expired_id]}).json()
    assert any(action.get("trigger_id") == expired_id for action in data.get("actions", []))


def run(bot_url: str) -> int:
    with httpx.Client(base_url=bot_url.rstrip("/"), timeout=9.0) as client:
        checks: list[tuple[str, Any]] = [("warmup", lambda: run_warmup(client)),
                                         ("auto_reply_hell / intent_transition / hostile", lambda: run_replays(client)),
                                         ("missing_customer + expired_trigger", lambda: run_edge_ticks(client))]
        for label, fn in checks:
            try:
                result = fn()
                if isinstance(result, dict):
                    for name, status in result.items():
                        print(f"PASS {name}: {status}")
                else:
                    print(f"PASS {label}")
            except Exception as exc:
                print(f"FAIL {label}: {type(exc).__name__}: {exc}")
                return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bot-url", default="http://127.0.0.1:8080")
    args = parser.parse_args()
    try:
        return run(args.bot_url)
    except httpx.HTTPError as exc:
        print(f"FAIL bot unavailable: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
