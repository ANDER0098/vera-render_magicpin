"""Black-box HTTP contract checks for the Magicpin Vera challenge bot.

The official starter pack's reference module is ``bot.py``. Set BOT_MODULE=app
if the implementation is instead exposed as ``app:app``.
"""

from __future__ import annotations

import importlib
import os
import re
import uuid
from datetime import datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> TestClient:
    module_name = os.environ.get("BOT_MODULE", "bot")
    module = importlib.import_module(module_name)
    return TestClient(module.app)


def token() -> str:
    return uuid.uuid4().hex[:12]


def push_context(
    client: TestClient,
    scope: str,
    context_id: str,
    version: int = 1,
    payload: dict[str, Any] | None = None,
) -> Any:
    response = client.post(
        "/v1/context",
        json={
            "scope": scope,
            "context_id": context_id,
            "version": version,
            "delivered_at": "2026-09-26T10:00:00Z",
            "payload": payload or {},
        },
    )
    return response


def assert_context_ack(response: Any, _context_id: str, _version: int = 1) -> None:
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["accepted"] is True
    assert isinstance(data["ack_id"], str) and data["ack_id"]
    assert isinstance(data["stored_at"], str) and data["stored_at"]
    datetime.fromisoformat(data["stored_at"].replace("Z", "+00:00"))


def install_merchant_trigger(
    client: TestClient,
    *,
    expired: bool = False,
    category_digest_title: str | None = None,
) -> dict[str, str]:
    suffix = token()
    category_id = f"dentists-{suffix}"
    merchant_id = f"m_test_{suffix}"
    trigger_id = f"trg_test_{suffix}"
    digest_id = f"d_test_{suffix}"
    title = category_digest_title or "Fluoride recall evidence for adult patients"

    category = {
        "slug": "dentists",
        "voice": {
            "tone": "peer_clinical",
            "vocab_allowed": ["fluoride varnish", "caries"],
            "vocab_taboo": ["guaranteed", "100% safe"],
        },
        "offer_catalog": [
            {"id": f"offer_{suffix}", "title": "Dental Cleaning @ ₹299", "value": "299"}
        ],
        "peer_stats": {"avg_ctr": 0.03},
        "digest": [
            {
                "id": digest_id,
                "kind": "research",
                "title": title,
                "source": "JIDA Oct 2026, p.14",
                "summary": title,
            }
        ],
    }
    merchant = {
        "merchant_id": merchant_id,
        "category_slug": category_id,
        "identity": {
            "name": f"Dr. Mira Dental Clinic {suffix}",
            "owner_first_name": "Mira",
            "city": "Delhi",
            "locality": "Lajpat Nagar",
            "languages": ["en", "hi"],
        },
        "subscription": {"status": "active", "plan": "Pro", "days_remaining": 4},
        "performance": {
            "window_days": 30,
            "views": 2410,
            "calls": 18,
            "directions": 45,
            "ctr": 0.021,
            "delta_7d": {"views_pct": 0.18, "calls_pct": -0.05},
        },
        "offers": [{"id": f"offer_{suffix}", "title": "Dental Cleaning @ ₹299", "status": "active"}],
        "signals": ["ctr_below_peer_median"],
    }
    trigger = {
        "id": trigger_id,
        "scope": "merchant",
        "kind": "research_digest",
        "source": "external",
        "merchant_id": merchant_id,
        "customer_id": None,
        "payload": {"category": "dentists", "top_item_id": digest_id},
        "urgency": 5,
        "suppression_key": f"research:{suffix}",
        "expires_at": "2020-01-01T00:00:00Z" if expired else "2099-01-01T00:00:00Z",
    }

    for scope, context_id, payload in (
        ("category", category_id, category),
        ("merchant", merchant_id, merchant),
        ("trigger", trigger_id, trigger),
    ):
        assert_context_ack(push_context(client, scope, context_id, payload=payload), context_id)

    return {
        "category_id": category_id,
        "merchant_id": merchant_id,
        "trigger_id": trigger_id,
        "digest_id": digest_id,
        "title": title,
    }


