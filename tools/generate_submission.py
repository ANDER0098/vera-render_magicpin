#!/usr/bin/env python3
"""Generate submission.jsonl: one compose() per canonical (merchant, trigger) pair.

Reads expanded/test_pairs.json (T01-T30), materializes each pair from the
dataset JSON exactly like the runner does, composes through bot.compose()
(the production planner + composer path), and writes submission.jsonl.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import compose  # noqa: E402
from vera.signals.derived import consent_allows  # noqa: E402
from vera.planner import _trigger_intent  # noqa: E402


def _consent_blocked(trigger: dict, customer: dict | None) -> bool:
    """True when plan() would drop this pair solely for lack of customer consent."""
    if str(trigger.get("scope") or "merchant") != "customer" or customer is None:
        return False
    consent = (customer.get("consent") or {}).get("scope") or []
    intent = _trigger_intent(str(trigger.get("kind") or ""))
    return not consent_allows(consent, intent)


def main() -> int:
    pairs = json.loads((ROOT / "expanded" / "test_pairs.json").read_text(encoding="utf-8"))["pairs"]

    def load(path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    # Same materialization as harness.runner._load_expanded: one JSON per
    # entity under expanded/{categories,merchants,customers,triggers}.
    merchants: dict = {}
    for p in (ROOT / "expanded" / "merchants").glob("*.json"):
        m = load(p)
        merchants[m["merchant_id"]] = m
    triggers: dict = {}
    for p in (ROOT / "expanded" / "triggers").glob("*.json"):
        t = load(p)
        triggers[t["id"]] = t
    categories: dict = {}
    for p in (ROOT / "expanded" / "categories").glob("*.json"):
        c = load(p)
        categories[str(c.get("slug") or p.stem)] = c
    customers: dict = {}
    for p in (ROOT / "expanded" / "customers").glob("*.json"):
        c = load(p)
        customers[c["customer_id"]] = c

    lines: list[str] = []
    failures: list[str] = []
    for pair in pairs:
        tid = pair["test_id"]
        trigger = triggers.get(str(pair["trigger_id"]))
        merchant = merchants.get(str(pair["merchant_id"]))
        if trigger is None or merchant is None:
            failures.append(f"{tid}: trigger/merchant not found in dataset")
            continue
        customer = customers.get(str(pair["customer_id"])) if pair.get("customer_id") else None
        cat = categories.get(str(merchant.get("category_slug") or pair.get("category") or ""), {})
        t0 = time.perf_counter()
        try:
            result = compose(cat, merchant, trigger, customer)
        except ValueError as exc:
            # plan() suppresses pairs whose customer consent does not cover the
            # trigger intent. That is the correct, scoreable decision: emit an
            # explicit no-send line rather than a consent-violating message.
            if _consent_blocked(trigger, customer):
                consent = (customer.get("consent") or {}).get("scope") or []
                intent = _trigger_intent(str(trigger.get("kind") or ""))
                result = {
                    "body": "",
                    "cta": "none",
                    "send_as": "vera",
                    "suppression_key": str(trigger.get("suppression_key") or pair["trigger_id"]),
                    "rationale": (
                        f"Suppressed: customer consent scope {consent} does not cover "
                        f"intent '{intent}' for trigger kind '{trigger.get('kind')}'; "
                        "sending would violate consent."),
                }
                print(f"{tid}: SUPPRESSED (consent)  intent={intent} consent={consent}")
            else:
                failures.append(f"{tid}: compose failed: {type(exc).__name__}: {exc}")
                continue
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{tid}: compose failed: {type(exc).__name__}: {exc}")
            continue
        secs = time.perf_counter() - t0
        missing = {"body", "cta", "send_as", "suppression_key", "rationale"} - set(result)
        if missing or (not str(result.get("body", "")).strip()
                       and "Suppressed" not in str(result.get("rationale"))):
            failures.append(f"{tid}: invalid compose result (missing={sorted(missing)})")
            continue
        lines.append(json.dumps({"test_id": tid, **result}, ensure_ascii=False))
        if result["body"]:
            print(f"{tid}: OK  {len(result['body']):>3} ch  {secs:.2f}s  cta={result['cta']}")

    out = ROOT / "submission.jsonl"
    out.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    print(f"\nwrote {out.name}: {len(lines)}/30 lines")
    if failures:
        print("FAILURES:")
        for f in failures:
            print("  -", f)
        return 1
    if len(lines) != 30:
        print("FAIL: expected 30 lines")
        return 1
    print("PASS: 30/30 canonical pairs composed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
