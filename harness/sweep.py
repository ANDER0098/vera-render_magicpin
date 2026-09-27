"""Golden corpus sweep: all 100 triggers x 2 `now` values -> zero hard violations.

Runs derive_facts -> resolve_voice -> compose_fallback -> validate for every
trigger in expanded/, writing harness/out/fallback_sweep.jsonl (one line per
trigger) so a human can eyeball copy quality.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.fallbacks.compose import compose_fallback  # noqa: E402
from vera.grounding.validator import has_hard, validate  # noqa: E402
from vera.signals.derived import derive_facts, is_placeholder_trigger  # noqa: E402
from vera.voice import resolve_voice  # noqa: E402
from harness.case_corpus import load_case_ngrams  # noqa: E402


def load_jsonl_dir(directory: Path) -> dict[str, dict]:
    items = {}
    for path in directory.glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        key = "merchant_id" if "merchants" in directory.name else \
            "customer_id" if "customers" in directory.name else "id"
        items[data[key]] = data
    return items


def run_sweep(now: datetime, out_path: Path | None = None) -> dict:
    categories = {p.stem: json.loads(p.read_text(encoding="utf-8"))
                  for p in (ROOT / "expanded" / "categories").glob("*.json")}
    merchants = load_jsonl_dir(ROOT / "expanded" / "merchants")
    customers = load_jsonl_dir(ROOT / "expanded" / "customers")
    triggers = load_jsonl_dir(ROOT / "expanded" / "triggers")
    corpus = load_case_ngrams(8)

    lines: list[str] = []
    hard_count = 0
    soft_histogram: Counter[str] = Counter()
    bodies_per_merchant: dict[str, list[str]] = {}
    rendered = 0

    for trigger in sorted(triggers.values(), key=lambda t: t["id"]):
        merchant = merchants.get(trigger.get("merchant_id"))
        if merchant is None:
            continue
        category = categories.get(str(merchant.get("category_slug")))
        customer = customers.get(trigger.get("customer_id")) if trigger.get("customer_id") else None

        facts = derive_facts(category, merchant, trigger, customer, now)
        voice = resolve_voice(category, merchant, customer, trigger)
        message = compose_fallback(
            trigger["kind"], category=category, merchant=merchant, trigger=trigger,
            customer=customer, facts=facts, voice=voice, now=now,
            prior_bodies=bodies_per_merchant.get(merchant["merchant_id"], []),
        )
        violations = validate(
            message.body, message.cta, message.send_as, facts=facts, voice=voice,
            category=category, merchant=merchant, customer=customer,
            prior_bodies=bodies_per_merchant.get(merchant["merchant_id"], []),
            case_corpus_ngrams=corpus, scope=trigger.get("scope"), trigger=trigger,
        )
        rendered += 1
        if has_hard(violations):
            hard_count += 1
        for v in violations:
            if v.severity == "soft":
                soft_histogram[v.code] += 1
        bodies_per_merchant.setdefault(merchant["merchant_id"], []).append(message.body)

        expected_send_as = "merchant_on_behalf" if trigger.get("scope") == "customer" else "vera"
        assert message.send_as == expected_send_as, (
            f"{trigger['id']}: send_as {message.send_as} != {expected_send_as}")
        assert message.body.strip(), f"{trigger['id']}: empty body"

        lines.append(json.dumps({
            "now": now.isoformat(),
            "kind": trigger["kind"],
            "trigger_id": trigger["id"],
            "merchant_id": merchant["merchant_id"],
            "customer_id": trigger.get("customer_id"),
            "lang": voice.lang,
            "body": message.body,
            "cta": message.cta,
            "send_as": message.send_as,
            "template_name": message.template_name,
            "violations": [f"{v.severity}:{v.code}" for v in violations],
            "rationale": message.rationale,
        }, ensure_ascii=False))

    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return {
        "rendered": rendered,
        "hard_violations": hard_count,
        "soft_by_code": dict(soft_histogram),
        "duplicate_bodies": {
            mid: n for mid, bodies in bodies_per_merchant.items()
            for n in [len(bodies) - len(set(bodies))] if n > 0
        },
    }


if __name__ == "__main__":
    # no wall-clock reads in harness/ — the alternate `now` is a fixed far
    # future date that proves expiry tolerance (trap T1) just as well
    fixed_now = datetime(2026, 4, 26, 10, 30, tzinfo=timezone.utc)
    alt_now = datetime(2027, 11, 15, 9, 0, tzinfo=timezone.utc).replace(tzinfo=None)
    out = ROOT / "harness" / "out" / "fallback_sweep.jsonl"
    result = run_sweep(fixed_now.replace(tzinfo=None), out)
    print("now=2026-04-26:", json.dumps(result, ensure_ascii=False))
    result_alt = run_sweep(alt_now, None)
    print("now=2027-11-15:", json.dumps(result_alt, ensure_ascii=False))