def assert_action_contract(action: dict[str, Any]) -> None:
    required = {
        "conversation_id",
        "merchant_id",
        "customer_id",
        "send_as",
        "trigger_id",
        "template_name",
        "template_params",
        "body",
        "cta",
        "suppression_key",
        "rationale",
    }
    assert required <= action.keys()
    for field in ("conversation_id", "merchant_id", "trigger_id", "template_name", "body", "cta", "suppression_key", "rationale"):
        assert isinstance(action[field], str) and action[field].strip(), field
    assert action["customer_id"] is None or isinstance(action["customer_id"], str)
    assert action["send_as"] in {"vera", "merchant_on_behalf"}
    assert isinstance(action["template_params"], list)
    assert not re.search(r"(?:https?://|www\.)", action["body"], re.IGNORECASE)


def assert_reply_contract(data: dict[str, Any]) -> None:
    assert data["action"] in {"send", "wait", "end"}
    assert isinstance(data.get("rationale"), str) and data["rationale"].strip()
    if data["action"] == "send":
        assert isinstance(data.get("body"), str) and data["body"].strip()
        assert isinstance(data.get("cta"), str)
        assert not re.search(r"(?:https?://|www\.)", data["body"], re.IGNORECASE)
    elif data["action"] == "wait":
        assert isinstance(data.get("wait_seconds"), int) and data["wait_seconds"] > 0


def test_healthz_schema_and_context_counts(client: TestClient) -> None:
    initial = client.get("/v1/healthz")
    assert initial.status_code == 200
    before = initial.json()
    assert before["status"] == "ok"
    assert isinstance(before["uptime_seconds"], int) and before["uptime_seconds"] >= 0
    counts_before = before["contexts_loaded"]
    assert set(counts_before) == {"category", "merchant", "customer", "trigger"}

    for scope in ("category", "merchant", "customer", "trigger"):
        context_id = f"health_{scope}_{token()}"
        assert_context_ack(push_context(client, scope, context_id), context_id)

    after = client.get("/v1/healthz")
    assert after.status_code == 200
    counts_after = after.json()["contexts_loaded"]
    for scope in counts_before:
        assert counts_after[scope] == counts_before[scope] + 1


def test_metadata_has_all_seven_required_fields(client: TestClient) -> None:
    response = client.get("/v1/metadata")
    assert response.status_code == 200
    data = response.json()
    required = {
        "team_name",
        "team_members",
        "model",
        "approach",
        "contact_email",
        "version",
        "submitted_at",
    }
    assert required <= data.keys()
    assert isinstance(data["team_members"], list)
    assert all(isinstance(member, str) for member in data["team_members"])
    for field in required - {"team_members"}:
        assert isinstance(data[field], str) and data[field].strip(), field


def test_context_versions_are_monotonic_and_stale_push_returns_409(client: TestClient) -> None:
    context_id = f"versioned_{token()}"
    assert_context_ack(push_context(client, "merchant", context_id, 1, {"marker": "v1"}), context_id, 1)
    assert_context_ack(push_context(client, "merchant", context_id, 2, {"marker": "v2"}), context_id, 2)

    for stale_version in (2, 1):
        response = push_context(client, "merchant", context_id, stale_version, {"marker": "stale"})
        assert response.status_code == 409
        assert response.json() == {
            "accepted": False,
            "reason": "stale_version",
            "current_version": 2,
        }


def test_context_rejects_unknown_scope_with_400(client: TestClient) -> None:
    response = push_context(client, "unknown_scope", f"bad_scope_{token()}")
    assert response.status_code == 400
    assert response.headers.get("content-type", "").startswith("application/json")
    data = response.json()
    assert data["accepted"] is False
    assert data["reason"] == "invalid_scope"
    assert isinstance(data.get("details"), str)


def test_tick_accepts_expired_trigger_and_returns_complete_action(client: TestClient) -> None:
    scenario = install_merchant_trigger(client, expired=True)
    response = client.post(
        "/v1/tick",
        json={"now": "2026-09-26T12:00:00Z", "available_triggers": [scenario["trigger_id"]]},
    )
    assert response.status_code == 200, response.text
    actions = response.json()["actions"]
    assert 1 <= len(actions) <= 20
    matching = [action for action in actions if action["trigger_id"] == scenario["trigger_id"]]
    assert matching, "expired triggers must not be silently discarded"
    for action in actions:
        assert_action_contract(action)


