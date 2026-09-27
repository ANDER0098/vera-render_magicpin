"""Shared fixtures for the grounding-layer test suite."""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXED_NOW = datetime(2026, 4, 26, 10, 30, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="session")
def root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def now() -> datetime:
    return FIXED_NOW


def load(path: str) -> dict:
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def rich_dentist() -> dict:
    return load("expanded/merchants/m_001_drmeera_dentist_delhi.json")


@pytest.fixture(scope="session")
def thin_salon() -> dict:
    return load("expanded/merchants/m_023_sushma_salon_pune.json")


@pytest.fixture(scope="session")
def categories() -> dict:
    return {
        slug: load(f"dataset/categories/{slug}.json")
        for slug in ("dentists", "salons", "restaurants", "gyms", "pharmacies")
    }


@pytest.fixture(scope="session")
def seed_triggers() -> list[dict]:
    return load("dataset/triggers_seed.json")["triggers"]


@pytest.fixture(scope="session")
def priya() -> dict:
    return load("expanded/customers/c_001_priya_for_m001.json")


@pytest.fixture(scope="session")
def sharma() -> dict:
    return load("expanded/customers/c_013_grandfather_for_m009.json")


@pytest.fixture(scope="session")
def case_ngrams() -> set:
    from harness.case_corpus import load_case_ngrams

    return load_case_ngrams(8)
