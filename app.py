"""FastAPI HTTP surface for the Magicpin Vera challenge."""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from vera.composer import compose
from vera.config import Settings
from vera.conversation import ConversationManager
from vera.llm.adapter import build_adapter
from vera.observability import metrics
from vera.planner import EPOCH, plan
from vera.prompts.registry import PromptRegistry
from vera.store import ContextStore, VALID_SCOPES
from vera.signals.derived import parse_dt

logging.basicConfig(level=os.environ.get("VERA_LOG_LEVEL", "INFO"))
log = logging.getLogger("vera.app")

try:
    importlib.import_module("harness.internal_vocab")
except Exception as exc:
    raise RuntimeError("harness.internal_vocab must be importable at server startup") from exc

settings = Settings.from_env()
store = ContextStore(settings.state_dir / "journal.jsonl")
conversations = ConversationManager(settings.state_dir / "conversations.jsonl")
adapter = build_adapter(settings)
prompts = PromptRegistry()
_started_at = time.monotonic()
app = FastAPI(title="Magicpin Vera", docs_url=None, redoc_url=None)


def _clock_now() -> datetime:
    """The one wall-clock boundary; core modules receive this value explicitly."""
    return datetime.now(timezone.utc)


def _iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


async def _body(request: Request) -> dict[str, Any] | None:
    try:
        value = await request.json()
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def _tick_now(raw: Any) -> datetime:
    parsed = parse_dt(raw)
    if parsed is not None:
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    delivered = [parse_dt(record.delivered_at) for record in store.snapshot() if record.delivered_at]
    valid = [value for value in delivered if value is not None]
    if valid:
        def utc_key(value: datetime) -> float:
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.timestamp()
        return max(valid, key=utc_key)
    return EPOCH


@app.get("/v1/healthz")
async def healthz() -> JSONResponse:
    # No disk or network calls in the liveness handler.
    try:
        counts = store.counts()
        payload = {"status": "ok", "uptime_seconds": max(0, int(time.monotonic() - _started_at)),
                   "contexts_loaded": {scope: int(counts.get(scope, 0)) for scope in VALID_SCOPES}}
    except Exception:
        log.exception("healthz_store_corrupt")
        payload = {"status": "ok", "uptime_seconds": 0,
                   "contexts_loaded": {scope: 0 for scope in VALID_SCOPES}}
    return JSONResponse(payload)


@app.get("/v1/metadata")
async def metadata() -> JSONResponse:
    runtime_model = adapter.model_name if adapter.name != "deterministic-fallback" else "deterministic-fallback"
    return JSONResponse({"team_name": settings.team_name,
                         "team_members": list(settings.team_members),
                         "model": runtime_model or "deterministic-fallback",
                         "approach": settings.approach,
                         "contact_email": settings.contact_email or "not-provided@example.invalid",
                         "version": settings.version,
                         "submitted_at": settings.submitted_at or "not submitted"})


@app.post("/v1/context")
async def push_context(request: Request) -> JSONResponse:
    data = await _body(request)
    if data is None:
        return JSONResponse({"accepted": False, "reason": "invalid_payload",
                             "details": "Request body must be a JSON object."}, status_code=400)
    scope = data.get("scope")
    context_id = data.get("context_id")
    version = data.get("version")
    payload = data.get("payload")
    if scope not in VALID_SCOPES:
        return JSONResponse({"accepted": False, "reason": "invalid_scope",
                             "details": "scope must be category, merchant, customer, or trigger."}, status_code=400)
    if not isinstance(context_id, str) or not context_id.strip():
        return JSONResponse({"accepted": False, "reason": "invalid_context",
                             "details": "context_id must be a non-empty string."}, status_code=400)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        return JSONResponse({"accepted": False, "reason": "invalid_version",
                             "details": "version must be an integer greater than or equal to 1."}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"accepted": False, "reason": "invalid_payload",
                             "details": "payload must be a JSON object."}, status_code=400)
    now = _clock_now()
    delivered_at = data.get("delivered_at")
    result = await store.put(scope, context_id.strip(), version, payload,
                             received_at=_iso_z(now), delivered_at=str(delivered_at or ""))
    if not result.accepted:
        if result.reason == "stale_version":
            return JSONResponse({"accepted": False, "reason": "stale_version",
                                 "current_version": result.current_version}, status_code=409)
        return JSONResponse({"accepted": False, "reason": result.reason,
                             "details": "Context could not be stored."}, status_code=400)
    metrics.event("context_accepted", scope=scope, context_id=context_id, version=version)
    return JSONResponse({"accepted": True, "ack_id": f"ack_{context_id}_v{version}",
                         "stored_at": _iso_z(now)})


