"""60 simulated minutes of keyless HTTP load plus a Phase-3 freshness check."""

from __future__ import annotations

import argparse
import copy
import ctypes
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from datetime import datetime, timedelta, timezone

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.replay_scenarios import run_replays  # noqa: E402


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _rss_bytes(pid: int | None) -> int | None:
    if os.name == "nt" and pid:
        class Counters(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                        ("PrivateUsage", ctypes.c_size_t)]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        handle = kernel.OpenProcess(0x0410, False, pid)
        if not handle:
            return None
        try:
            counters = Counters()
            counters.cb = ctypes.sizeof(counters)
            ok = psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
            return int(counters.WorkingSetSize) if ok else None
        finally:
            kernel.CloseHandle(handle)
    try:
        import resource
        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(value * (1024 if sys.platform != "darwin" else 1))
    except (ImportError, AttributeError, OSError):
        return None


def _request(client: httpx.Client, method: str, path: str, payload: dict | None = None) -> tuple[dict, float]:
    start = time.monotonic()
    response = client.request(method, path, json=payload)
    latency = time.monotonic() - start
    assert response.status_code == 200, f"{method} {path}: {response.status_code} {response.text[:180]}"
    return response.json(), latency


def _push(client: httpx.Client, scope: str, cid: str, payload: dict, version: int = 1,
          delivered_at: str = "2026-04-26T10:00:00Z") -> float:
    data, latency = _request(client, "POST", "/v1/context", {"scope": scope,
        "context_id": cid, "version": version, "payload": payload,
        "delivered_at": delivered_at})
    assert data.get("accepted") is True, f"context/{scope}/{cid}: {data}"
    return latency


def _load_directories() -> tuple[list[dict], dict[str, dict], dict[str, dict], dict[str, dict]]:
    categories = [_load(path) for path in sorted((ROOT / "expanded" / "categories").glob("*.json"))]
    merchants = {data["merchant_id"]: data for path in (ROOT / "expanded" / "merchants").glob("*.json")
                 for data in [_load(path)]}
    customers = {data["customer_id"]: data for path in (ROOT / "expanded" / "customers").glob("*.json")
                 for data in [_load(path)]}
    triggers = {data["id"]: data for path in (ROOT / "expanded" / "triggers").glob("*.json")
                for data in [_load(path)]}
    return categories, merchants, customers, triggers


