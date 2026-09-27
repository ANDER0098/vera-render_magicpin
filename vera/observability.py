"""Low-overhead structured logs and counters for request-level decisions."""

from __future__ import annotations

import json
import logging
import threading
from collections import Counter
from typing import Any

log = logging.getLogger("vera")


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: Counter[str] = Counter()

    def inc(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counts[name] += amount

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)

    def event(self, name: str, **fields: Any) -> None:
        self.inc(name)
        log.info(json.dumps({"event": name, **fields}, ensure_ascii=False, default=str))


metrics = Metrics()
