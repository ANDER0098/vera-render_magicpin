"""Unit tests for evaluation sets E1, E2, E3, E4."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from harness.eval_sets import load_e1, load_e2, load_e3, load_e4
from tools.build_eval_sets import build_sets


def test_counts() -> None:
    e1 = load_e1()
    e2 = load_e2()
    e3 = load_e3()
    e4 = load_e4()

    assert len(e1) == 30, f"Expected E1 to have 30 pairs, got {len(e1)}"
    assert len(e2) == 100, f"Expected E2 to have 100 triggers, got {len(e2)}"
    assert len(e3) >= 40, f"Expected E3 to have >= 40 cases, got {len(e3)}"
    assert len(e4) >= 8, f"Expected E4 to have >= 8 replay scenarios, got {len(e4)}"


def test_e1_placeholders() -> None:
    e1 = load_e1()
    placeholders = [p["test_id"] for p in e1 if p.get("placeholder_payload") is True]
    expected = [
        "T03", "T04", "T08", "T10", "T12", "T14", "T15",
        "T17", "T19", "T23", "T25", "T27", "T29"
    ]
    assert sorted(placeholders) == sorted(expected), f"Mismatch in placeholder test pairs: {placeholders}"
    assert len(placeholders) == 13


def test_determinism() -> None:
    """Building the evaluation sets twice yields byte-identical files."""
    with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
        dir1 = Path(d1)
        dir2 = Path(d2)

        build_sets(out_dir=dir1)
        build_sets(out_dir=dir2)

        for filename in ("e1_canonical.json", "e2_full.json", "e3_adversarial.json", "e4_replays.json"):
            b1 = (dir1 / filename).read_bytes()
            b2 = (dir2 / filename).read_bytes()
            assert b1 == b2, f"Determinism failure for {filename}: files differ across runs"


def test_e3_context_pushes() -> None:
    """Every E3 case's context pushes reference IDs that resolve inside the case itself."""
    e3 = load_e3()
    assert len(e3) >= 40

    for case in e3:
        rule = str(case.get("rule", ""))
        pushes = case.get("pushes", [])

        pushed_categories = set()
        pushed_merchants = set()
        pushed_customers = set()
        pushed_triggers = set()

        for p in pushes:
            assert "scope" in p and "id" in p and "payload" in p and "version" in p
            scope = p["scope"]
            cid = p["id"]
            if scope == "category":
                pushed_categories.add(cid)
            elif scope == "merchant":
                pushed_merchants.add(cid)
            elif scope == "customer":
                pushed_customers.add(cid)
            elif scope == "trigger":
                pushed_triggers.add(cid)

        # 1. Merchant references must resolve to pushed categories
        for p in pushes:
            if p["scope"] == "merchant":
                cat_slug = p["payload"].get("category_slug")
                if cat_slug:
                    assert cat_slug in pushed_categories, (
                        f"Case {case['case_id']}: Merchant {p['id']} references category '{cat_slug}' "
                        f"not in pushed categories {pushed_categories}"
                    )

        # 2. Customer references must resolve to pushed merchants
        for p in pushes:
            if p["scope"] == "customer":
                mid = p["payload"].get("merchant_id")
                if mid:
                    assert mid in pushed_merchants, (
                        f"Case {case['case_id']}: Customer {p['id']} references merchant '{mid}' "
                        f"not in pushed merchants {pushed_merchants}"
                    )

        # 3. Trigger references must resolve to pushed merchants and customers (unless intentionally testing missing)
        for p in pushes:
            if p["scope"] == "trigger":
                mid = p["payload"].get("merchant_id")
                if mid and rule != "5":
                    assert mid in pushed_merchants, (
                        f"Case {case['case_id']}: Trigger {p['id']} references merchant '{mid}' "
                        f"not in pushed merchants {pushed_merchants}"
                    )

                cid = p["payload"].get("customer_id")
                if cid and rule not in ("2", "5"):
                    assert cid in pushed_customers, (
                        f"Case {case['case_id']}: Trigger {p['id']} references customer '{cid}' "
                        f"not in pushed customers {pushed_customers}"
                    )

        # 4. Available triggers in tick must resolve to pushed triggers (unless intentionally testing missing)
        tick_triggers = case.get("tick", {}).get("available_triggers", [])
        if rule != "4":
            for tid in tick_triggers:
                # Rule 5 is testing unpushed merchant; the trigger might or might not be pushed
                if rule != "5":
                    assert tid in pushed_triggers, (
                        f"Case {case['case_id']}: Tick trigger '{tid}' was not pushed in this case"
                    )
