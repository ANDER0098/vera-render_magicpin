"""Build evaluation sets for the Magicpin Vera challenge.

Generates:
  - harness/eval_sets/e1_canonical.json (30 canonical pairs tagged with metadata)
  - harness/eval_sets/e2_full.json (all 100 triggers joined to merchant, category, customer)
  - harness/eval_sets/e3_adversarial.json (minimum 40 adversarial synthetic test cases)
  - harness/eval_sets/e4_replays.json (8 conversation replay scenarios with action/token expectations)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional


def build_sets(base_dir: Optional[Path] = None, out_dir: Optional[Path] = None) -> Dict[str, Any]:
    root = Path(__file__).resolve().parent.parent
    if base_dir is None:
        base_dir = root / "expanded"
    if out_dir is None:
        out_dir = root / "harness" / "eval_sets"

    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. Load Categories
    categories: Dict[str, Dict[str, Any]] = {}
    for cat_file in sorted((base_dir / "categories").glob("*.json")):
        with open(cat_file, "r", encoding="utf-8") as f:
            cat = json.load(f)
            slug = cat.get("slug", cat_file.stem)
            categories[slug] = cat

    # 2. Load Merchants
    merchants: Dict[str, Dict[str, Any]] = {}
    for m_file in sorted((base_dir / "merchants").glob("*.json")):
        with open(m_file, "r", encoding="utf-8") as f:
            m = json.load(f)
            merchants[m["merchant_id"]] = m

    # 3. Load Customers
    customers: Dict[str, Dict[str, Any]] = {}
    if (base_dir / "customers").exists():
        for c_file in sorted((base_dir / "customers").glob("*.json")):
            with open(c_file, "r", encoding="utf-8") as f:
                c = json.load(f)
                customers[c["customer_id"]] = c

    # 4. Load Triggers
    triggers: Dict[str, Dict[str, Any]] = {}
    for t_file in sorted((base_dir / "triggers").glob("*.json")):
        with open(t_file, "r", encoding="utf-8") as f:
            t = json.load(f)
            triggers[t["id"]] = t

    def is_thin_merchant(m: Optional[Dict[str, Any]]) -> bool:
        if not m:
            return False
        return (
            not m.get("offers")
            and not m.get("signals")
            and not m.get("conversation_history")
            and not m.get("review_themes")
        )

    # =========================================================================
    # E1: Canonical (30 pairs)
    # =========================================================================
    with open(base_dir / "test_pairs.json", "r", encoding="utf-8") as f:
        pairs = json.load(f)["pairs"]

    e1: List[Dict[str, Any]] = []
    for p in pairs:
        tid = p["trigger_id"]
        mid = p["merchant_id"]
        cid = p.get("customer_id")

        t = triggers.get(tid, {})
        m = merchants.get(mid, {})

        payload = t.get("payload", {})
        is_placeholder = payload.get("placeholder") is True
        thin = is_thin_merchant(m)

        e1_item = {
            "test_id": p["test_id"],
            "trigger_id": tid,
            "merchant_id": mid,
            "customer_id": cid,
            "placeholder_payload": is_placeholder,
            "thin_merchant": thin,
            "category": m.get("category_slug", payload.get("category")),
            "kind": t.get("kind"),
            "trigger_kind": t.get("kind"),
            "scope": t.get("scope"),
            "urgency": t.get("urgency"),
            "has_customer": bool(cid),
        }
        e1.append(e1_item)

    e1.sort(key=lambda x: x["test_id"])
    e1_path = out_dir / "e1_canonical.json"
    with open(e1_path, "w", encoding="utf-8") as f:
        json.dump(e1, f, indent=2, sort_keys=True)

    # =========================================================================
    # E2: Full (100 triggers)
    # =========================================================================
    e2: List[Dict[str, Any]] = []
    for tid in sorted(triggers.keys()):
        t = triggers[tid]
        mid = t["merchant_id"]
        m = merchants.get(mid)
        cid = t.get("customer_id")
        c = customers.get(cid) if cid else None
        cat_slug = m.get("category_slug") if m else t.get("payload", {}).get("category")
        cat = categories.get(cat_slug) if cat_slug else None

        is_placeholder = t.get("payload", {}).get("placeholder") is True
        thin = is_thin_merchant(m)

        e2_item = {
            "trigger": t,
            "merchant": m,
            "category": cat,
            "category_slug": cat_slug,
            "customer": c,
            "test_id": tid,
            "trigger_id": tid,
            "merchant_id": mid,
            "customer_id": cid,
            "kind": t.get("kind"),
            "trigger_kind": t.get("kind"),
            "scope": t.get("scope"),
            "urgency": t.get("urgency"),
            "placeholder_payload": is_placeholder,
            "thin_merchant": thin,
            "has_customer": bool(cid),
        }
        e2.append(e2_item)

    e2_path = out_dir / "e2_full.json"
    with open(e2_path, "w", encoding="utf-8") as f:
        json.dump(e2, f, indent=2, sort_keys=True)

    # =========================================================================
    # E3: Adversarial Synthetic (>= 40 cases)
    # =========================================================================
    e3: List[Dict[str, Any]] = []

    def make_push(scope: str, cid: str, version: int, payload: Dict[str, Any], delivered_at: str = "2026-05-01T10:00:00Z") -> Dict[str, Any]:
        return {
            "scope": scope,
            "id": cid,
            "context_id": cid,
            "version": version,
            "payload": payload,
            "delivered_at": delivered_at,
        }

    def add_case(rule: str, desc: str, pushes: List[Dict[str, Any]], tick: Dict[str, Any], assertions: List[Dict[str, Any]]):
        case_id = f"E3_{len(e3) + 1:03d}"
        e3.append({
            "case_id": case_id,
            "rule": rule,
            "desc": desc,
            "pushes": pushes,
            "tick": tick,
            "assertions": assertions,
        })

    # Thin merchants and valid placeholder triggers (merchant-scoped or consented)
    thin_m_set = {mid for mid, m in merchants.items() if is_thin_merchant(m)}
    valid_thin_placeholders = []
    for tid, t in sorted(triggers.items()):
        if t.get("payload", {}).get("placeholder") is True and t.get("merchant_id") in thin_m_set:
            if t.get("scope") == "merchant":
                valid_thin_placeholders.append(t)
            elif t.get("scope") == "customer":
                cid = t.get("customer_id")
                cust = customers.get(cid) if cid else None
                if cust:
                    consent = (cust.get("consent") or {}).get("scope") or []
                    # ensure intent matches consent
                    if "promotional_offers" in consent or "recall_reminders" in consent:
                        valid_thin_placeholders.append(t)

    thin_merchants_list = [merchants[mid] for mid in sorted(thin_m_set)]

    # Rule 1: Thin merchant + placeholder trigger (16 variants across categories)
    for i in range(16):
        t = valid_thin_placeholders[i % len(valid_thin_placeholders)]
        mid = t["merchant_id"]
        m = merchants[mid]
        cat_slug = m["category_slug"]
        cat = categories[cat_slug]

        pushes = [
            make_push("category", cat_slug, 1, cat),
            make_push("merchant", mid, 1, m),
        ]
        cid = t.get("customer_id")
        if cid and cid in customers:
            pushes.append(make_push("customer", cid, 1, customers[cid]))
        pushes.append(make_push("trigger", t["id"], 1, t))

        add_case(
            rule="1",
            desc=f"thin merchant {mid} with placeholder trigger {t['id']}",
            pushes=pushes,
            tick={"now": "2026-05-01T10:00:00Z", "available_triggers": [t["id"]]},
            assertions=[
                {"type": "status_200"},
                {"type": "action_present"},
                {"type": "valid_11_fields"},
                {"type": "no_urls"},
            ],
        )

    # Rule 2: Customer-scoped trigger where customer context is NEVER pushed (trap T6, 2 variants)
    customer_triggers = [t for t in triggers.values() if t.get("scope") == "customer" and t.get("customer_id")]
    for i, ct in enumerate(customer_triggers[:2]):
        m = merchants[ct["merchant_id"]]
        cat_slug = m["category_slug"]
        pushes = [
            make_push("category", cat_slug, 1, categories[cat_slug]),
            make_push("merchant", m["merchant_id"], 1, m),
            make_push("trigger", ct["id"], 1, ct),
            # Customer intentionally omitted!
        ]
        add_case(
            rule="2",
            desc=f"customer-scoped trigger {ct['id']} without pushed customer context (trap T6)",
            pushes=pushes,
            tick={"now": "2026-05-01T10:00:00Z", "available_triggers": [ct["id"]]},
            assertions=[
                {"type": "status_200"},
                {"type": "no_crash"},
            ],
        )

    # Rule 3: Every trigger already expired relative to `now` (trap T1, 2 variants)
    for i in range(2):
        base_t = valid_thin_placeholders[i]
        t = dict(base_t)
        t["id"] = f"trg_expired_{i:02d}_{base_t['id']}"
        t["expires_at"] = "2026-04-01T00:00:00Z"
        m = merchants[t["merchant_id"]]
        cat_slug = m["category_slug"]
        pushes = [
            make_push("category", cat_slug, 1, categories[cat_slug]),
            make_push("merchant", m["merchant_id"], 1, m),
            make_push("trigger", t["id"], 1, t),
        ]
        add_case(
            rule="3",
            desc=f"trigger {t['id']} expired relative to now (trap T1)",
            pushes=pushes,
            tick={"now": "2027-01-01T10:00:00Z", "available_triggers": [t["id"]]},
            assertions=[
                {"type": "status_200"},
                {"type": "action_present"},
            ],
        )

    # Rule 4: available_triggers naming a trigger id that was never pushed (2 variants)
    for i in range(2):
        m = thin_merchants_list[i]
        cat_slug = m["category_slug"]
        ghost_tid = f"trg_ghost_never_pushed_{i:02d}"
        pushes = [
            make_push("category", cat_slug, 1, categories[cat_slug]),
            make_push("merchant", m["merchant_id"], 1, m),
        ]
        add_case(
            rule="4",
            desc=f"tick available_triggers references unpushed trigger {ghost_tid}",
            pushes=pushes,
            tick={"now": "2026-05-01T10:00:00Z", "available_triggers": [ghost_tid]},
            assertions=[
                {"type": "status_200"},
                {"type": "no_actions_or_empty"},
            ],
        )

    # Rule 5: a merchant_id that was never pushed (2 variants)
    for i in range(2):
        ghost_mid = f"m_ghost_unpushed_{i:02d}"
        t_ghost = {
            "id": f"trg_ghost_m_{i:02d}",
            "scope": "merchant",
            "kind": "curious_ask_due",
            "merchant_id": ghost_mid,
            "payload": {"placeholder": True, "metric_or_topic": "curious_ask_due"},
            "urgency": 2,
        }
        pushes = [
            make_push("category", "dentists", 1, categories["dentists"]),
            # Merchant is intentionally NEVER pushed!
        ]
        add_case(
            rule="5",
            desc=f"tick references trigger with unpushed merchant {ghost_mid}",
            pushes=pushes,
            tick={"now": "2026-05-01T10:00:00Z", "available_triggers": [t_ghost["id"]]},
            assertions=[
                {"type": "status_200"},
                {"type": "no_actions_or_empty"},
            ],
        )

    # Rule 6: a trigger kind that does not exist anywhere in the dataset (GENERIC_FRAME path, 2 variants)
    for i in range(2):
        m = thin_merchants_list[i + 2]
        cat_slug = m["category_slug"]
        novel_kind = f"novel_custom_signal_kind_{i:02d}"
        t_novel = {
            "id": f"trg_novel_kind_{i:02d}",
            "scope": "merchant",
            "kind": novel_kind,
            "merchant_id": m["merchant_id"],
            "payload": {"details": "Unseen signal format"},
            "urgency": 3,
        }
        pushes = [
            make_push("category", cat_slug, 1, categories[cat_slug]),
            make_push("merchant", m["merchant_id"], 1, m),
            make_push("trigger", t_novel["id"], 1, t_novel),
        ]
        add_case(
            rule="6",
            desc=f"trigger kind {novel_kind} routes through generic frame",
            pushes=pushes,
            tick={"now": "2026-05-01T10:00:00Z", "available_triggers": [t_novel["id"]]},
            assertions=[
                {"type": "status_200"},
                {"type": "action_present"},
            ],
        )

    # Rule 7: a v2 category push adding 5 brand-new digest items, then tick citing NEW item (2 variants)
    for i in range(2):
        cat_slug = "dentists" if i == 0 else "pharmacies"
        cat_v1 = categories[cat_slug]
        cat_v2 = dict(cat_v1)
        new_items = [
            {
                "id": f"d_new_study_{i}_{j}",
                "kind": "research",
                "title": f"New Clinical Finding Batch {i}-{j}",
                "source": f"JIDA Nov 2026, p.{10 + j}",
                "summary": f"Novel clinical recommendation {j}",
            }
            for j in range(5)
        ]
        cat_v2["digest"] = list(cat_v1.get("digest", [])) + new_items
        m = next(m for m in merchants.values() if m["category_slug"] == cat_slug)
        t_digest = {
            "id": f"trg_new_digest_cite_{i:02d}",
            "scope": "merchant",
            "kind": "research_digest",
            "merchant_id": m["merchant_id"],
            "payload": {"category": cat_slug, "top_item_id": new_items[0]["id"]},
            "urgency": 3,
        }
        pushes = [
            make_push("category", cat_slug, 1, cat_v1),
            make_push("merchant", m["merchant_id"], 1, m),
            make_push("category", cat_slug, 2, cat_v2, delivered_at="2026-05-01T10:01:00Z"),
            make_push("trigger", t_digest["id"], 1, t_digest, delivered_at="2026-05-01T10:02:00Z"),
        ]
        add_case(
            rule="7",
            desc=f"v2 category push with 5 new digest items, citing {new_items[0]['id']}",
            pushes=pushes,
            tick={"now": "2026-05-01T10:03:00Z", "available_triggers": [t_digest["id"]]},
            assertions=[
                {"type": "status_200"},
                {"type": "action_present"},
                {"type": "cite_new_item", "item_id": new_items[0]["id"]},
            ],
        )

    # Rule 8: a v2 merchant push shifting performance numbers, then tick using NEW number (2 variants)
    for i in range(2):
        m = dict(thin_merchants_list[i + 4])
        cat_slug = m["category_slug"]
        m_v1 = dict(m)
        m_v1["performance"] = dict(m.get("performance", {}))
        m_v1["performance"]["views"] = 120

        m_v2 = dict(m)
        m_v2["performance"] = dict(m.get("performance", {}))
        m_v2["performance"]["views"] = 8888

        t_perf = {
            "id": f"trg_perf_v2_{i:02d}",
            "scope": "merchant",
            "kind": "perf_dip",
            "merchant_id": m["merchant_id"],
            "payload": {"placeholder": True, "metric_or_topic": "perf_dip"},
            "urgency": 3,
        }
        pushes = [
            make_push("category", cat_slug, 1, categories[cat_slug]),
            make_push("merchant", m["merchant_id"], 1, m_v1),
            make_push("merchant", m["merchant_id"], 2, m_v2, delivered_at="2026-05-01T10:01:00Z"),
            make_push("trigger", t_perf["id"], 1, t_perf, delivered_at="2026-05-01T10:02:00Z"),
        ]
        add_case(
            rule="8",
            desc=f"v2 merchant push shifts views to 8888; verify new number is used",
            pushes=pushes,
            tick={"now": "2026-05-01T10:03:00Z", "available_triggers": [t_perf["id"]]},
            assertions=[
                {"type": "status_200"},
                {"type": "action_present"},
            ],
        )

    # Rule 9: brand-new customer scope pushed mid-window with recall_due trigger 2 minutes later (2 variants)
    for i in range(2):
        m = thin_merchants_list[i + 6]
        cat_slug = m["category_slug"]
        c_mid = {
            "customer_id": f"c_midwindow_{i:02d}",
            "merchant_id": m["merchant_id"],
            "identity": {"first_name": "Aarav", "language_pref": "en"},
            "consent": {"scope": ["recall_reminders"]},
        }
        t_recall = {
            "id": f"trg_midwindow_recall_{i:02d}",
            "scope": "customer",
            "kind": "recall_due",
            "merchant_id": m["merchant_id"],
            "customer_id": c_mid["customer_id"],
            "payload": {"available_slots": ["Tomorrow 10 AM", "Tomorrow 2 PM"]},
            "urgency": 4,
            "expires_at": "2026-05-01T10:10:00Z",
        }
        pushes = [
            make_push("category", cat_slug, 1, categories[cat_slug], delivered_at="2026-05-01T10:00:00Z"),
            make_push("merchant", m["merchant_id"], 1, m, delivered_at="2026-05-01T10:00:00Z"),
            make_push("customer", c_mid["customer_id"], 1, c_mid, delivered_at="2026-05-01T10:01:00Z"),
            make_push("trigger", t_recall["id"], 1, t_recall, delivered_at="2026-05-01T10:03:00Z"),
        ]
        add_case(
            rule="9",
            desc=f"customer scope {c_mid['customer_id']} pushed mid-window with recall_due trigger 2 min later",
            pushes=pushes,
            tick={"now": "2026-05-01T10:03:30Z", "available_triggers": [t_recall["id"]]},
            assertions=[
                {"type": "status_200"},
                {"type": "action_present"},
            ],
        )

    # Rule 10: payload containing unseen snake_case field (must not leak into copy, 2 variants)
    for i in range(2):
        m = thin_merchants_list[i + 8]
        cat_slug = m["category_slug"]
        leak_token = f"unseen_machine_code_xyz_{i:02d}"
        t_snake = {
            "id": f"trg_snake_field_{i:02d}",
            "scope": "merchant",
            "kind": "curious_ask_due",
            "merchant_id": m["merchant_id"],
            "payload": {"new_field": leak_token, "metric_or_topic": "curious_ask_due"},
            "urgency": 2,
        }
        pushes = [
            make_push("category", cat_slug, 1, categories[cat_slug]),
            make_push("merchant", m["merchant_id"], 1, m),
            make_push("trigger", t_snake["id"], 1, t_snake),
        ]
        add_case(
            rule="10",
            desc=f"payload containing snake_case enum {leak_token} must not leak into body",
            pushes=pushes,
            tick={"now": "2026-05-01T10:00:00Z", "available_triggers": [t_snake["id"]]},
            assertions=[
                {"type": "status_200"},
                {"type": "action_present"},
                {"type": "no_snake_case_leak", "forbidden": leak_token},
            ],
        )

    # Rule 11: trigger payload referencing non-existent digest item id (2 variants)
    for i in range(2):
        m = thin_merchants_list[i + 10]
        cat_slug = m["category_slug"]
        t_missing_digest = {
            "id": f"trg_missing_digest_{i:02d}",
            "scope": "merchant",
            "kind": "research_digest",
            "merchant_id": m["merchant_id"],
            "payload": {"category": cat_slug, "top_item_id": f"d_nonexistent_item_999_{i}"},
            "urgency": 2,
        }
        pushes = [
            make_push("category", cat_slug, 1, categories[cat_slug]),
            make_push("merchant", m["merchant_id"], 1, m),
            make_push("trigger", t_missing_digest["id"], 1, t_missing_digest),
        ]
        add_case(
            rule="11",
            desc="trigger payload references nonexistent digest item id; must handle without hallucination",
            pushes=pushes,
            tick={"now": "2026-05-01T10:00:00Z", "available_triggers": [t_missing_digest["id"]]},
            assertions=[
                {"type": "status_200"},
                {"type": "action_present"},
            ],
        )

    # Rule 12: two triggers for the SAME merchant in one tick (only one action emitted, 2 variants)
    for i in range(2):
        m = thin_merchants_list[i + 12]
        cat_slug = m["category_slug"]
        t1 = {
            "id": f"trg_same_m_1_{i:02d}",
            "scope": "merchant",
            "kind": "curious_ask_due",
            "merchant_id": m["merchant_id"],
            "payload": {"placeholder": True, "metric_or_topic": "curious_ask_due"},
            "urgency": 2,
        }
        t2 = {
            "id": f"trg_same_m_2_{i:02d}",
            "scope": "merchant",
            "kind": "perf_dip",
            "merchant_id": m["merchant_id"],
            "payload": {"placeholder": True, "metric_or_topic": "perf_dip"},
            "urgency": 4,
        }
        pushes = [
            make_push("category", cat_slug, 1, categories[cat_slug]),
            make_push("merchant", m["merchant_id"], 1, m),
            make_push("trigger", t1["id"], 1, t1),
            make_push("trigger", t2["id"], 1, t2),
        ]
        add_case(
            rule="12",
            desc=f"two triggers for same merchant {m['merchant_id']} in one tick; exactly one action emitted",
            pushes=pushes,
            tick={"now": "2026-05-01T10:00:00Z", "available_triggers": [t1["id"], t2["id"]]},
            assertions=[
                {"type": "status_200"},
                {"type": "one_action_only"},
                {"type": "max_one_per_merchant"},
            ],
        )

    # Rule 13: 25 triggers in one tick (respect 20-action cap, 2 variants)
    for i in range(2):
        merchants_25 = list(merchants.values())[i * 25: (i + 1) * 25]
        if len(merchants_25) < 25:
            merchants_25 = list(merchants.values())[:25]
        pushes_25 = []
        tids_25 = []
        cats_pushed = set()
        for idx, m_25 in enumerate(merchants_25):
            cslug = m_25["category_slug"]
            if cslug not in cats_pushed:
                pushes_25.append(make_push("category", cslug, 1, categories[cslug]))
                cats_pushed.add(cslug)
            pushes_25.append(make_push("merchant", m_25["merchant_id"], 1, m_25))
            tid_25 = f"trg_bulk_25_{i}_{idx:02d}"
            t_item = {
                "id": tid_25,
                "scope": "merchant",
                "kind": "curious_ask_due",
                "merchant_id": m_25["merchant_id"],
                "payload": {"placeholder": True, "metric_or_topic": "curious_ask_due"},
                "urgency": (idx % 5) + 1,
            }
            pushes_25.append(make_push("trigger", tid_25, 1, t_item))
            tids_25.append(tid_25)

        add_case(
            rule="13",
            desc="25 triggers in one tick; must respect 20 action cap",
            pushes=pushes_25,
            tick={"now": "2026-05-01T10:00:00Z", "available_triggers": tids_25},
            assertions=[
                {"type": "status_200"},
                {"type": "respect_20_cap"},
                {"type": "max_actions", "cap": 20},
            ],
        )

    # Rule 14: merchant whose languages are ["en"] only (pure English copy, 2 variants)
    for i in range(2):
        base_m = thin_merchants_list[i + 14]
        m_en = dict(base_m)
        m_en["merchant_id"] = f"m_en_only_{i:02d}"
        m_en["identity"] = dict(base_m.get("identity", {}))
        m_en["identity"]["languages"] = ["en"]
        cat_slug = m_en["category_slug"]
        t_en = {
            "id": f"trg_en_only_{i:02d}",
            "scope": "merchant",
            "kind": "curious_ask_due",
            "merchant_id": m_en["merchant_id"],
            "payload": {"placeholder": True, "metric_or_topic": "curious_ask_due"},
            "urgency": 2,
        }
        pushes = [
            make_push("category", cat_slug, 1, categories[cat_slug]),
            make_push("merchant", m_en["merchant_id"], 1, m_en),
            make_push("trigger", t_en["id"], 1, t_en),
        ]
        add_case(
            rule="14",
            desc=f"merchant {m_en['merchant_id']} with en-only languages must receive pure English copy",
            pushes=pushes,
            tick={"now": "2026-05-01T10:00:00Z", "available_triggers": [t_en["id"]]},
            assertions=[
                {"type": "status_200"},
                {"type": "action_present"},
                {"type": "pure_english"},
            ],
        )

    # Rule 15: customer with language_pref "te-en mix" / "kn-en mix" / "ta-en mix" (3 variants)
    for i, mix_pref in enumerate(["te-en mix", "kn-en mix", "ta-en mix"]):
        m = thin_merchants_list[i]
        cat_slug = m["category_slug"]
        c_mix = {
            "customer_id": f"c_mix_{i:02d}",
            "merchant_id": m["merchant_id"],
            "identity": {"first_name": "Pooja", "language_pref": mix_pref},
            "consent": {"scope": ["recall_reminders"]},
        }
        t_mix = {
            "id": f"trg_mix_lang_{i:02d}",
            "scope": "customer",
            "kind": "recall_due",
            "merchant_id": m["merchant_id"],
            "customer_id": c_mix["customer_id"],
            "payload": {"placeholder": True, "metric_or_topic": "recall_due"},
            "urgency": 3,
        }
        pushes = [
            make_push("category", cat_slug, 1, categories[cat_slug]),
            make_push("merchant", m["merchant_id"], 1, m),
            make_push("customer", c_mix["customer_id"], 1, c_mix),
            make_push("trigger", t_mix["id"], 1, t_mix),
        ]
        add_case(
            rule="15",
            desc=f"customer {c_mix['customer_id']} with language_pref '{mix_pref}'",
            pushes=pushes,
            tick={"now": "2026-05-01T10:00:00Z", "available_triggers": [t_mix["id"]]},
            assertions=[
                {"type": "status_200"},
                {"type": "action_present"},
            ],
        )

    e3_path = out_dir / "e3_adversarial.json"
    with open(e3_path, "w", encoding="utf-8") as f:
        json.dump(e3, f, indent=2, sort_keys=True)

    # =========================================================================
    # E4: Conversation Replays (8 scenarios)
    e4 = [
        {
            "scenario_id": "e4_001_auto_reply_hell",
            "name": "auto-reply hell",
            "description": "identical canned text on FOUR DISTINCT conversation ids for one merchant (trap T2)",
            "merchant_id": "m_001_drmeera_dentist_delhi",
            "turns": [
                {
                    "conversation_id": "conv_auto_1",
                    "from_role": "merchant",
                    "message": "Thank you for contacting us! Our team will respond shortly.",
                    "turn_number": 2,
                    "expected_action": "send",
                    "description": "turn 1: canned text -> expect nudge",
                },
                {
                    "conversation_id": "conv_auto_2",
                    "from_role": "merchant",
                    "message": "Thank you for contacting us! Our team will respond shortly.",
                    "turn_number": 3,
                    "expected_action": "wait",
                    "min_wait_seconds": 86400,
                    "description": "turn 2: repeat across conversations -> expect wait >= 86400",
                },
                {
                    "conversation_id": "conv_auto_3",
                    "from_role": "merchant",
                    "message": "Thank you for contacting us! Our team will respond shortly.",
                    "turn_number": 4,
                    "expected_action": "end",
                    "description": "turn 3: end by turn 4",
                },
                {
                    "conversation_id": "conv_auto_4",
                    "from_role": "merchant",
                    "message": "Thank you for contacting us! Our team will respond shortly.",
                    "turn_number": 5,
                    "expected_action": "end",
                    "description": "turn 4: stay ended",
                },
            ],
        },
        {
            "scenario_id": "e4_002_intent_transition",
            "name": "intent transition",
            "description": "two qualifying turns then 'Ok lets do it. Whats next?' (trap T4)",
            "merchant_id": "m_002_bharat_dentist_mumbai",
            "turns": [
                {
                    "conversation_id": "conv_intent_1",
                    "from_role": "merchant",
                    "message": "Ok lets do it. Whats next?",
                    "turn_number": 2,
                    "expected_action": "send",
                    "require_tokens": ["done", "sending", "draft", "here", "confirm", "proceed", "next"],
                    "forbid_tokens": ["would you", "do you", "can you tell", "what if", "how about"],
                    "description": "action mode: must contain actioning token and NONE of qualifying bigrams",
                },
            ],
        },
        {
            "scenario_id": "e4_003_hostile_then_offtopic",
            "name": "hostile then off-topic",
            "description": "hostile refusal then off-topic GST filing request",
            "merchant_id": "m_003_studio11_salon_hyderabad",
            "turns": [
                {
                    "conversation_id": "conv_hostile_1",
                    "from_role": "merchant",
                    "message": "Stop messaging me. This is useless spam.",
                    "turn_number": 2,
                    "expected_action": "end",
                    "allowed_actions": ["end", "send"],
                    "require_tokens_if_send": ["sorry", "apolog", "won't"],
                    "description": "turn 1: hostile -> end or apology",
                },
                {
                    "conversation_id": "conv_hostile_2",
                    "from_role": "merchant",
                    "message": "Btw can you also help me with my GST filing?",
                    "turn_number": 2,
                    "expected_action": "send",
                    "allowed_actions": ["send", "end"],
                    "description": "turn 2: off-topic turn must be politely declined and redirected",
                },
            ],
        },
        {
            "scenario_id": "e4_004_curveball",
            "name": "curveball we cannot answer",
            "description": "what's my exact Google ranking for 'dentist in Delhi'?",
            "merchant_id": "m_001_drmeera_dentist_delhi",
            "turns": [
                {
                    "conversation_id": "conv_curveball_1",
                    "from_role": "merchant",
                    "message": "what's my exact Google ranking for 'dentist in Delhi'?",
                    "turn_number": 2,
                    "expected_action": "send",
                    "forbid_tokens": ["rank 1", "rank 2", "#1", "#2"],
                    "description": "must not fabricate a number",
                },
            ],
        },
        {
            "scenario_id": "e4_005_silence",
            "name": "silence / non-answer",
            "description": "'ok' then '.' then '???' -> must not repeat verbatim, must end after 3 unanswered nudges",
            "merchant_id": "m_005_pizzajunction_restaurant_delhi",
            "turns": [
                {
                    "conversation_id": "conv_silence_1",
                    "from_role": "merchant",
                    "message": "ok",
                    "turn_number": 2,
                    "expected_action": "send",
                    "allowed_actions": ["send", "wait"],
                },
                {
                    "conversation_id": "conv_silence_1",
                    "from_role": "merchant",
                    "message": ".",
                    "turn_number": 3,
                    "expected_action": "wait",
                    "allowed_actions": ["wait", "send", "end"],
                },
                {
                    "conversation_id": "conv_silence_1",
                    "from_role": "merchant",
                    "message": "???",
                    "turn_number": 4,
                    "expected_action": "end",
                    "allowed_actions": ["end", "wait"],
                },
            ],
        },
        {
            "scenario_id": "e4_006_lang_switch",
            "name": "mid-conversation language switch",
            "description": "English turn then Hindi romanised turn -> reply switches to hi_en",
            "merchant_id": "m_006_southindiancafe_restaurant_bangalore",
            "turns": [
                {
                    "conversation_id": "conv_switch_1",
                    "from_role": "merchant",
                    "message": "Hello, can you explain the offer in detail?",
                    "turn_number": 2,
                    "expected_action": "send",
                },
                {
                    "conversation_id": "conv_switch_1",
                    "from_role": "merchant",
                    "message": "theek hai bhai, mujhe thoda detail me samjhao kya karna hoga",
                    "turn_number": 3,
                    "expected_action": "send",
                    "description": "reply must switch to hi_en code mix",
                },
            ],
        },
        {
            "scenario_id": "e4_007_commitment_reversal",
            "name": "commitment then reversal",
            "description": "'yes do it' then 'actually no, cancel' -> must honour the cancel",
            "merchant_id": "m_007_powerhouse_gym_bangalore",
            "turns": [
                {
                    "conversation_id": "conv_rev_1",
                    "from_role": "merchant",
                    "message": "yes do it",
                    "turn_number": 2,
                    "expected_action": "send",
                },
                {
                    "conversation_id": "conv_rev_1",
                    "from_role": "merchant",
                    "message": "actually no, cancel",
                    "turn_number": 3,
                    "expected_action": "send",
                    "allowed_actions": ["send", "wait", "end"],
                    "description": "must honour the cancellation",
                },
            ],
        },
        {
            "scenario_id": "e4_008_hinglish_auto_reply",
            "name": "auto-reply in Hinglish (Pattern B)",
            "description": "Pattern B text from challenge-brief.md section 9",
            "merchant_id": "m_004_glamour_salon_pune",
            "turns": [
                {
                    "conversation_id": "conv_pattern_b_1",
                    "from_role": "merchant",
                    "message": "Aapki jaankari ke liye bahut-bahut shukriya. Main aapki yeh sabhi baatein aur sujhaav hamari team tak pahuncha deti hoon.",
                    "turn_number": 2,
                    "expected_action": "send",
                    "allowed_actions": ["send", "wait"],
                },
                {
                    "conversation_id": "conv_pattern_b_1",
                    "from_role": "merchant",
                    "message": "Aapki madad ke liye shukriya, lekin main ek automated assistant hoon...",
                    "turn_number": 3,
                    "expected_action": "end",
                    "allowed_actions": ["end", "wait"],
                    "description": "detects automated assistant in Hinglish and exits gracefully",
                },
            ],
        },
    ]

    e4_path = out_dir / "e4_replays.json"
    with open(e4_path, "w", encoding="utf-8") as f:
        json.dump(e4, f, indent=2, sort_keys=True)

    return {
        "e1_count": len(e1),
        "e2_count": len(e2),
        "e3_count": len(e3),
        "e4_count": len(e4),
    }


if __name__ == "__main__":
    res = build_sets()
    print("Build eval sets completed:", res)
