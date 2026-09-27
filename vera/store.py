"""Versioned in-memory context store with an append-only restart journal."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

VALID_SCOPES = frozenset({"category", "merchant", "customer", "trigger"})


@dataclass(frozen=True)
class ContextRecord:
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    received_at: str = ""
    delivered_at: str = ""


@dataclass(frozen=True)
class PutResult:
    accepted: bool
    reason: str = ""
    current_version: int | None = None


class ContextStore:
    """Atomic per-context version replacement and durable journal replay."""

    def __init__(self, journal_path: str | Path | None = None) -> None:
        if journal_path is None:
            state_dir = Path(os.environ.get("VERA_STATE_DIR", Path(__file__).resolve().parents[1] / "vera_state"))
            journal_path = state_dir / "journal.jsonl"
        self._journal = Path(journal_path)
        self._records: dict[tuple[str, str], ContextRecord] = {}
        self._key_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._guard = threading.RLock()
        self._journal_guard = threading.Lock()
        self.merchant_category: dict[str, str] = {}
        self.merchant_customers: dict[str, set[str]] = {}
        self.trigger_links: dict[str, tuple[str | None, str | None]] = {}
        self.category_merchants: dict[str, set[str]] = {}
        self._rehydrate()

    def _lock_for(self, key: tuple[str, str]) -> asyncio.Lock:
        with self._guard:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._key_locks[key] = lock
            return lock

    async def put(
        self,
        scope: str,
        context_id: str,
        version: int,
        payload: dict[str, Any],
        *,
        received_at: str = "",
        delivered_at: str = "",
    ) -> PutResult:
        if scope not in VALID_SCOPES or not isinstance(context_id, str) or not context_id:
            return PutResult(False, "invalid_scope" if scope not in VALID_SCOPES else "invalid_context")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            return PutResult(False, "invalid_version")
        if not isinstance(payload, dict):
            return PutResult(False, "invalid_payload")
        key = (scope, context_id)
        async with self._lock_for(key):
            with self._guard:
                current = self._records.get(key)
                if current is not None and version <= current.version:
                    return PutResult(False, "stale_version", current.version)
            record = ContextRecord(
                scope=scope,
                context_id=context_id,
                version=version,
                payload=copy.deepcopy(payload),
                received_at=received_at,
                delivered_at=delivered_at,
            )
            await asyncio.to_thread(self._append_record, record)
            with self._guard:
                self._replace_record(record)
            return PutResult(True, current_version=version)

    def _append_record(self, record: ContextRecord) -> None:
        self._journal.parent.mkdir(parents=True, exist_ok=True)
        entry = json.dumps(asdict(record), ensure_ascii=False, separators=(",", ":"))
        with self._journal_guard, self._journal.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(entry + "\n")
            stream.flush()

    def _rehydrate(self) -> None:
        if not self._journal.exists():
            return
        try:
            with self._journal.open("r", encoding="utf-8") as stream:
                for line in stream:
                    try:
                        item = json.loads(line)
                        record = ContextRecord(
                            scope=item["scope"],
                            context_id=item["context_id"],
                            version=int(item["version"]),
                            payload=item["payload"],
                            received_at=str(item.get("received_at", "")),
                            delivered_at=str(item.get("delivered_at", "")),
                        )
                        if record.scope not in VALID_SCOPES or not isinstance(record.payload, dict):
                            continue
                        key = (record.scope, record.context_id)
                        current = self._records.get(key)
                        if current is None or record.version > current.version:
                            self._replace_record(record)
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                        # A torn/corrupt final journal line must not prevent boot.
                        continue
        except OSError:
            return

    def _remove_indexes(self, record: ContextRecord) -> None:
        payload = record.payload
        if record.scope == "merchant":
            merchant_id = str(payload.get("merchant_id") or record.context_id)
            old_category = self.merchant_category.pop(merchant_id, None)
            if old_category:
                members = self.category_merchants.get(old_category)
                if members:
                    members.discard(merchant_id)
                    if not members:
                        self.category_merchants.pop(old_category, None)
        elif record.scope == "customer":
            merchant_id = str(payload.get("merchant_id") or "")
            if merchant_id:
                members = self.merchant_customers.get(merchant_id)
                if members:
                    members.discard(record.context_id)
                    if not members:
                        self.merchant_customers.pop(merchant_id, None)
        elif record.scope == "trigger":
            self.trigger_links.pop(record.context_id, None)

    def _replace_record(self, record: ContextRecord) -> None:
        key = (record.scope, record.context_id)
        old = self._records.get(key)
        if old is not None:
            self._remove_indexes(old)
        self._records[key] = record
        payload = record.payload
        if record.scope == "merchant":
            merchant_id = str(payload.get("merchant_id") or record.context_id)
            category = str(payload.get("category_slug") or "")
            if category:
                self.merchant_category[merchant_id] = category
                self.category_merchants.setdefault(category, set()).add(merchant_id)
        elif record.scope == "customer":
            merchant_id = str(payload.get("merchant_id") or "")
            if merchant_id:
                self.merchant_customers.setdefault(merchant_id, set()).add(record.context_id)
        elif record.scope == "trigger":
            self.trigger_links[record.context_id] = (
                payload.get("merchant_id"), payload.get("customer_id")
            )

    def get(self, scope: str, cid: str) -> ContextRecord | None:
        with self._guard:
            record = self._records.get((scope, cid))
            return copy.deepcopy(record) if record is not None else None

    def snapshot(self, scope: str | None = None) -> list[ContextRecord]:
        with self._guard:
            records = [record for (record_scope, _), record in self._records.items()
                       if scope is None or record_scope == scope]
            return copy.deepcopy(records)

    def counts(self) -> dict[str, int]:
        with self._guard:
            counts = {name: 0 for name in VALID_SCOPES}
            for scope, _ in self._records:
                counts[scope] += 1
            return counts

    def journal_path(self) -> Path:
        return self._journal

    def wipe(self) -> None:
        with self._journal_guard, self._guard:
            self._records.clear()
            self.merchant_category.clear()
            self.merchant_customers.clear()
            self.trigger_links.clear()
            self.category_merchants.clear()
            self._key_locks.clear()
            self._journal.parent.mkdir(parents=True, exist_ok=True)
            self._journal.write_text("", encoding="utf-8")