def test_tick_may_return_no_actions_when_no_trigger_is_available(client: TestClient) -> None:
    response = client.post(
        "/v1/tick",
        json={"now": "2026-09-26T12:00:00Z", "available_triggers": []},
    )
    assert response.status_code == 200
    assert response.json() == {"actions": []}


def test_customer_trigger_without_pushed_customer_context_degrades_gracefully(client: TestClient) -> None:
    scenario = install_merchant_trigger(client)
    customer_id = f"c_missing_{token()}"
    trigger_id = f"trg_recall_{token()}"
    trigger = {
        "id": trigger_id,
        "scope": "customer",
        "kind": "recall_due",
        "source": "internal",
        "merchant_id": scenario["merchant_id"],
        "customer_id": customer_id,
        "payload": {
            "service_due": "six-month cleaning recall",
            "available_slots": ["Wed 6pm", "Thu 5pm"],
        },
        "urgency": 4,
        "suppression_key": f"recall:{customer_id}",
        "expires_at": "2099-01-01T00:00:00Z",
    }
    assert_context_ack(push_context(client, "trigger", trigger_id, payload=trigger), trigger_id)

    response = client.post(
        "/v1/tick",
        json={"now": "2026-09-26T12:00:00Z", "available_triggers": [trigger_id]},
    )
    assert response.status_code == 200, response.text
    actions = response.json()["actions"]
    matching = [action for action in actions if action["trigger_id"] == trigger_id]
    assert matching, "missing customer context must not crash or drop a usable trigger"
    assert_action_contract(matching[0])
    assert matching[0]["customer_id"] == customer_id
    assert matching[0]["send_as"] == "merchant_on_behalf"


def test_reply_cold_starts_unknown_conversation_and_allows_missing_optional_ids(client: TestClient) -> None:
    response = client.post(
        "/v1/reply",
        json={
            "conversation_id": f"unknown_{token()}",
            "from_role": "merchant",
            "message": "Can you tell me what this is about?",
            "received_at": "2026-09-26T12:00:00Z",
            "turn_number": 2,
        },
    )
    assert response.status_code == 200, response.text
    assert_reply_contract(response.json())


def test_reply_after_commit_moves_to_action_without_qualifying_question(client: TestClient) -> None:
    response = client.post(
        "/v1/reply",
        json={
            "conversation_id": f"commit_{token()}",
            "merchant_id": f"unknown_merchant_{token()}",
            "from_role": "merchant",
            "message": "Ok, let's do it. What's next?",
            "received_at": "2026-09-26T12:00:00Z",
            "turn_number": 3,
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert_reply_contract(data)
    assert data["action"] == "send"
    body = data["body"].lower()
    assert any(word in body for word in ("done", "sending", "draft", "here", "confirm", "proceed", "next"))
    assert not any(phrase in body for phrase in ("would you", "do you", "can you tell", "what if", "how about"))


def test_repeated_auto_reply_is_fingerprinted_across_conversation_ids(client: TestClient) -> None:
    merchant_id = f"m_auto_{token()}"
    canned = "Thank you for contacting our clinic! Our team will respond shortly."
    outcomes = []
    for turn_number in (2, 3, 4):
        response = client.post(
            "/v1/reply",
            json={
                "conversation_id": f"conv_auto_{token()}",
                "merchant_id": merchant_id,
                "from_role": "merchant",
                "message": canned,
                "received_at": "2026-09-26T12:00:00Z",
                "turn_number": turn_number,
            },
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert_reply_contract(data)
        outcomes.append(data["action"])
    assert outcomes == ["send", "wait", "end"]


def test_hostile_opt_out_ends_or_sends_a_clear_apology(client: TestClient) -> None:
    response = client.post(
        "/v1/reply",
        json={
            "conversation_id": f"hostile_{token()}",
            "merchant_id": f"m_hostile_{token()}",
            "from_role": "merchant",
            "message": "Why are you bothering me? This is useless. Stop messaging me.",
            "received_at": "2026-09-26T12:00:00Z",
            "turn_number": 2,
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert_reply_contract(data)
    assert data["action"] in {"end", "send"}
    if data["action"] == "send":
        body = data["body"].lower()
        assert any(token in body for token in ("sorry", "apolog", "won't"))