@app.post("/v1/tick")
async def tick(request: Request) -> JSONResponse:
    data = await _body(request)
    if data is None:
        data = {}
    now = _tick_now(data.get("now"))
    raw_ids = data.get("available_triggers")
    ids = [str(value) for value in raw_ids if isinstance(value, (str, int))] if isinstance(raw_ids, list) else []
    planned = plan(now, ids, store, conversations, settings)
    if not planned:
        return JSONResponse({"actions": []})
    tasks = [asyncio.create_task(compose(
        item, now, adapter, settings,
        prior_bodies=(conversations.get(item.conversation_id).bot_bodies_sent
                      if conversations.get(item.conversation_id) else []),
        history=item.merchant.get("conversation_history") or [], registry=prompts))
        for item in planned]
    done, pending = await asyncio.wait(tasks, timeout=7.35)
    for task in pending:
        task.cancel()
    actions: list[dict[str, Any]] = []
    stamp = _iso_z(now)
    for index, task in enumerate(tasks):
        if task not in done:
            continue
        try:
            action = task.result()
            if not _valid_action(action):
                continue
            actions.append(action)
            item = planned[index]
            conversations.mark_send(item.conversation_id, item.merchant_id, stamp,
                                    item.suppression_key, action["body"], item.trigger_id,
                                    item.customer_id, action["cta"])
        except Exception:
            log.exception("tick_composition_failed")
    metrics.event("tick_finished", selected=len(planned), returned=len(actions))
    return JSONResponse({"actions": actions[:20]})


@app.post("/v1/reply")
async def reply(request: Request) -> JSONResponse:
    data = await _body(request)
    if data is None:
        data = {}
    try:
        result = conversations.handle(data, store)
        if not _valid_reply(result):
            result = {"action": "end", "rationale": "Reply could not be safely composed; conversation closed."}
        return JSONResponse(result)
    except Exception:
        log.exception("reply_handling_failed")
        return JSONResponse({"action": "end", "rationale": "Reply handling failed safely; conversation closed."})


@app.post("/v1/teardown")
async def teardown() -> JSONResponse:
    store.wipe()
    conversations.reset()
    try:
        adapter.cache.clear()
    except Exception:
        log.exception("cache_teardown_failed")
    return JSONResponse({"torn_down": True})


@app.exception_handler(Exception)
async def catch_all(request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled_route_error path=%s", request.url.path, exc_info=exc)
    if request.url.path.endswith("/tick"):
        return JSONResponse({"actions": []})
    if request.url.path.endswith("/reply"):
        return JSONResponse({"action": "end", "rationale": "Unhandled error; conversation closed safely."})
    if request.url.path.endswith("/context"):
        return JSONResponse({"accepted": False, "reason": "internal_error",
                             "details": "Context could not be stored."})
    return JSONResponse({"status": "ok"})


def _valid_action(action: Any) -> bool:
    fields = {"conversation_id", "merchant_id", "customer_id", "send_as", "trigger_id",
              "template_name", "template_params", "body", "cta", "suppression_key", "rationale"}
    return (isinstance(action, dict) and fields <= action.keys() and
            all(isinstance(action[key], str) and action[key].strip()
                for key in ("conversation_id", "merchant_id", "trigger_id", "template_name",
                            "body", "cta", "suppression_key", "rationale")) and
            action["send_as"] in {"vera", "merchant_on_behalf"} and
            isinstance(action["template_params"], list) and
            (action["customer_id"] is None or isinstance(action["customer_id"], str)))


def _valid_reply(value: Any) -> bool:
    if not isinstance(value, dict) or not isinstance(value.get("rationale"), str) or not value["rationale"].strip():
        return False
    if value.get("action") == "send":
        return bool(isinstance(value.get("body"), str) and value["body"].strip() and
                    isinstance(value.get("cta"), str))
    if value.get("action") == "wait":
        return isinstance(value.get("wait_seconds"), int) and value["wait_seconds"] > 0
    return value.get("action") == "end"