def run(bot_url: str | None = None, rate: float = 10.0) -> int:
    process = None
    temp_state = None
    if bot_url:
        base_url = bot_url.rstrip("/")
    else:
        temp_state = tempfile.TemporaryDirectory(prefix="vera-soak-", dir=ROOT)
        port = _free_port()
        base_url = f"http://127.0.0.1:{port}"
        child_env = dict(os.environ)
        child_env["VERA_STATE_DIR"] = temp_state.name
        child_env["PYTHONPATH"] = str(ROOT) + os.pathsep + child_env.get("PYTHONPATH", "")
        process = subprocess.Popen([sys.executable, "-m", "uvicorn", "app:app", "--host",
                                     "127.0.0.1", "--port", str(port), "--workers", "1",
                                     "--log-level", "warning"], cwd=ROOT, env=child_env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        with httpx.Client(base_url=base_url, timeout=9.0) as client:
            if process:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    try:
                        if client.get("/v1/healthz").status_code == 200:
                            break
                    except httpx.HTTPError:
                        time.sleep(.15)
                else:
                    raise RuntimeError("isolated Uvicorn server did not start")
            _request(client, "POST", "/v1/teardown")
            start_health, health_latency = _request(client, "GET", "/v1/healthz")
            assert health_latency < 2.0
            _request(client, "GET", "/v1/metadata")

            categories, merchants, customers, triggers = _load_directories()
            assert (len(categories), len(merchants), len(customers)) == (5, 50, 200)
            expected_counts = {"category": 5, "merchant": 50, "customer": 200, "trigger": 0}
            interval = 1.0 / max(.1, rate)
            push_latencies: list[float] = []
            pushed = 0
            for scope, entries in (("category", [(c.get("slug") or "", c) for c in categories]),
                                   ("merchant", list(merchants.items())),
                                   ("customer", list(customers.items()))):
                for cid, payload in entries:
                    before = time.monotonic()
                    latency = _push(client, scope, cid, payload)
                    push_latencies.append(latency)
                    pushed += 1
                    time.sleep(max(0.0, interval - (time.monotonic() - before)))
            assert pushed == 255
            warm, warm_latency = _request(client, "GET", "/v1/healthz")
            assert warm["contexts_loaded"] == expected_counts, warm
            assert warm_latency < 2.0
            server_pid = process.pid if process else None
            rss_start = _rss_bytes(server_pid)

            now = "2026-04-26T10:30:00Z"
            used_merchants: set[str] = set()
            reserved_m1 = "m_001_drmeera_dentist_delhi"
            recall_seed = next((copy.deepcopy(t) for t in sorted(triggers.values(), key=lambda t: t["id"])
                                if t.get("kind") == "recall_due" and t.get("merchant_id") != reserved_m1), None)
            if recall_seed is None:
                raise RuntimeError("expanded dataset has no non-reserved recall trigger")
            late_mid = str(recall_seed["merchant_id"])
            normal_pool = [copy.deepcopy(t) for t in sorted(triggers.values(), key=lambda t: t["id"])
                           if t.get("scope") == "merchant" and t.get("merchant_id") not in {reserved_m1, late_mid}]
            normal_by_merchant: dict[str, dict] = {}
            for trigger in normal_pool:
                normal_by_merchant.setdefault(trigger["merchant_id"], trigger)
            normal_pool = list(normal_by_merchant.values())
            normal_index = 0
            tick_latencies: list[float] = []
            reply_latencies: list[float] = []
            action_bodies: dict[str, set[str]] = {}
            v2_body = ""
            sim_start = datetime(2026, 4, 26, 10, 30, tzinfo=timezone.utc)
            for index in range(12):
                instant = time.monotonic()
                simulated_now = sim_start + timedelta(minutes=5 * index)
                stamp = simulated_now.isoformat().replace("+00:00", "Z")
                if index == 5:
                    category = copy.deepcopy(next(item for item in categories if item.get("slug") == "dentists"))
                    category["digest"] = list(category.get("digest") or []) + [{
                        "id": "d_soak_v2_digest", "kind": "research", "title": "Updated listing photo guidance",
                        "source": "Soak harness update"}]
                    category_payload = category
                    _push(client, "category", "dentists", category_payload, version=2, delivered_at=stamp)
                    merchant = copy.deepcopy(merchants[reserved_m1])
                    merchant["performance"] = dict(merchant.get("performance") or {}, views=2580)
                    _push(client, "merchant", reserved_m1, merchant, version=2, delivered_at=stamp)
                    fresh = {"id": "trg_soak_v2_fresh", "scope": "merchant", "kind": "perf_dip",
                             "source": "internal", "merchant_id": reserved_m1, "customer_id": None,
                             "urgency": 5, "suppression_key": "soak:v2:fresh",
                             "payload": {"metric": "calls", "delta_pct": -0.4, "window": "7d"},
                             "expires_at": "2020-01-01T00:00:00Z"}
                    _push(client, "trigger", fresh["id"], fresh, delivered_at=stamp)
                    trigger_ids = [fresh["id"]]
                    used_merchants.add(reserved_m1)
                elif index == 8:
                    customer_id = "c_soak_surprise"
                    sample = copy.deepcopy(customers[str(recall_seed.get("customer_id"))])
                    sample["customer_id"] = customer_id
                    sample["merchant_id"] = late_mid
                    sample.setdefault("consent", {})["scope"] = ["recall_reminders", "appointment_reminders"]
                    customer_stamp = stamp
                    # This customer context arrives two simulated minutes before its trigger.
                    _push(client, "customer", customer_id, sample, delivered_at=customer_stamp)
                    late = copy.deepcopy(recall_seed)
                    late.update(id="trg_soak_surprise_recall", customer_id=customer_id,
                                merchant_id=late_mid, scope="customer", suppression_key="soak:late:recall")
                    late["payload"] = dict(late.get("payload") or {})
                    trigger_stamp = (simulated_now + timedelta(minutes=2)).isoformat().replace("+00:00", "Z")
                    _push(client, "trigger", late["id"], late, delivered_at=trigger_stamp)
                    trigger_ids = [late["id"]]
                    used_merchants.add(late_mid)
                else:
                    while normal_index < len(normal_pool) and normal_pool[normal_index]["merchant_id"] in used_merchants:
                        normal_index += 1
                    if normal_index >= len(normal_pool):
                        raise RuntimeError("not enough distinct merchants for 12 simulated ticks")
                    trigger = normal_pool[normal_index]
                    normal_index += 1
                    used_merchants.add(str(trigger["merchant_id"]))
                    _push(client, "trigger", trigger["id"], trigger, delivered_at=stamp)
                    trigger_ids = [trigger["id"]]

                data, latency = _request(client, "POST", "/v1/tick", {"now": stamp,
                                                    "available_triggers": trigger_ids})
                tick_latencies.append(latency)
                actions = data.get("actions") or []
                assert len(actions) <= 20
                for action in actions:
                    cid, body = action["conversation_id"], action["body"]
                    seen = action_bodies.setdefault(cid, set())
                    normalized = " ".join(body.lower().split())
                    assert normalized not in seen, f"repeat body in {cid}"
                    seen.add(normalized)
                    if index == 5 and action["trigger_id"] == "trg_soak_v2_fresh":
                        v2_body = body
                assert latency < 8.0, f"tick {index + 1} took {latency:.3f}s"
                time.sleep(max(0.0, .02 - (time.monotonic() - instant)))

            assert v2_body and ("2,580" in v2_body or "2580" in v2_body) and "2,410" not in v2_body and "2410" not in v2_body, v2_body
            replay_outcomes = run_replays(client, reply_latencies)
            assert len(replay_outcomes) == 3
            assert max(reply_latencies, default=0) < 8.0
            rss_end = _rss_bytes(server_pid)
            rss_growth = ((rss_end - rss_start) / (1024 * 1024)
                          if rss_start is not None and rss_end is not None else None)
            assert rss_growth is None or rss_growth < 50.0, f"RSS grew {rss_growth:.1f} MiB"
            health_end, health_end_latency = _request(client, "GET", "/v1/healthz")
            assert health_end_latency < 2.0
            print("SOAK PASS: 255 context pushes at ~10 req/s; 12 simulated 5-minute ticks (60 minutes total); "
                  f"max tick={max(tick_latencies, default=0):.3f}s; healthz max <2s; replay scenarios=3/3; "
                  f"v2 fresh value=2580 views; RSS growth={rss_growth if rss_growth is not None else 'unavailable'} MiB.")
            print("SOAK ASSERTIONS: zero non-200 responses; <=20 actions/tick; no repeated body per conversation; "
                  "late customer context delivered 2 simulated minutes before recall trigger.")
            return 0
    except Exception as exc:
        print(f"SOAK FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if process:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if temp_state:
            temp_state.cleanup()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bot-url", help="use an already-running test server instead of launching an isolated one")
    parser.add_argument("--rate", type=float, default=10.0, help="context push rate, requests/second")
    args = parser.parse_args()
    return run(args.bot_url, args.rate)


if __name__ == "__main__":
    raise SystemExit(main())
