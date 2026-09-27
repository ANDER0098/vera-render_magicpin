"""Evaluation sets for Vera AI Challenge.

Exposes helpers to load E1 (canonical), E2 (full), E3 (adversarial), and E4 (replays).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

_EVAL_DIR = Path(__file__).resolve().parent


def load_e1() -> List[Dict[str, Any]]:
    with open(_EVAL_DIR / "e1_canonical.json", "r", encoding="utf-8") as f:
        return json.load(f)


def load_e2() -> List[Dict[str, Any]]:
    with open(_EVAL_DIR / "e2_full.json", "r", encoding="utf-8") as f:
        return json.load(f)


def load_e3() -> List[Dict[str, Any]]:
    with open(_EVAL_DIR / "e3_adversarial.json", "r", encoding="utf-8") as f:
        return json.load(f)


def load_e4() -> List[Dict[str, Any]]:
    with open(_EVAL_DIR / "e4_replays.json", "r", encoding="utf-8") as f:
        return json.load(f)
