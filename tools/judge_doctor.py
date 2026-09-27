#!/usr/bin/env python3
"""
judge_doctor.py — find out whether your judge LLM actually works, and which model to pin.

Gemini free tier returns 503 (model overloaded) intermittently, and the shipped
judge_simulator.py has NO retry logic — one 503 and the whole run dies or falls back.

This script answers four questions in about 30 seconds:
  1. Is the key valid?
  2. Which models can this key actually call?
  3. Which of them respond to a judge-shaped prompt right now, and how fast?
  4. Does the response parse as the scorecard JSON we need?

Usage:
    export GEMINI_API_KEY=AIza...
    python3 tools/judge_doctor.py
    python3 tools/judge_doctor.py --provider gemini --repeat 5
    python3 tools/judge_doctor.py --provider groq   --key-env GROQ_API_KEY
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request

# The shipped judge's system prompt, verbatim (trimmed here for the probe).
PROBE_SYSTEM = """You are a STRICT judge for the magicpin AI Challenge. You score merchant engagement messages.
Score each dimension 0-10 (5 is average, 7+ is good, 9+ is excellent).
PENALTIES: Fabricating data not in context: -2. Exposing internal jargon to merchant: -1.
RESPOND ONLY WITH THIS EXACT JSON FORMAT:
{"specificity": <0-10>, "specificity_reason": "<1-2 sentences>",
 "category_fit": <0-10>, "category_fit_reason": "<why>",
 "merchant_fit": <0-10>, "merchant_fit_reason": "<why>",
 "decision_quality": <0-10>, "decision_quality_reason": "<why>",
 "engagement_compulsion": <0-10>, "engagement_reason": "<why>",
 "hint": "<one sentence>"}"""

PROBE_USER = """SCORE THIS MESSAGE:

=== CONTEXT PROVIDED TO BOT ===
Category: salons
Voice: warm_practical
Merchant: The Beauty Bar
Owner: Sushma
Locality: Baner
Languages: ['en', 'hi']
Performance: views=2547, calls=22, ctr=0.058
Signals: []
Active Offers: []
Trigger Kind: perf_dip
Trigger Payload: {"placeholder": true, "metric_or_topic": "perf_dip"}
Trigger Urgency: 3

=== BOT'S MESSAGE ===
Body (240 chars): "Sushma, aapki listing kaam kar rahi hai - 2,547 views aur 5.8% click rate
dono metro unisex-salon average (2,400 aur 4.0%) se upar hain. Lekin calls sirf 22 aaye, average
28 hota hai. 'Haircut @ Rs.99' push kar dete hain? Bhej dun?"
CTA: binary_yes_no
Send As: vera

Score each dimension 0-10 with clear reasoning. Be STRICT."""

GEMINI_LIST = "https://generativelanguage.googleapis.com/v1beta/models?key={key}"
GEMINI_CALL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
GROQ_LIST = "https://api.groq.com/openai/v1/models"
GROQ_CALL = "https://api.groq.com/openai/v1/chat/completions"
OPENAI_CALL = "https://api.openai.com/v1/chat/completions"


def _post(url: str, body: dict, headers: dict, timeout: int = 45):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", **headers}
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode()), None, time.time() - t0
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:300] if e.fp else ""
        return None, f"HTTP {e.code}: {detail}", time.time() - t0
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}", time.time() - t0


def _get(url: str, headers: dict, timeout: int = 30):
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode()), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}: {e.read().decode()[:200] if e.fp else ''}"
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


def list_gemini(key: str) -> list[str]:
    d, err = _get(GEMINI_LIST.format(key=key), {})
    if err:
        print(f"  ! cannot list models: {err}")
        return []
    out = []
    for m in d.get("models", []):
        if "generateContent" in m.get("supportedGenerationMethods", []):
            out.append(m["name"].replace("models/", ""))
    return out


def list_groq(key: str) -> list[str]:
    d, err = _get(GROQ_LIST, {"Authorization": f"Bearer {key}"})
    if err:
        print(f"  ! cannot list models: {err}")
        return []
    return sorted(m["id"] for m in d.get("data", []))


def call_gemini(key: str, model: str):
    return _post(
        GEMINI_CALL.format(model=model, key=key),
        {
            "contents": [{"parts": [{"text": f"{PROBE_SYSTEM}\n\n{PROBE_USER}"}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 1500},
        },
        {},
    )


def call_groq(key: str, model: str):
    return _post(
        GROQ_CALL,
        {
            "model": model,
            "temperature": 0.2,
            "max_tokens": 1500,
            "messages": [
                {"role": "system", "content": PROBE_SYSTEM},
                {"role": "user", "content": PROBE_USER},
            ],
        },
        {"Authorization": f"Bearer {key}"},
    )


def call_openai_compat(key: str, model: str, base: str = OPENAI_CALL):
    return _post(
        base,
        {
            "model": model,
            "temperature": 0.2,
            "max_tokens": 1500,
            "messages": [
                {"role": "system", "content": PROBE_SYSTEM},
                {"role": "user", "content": PROBE_USER},
            ],
        },
        {"Authorization": f"Bearer {key}"},
    )


def extract_text(provider: str, data: dict) -> str:
    if provider == "gemini":
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception:  # noqa: BLE001
            return ""
    try:
        return data["choices"][0]["message"]["content"]
    except Exception:  # noqa: BLE001
        return ""


def parses_as_scorecard(text: str) -> tuple[bool, str]:
    import re

    m = re.search(r"\{[\s\S]*\}", text or "")
    if not m:
        return False, "no JSON object in response"
    try:
        d = json.loads(m.group())
    except Exception as e:  # noqa: BLE001
        return False, f"unparseable JSON: {e}"
    need = {"specificity", "category_fit", "merchant_fit", "decision_quality", "engagement_compulsion"}
    missing = need - set(d)
    if missing:
        return False, f"missing dimensions: {sorted(missing)}"
    scores = []
    for key in need:
        value = d[key]
        if isinstance(value, bool):
            return False, f"invalid score for {key}: expected a number from 0 to 10"
        if isinstance(value, int):
            score = value
        elif isinstance(value, float) and value.is_integer():
            score = int(value)
        elif isinstance(value, str) and value.strip().isdigit():
            score = int(value.strip())
        else:
            return False, f"invalid score for {key}: expected a number from 0 to 10"
        if not 0 <= score <= 10:
            return False, f"invalid score for {key}: expected a number from 0 to 10"
        scores.append(score)
    total = sum(scores)
    return True, f"parsed OK, total {total}/50"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="gemini", choices=["gemini", "groq", "openai_compat"])
    ap.add_argument("--key-env", default=None)
    ap.add_argument("--model", default=None, help="test one specific model")
    ap.add_argument("--repeat", type=int, default=3, help="calls per model (exposes flakiness)")
    ap.add_argument("--base-url", default=None, help="for openai_compat")
    ap.add_argument("--max-models", type=int, default=8)
    a = ap.parse_args()

    env = a.key_env or {"gemini": "GEMINI_API_KEY", "groq": "GROQ_API_KEY",
                        "openai_compat": "OPENAI_API_KEY"}[a.provider]
    key = os.environ.get(env, "")
    if not key:
        print(f"FAIL: ${env} is not set.  export {env}=...")
        return 2

    print(f"provider = {a.provider}   key = set (value hidden)   repeat = {a.repeat}\n")

    if a.model:
        models = [a.model]
    elif a.provider == "gemini":
        models = list_gemini(key)
        # prefer flash/pro text models, skip embedding/image/tts noise
        models = [m for m in models if not any(x in m for x in
                  ("embedding", "imagen", "tts", "veo", "nano-banana", "aqa"))]
        models = models[: a.max_models]
    elif a.provider == "groq":
        models = list_groq(key)[: a.max_models]
    else:
        print("openai_compat needs --model (no list endpoint assumed)")
        return 2

    if not models:
        print("FAIL: no callable models found for this key.")
        return 2

    print(f"Testing {len(models)} model(s):\n")
    print(f"{'model':40s} {'ok/total':>9s} {'p50 s':>7s} {'errors':>7s}  scorecard")
    print("-" * 96)

    results = []
    for m in models:
        oks, errs, lats, last = 0, [], [], ""
        for _ in range(a.repeat):
            if a.provider == "gemini":
                d, err, lat = call_gemini(key, m)
            elif a.provider == "groq":
                d, err, lat = call_groq(key, m)
            else:
                d, err, lat = call_openai_compat(key, m, a.base_url or OPENAI_CALL)
            lats.append(lat)
            if err:
                errs.append(err.split("\n")[0][:60])
                continue
            ok, msg = parses_as_scorecard(extract_text(a.provider, d))
            last = msg
            oks += 1 if ok else 0
            if not ok:
                errs.append(msg[:60])
        p50 = statistics.median(lats) if lats else 0.0
        uniq = sorted(set(errs))
        print(f"{m[:40]:40s} {oks}/{a.repeat:<7d} {p50:7.1f} {len(errs):7d}  {last or (uniq[0] if uniq else '')}")
        if uniq and oks < a.repeat:
            for u in uniq[:2]:
                print(f"{'':40s}   ! {u}")
        results.append((m, oks, p50, uniq))

    print()
    good = [r for r in results if r[1] == a.repeat]
    if not good:
        flaky = [r for r in results if r[1] > 0]
        if flaky:
            best = max(flaky, key=lambda r: (r[1], -r[2]))
            print(f"NO model is fully reliable right now. Best partial: {best[0]} "
                  f"({best[1]}/{a.repeat}, p50 {best[2]:.1f}s)")
            print("=> This is transient capacity, not your config. Add retry+backoff, or use a")
            print("   second provider. Your own harness/runner.py already throttles and caches.")
        else:
            print("FAIL: no model returned a usable scorecard. Check the key and model names.")
        return 1

    best = min(good, key=lambda r: r[2])
    print(f"RECOMMENDED: LLM_MODEL = \"{best[0]}\"   ({best[1]}/{a.repeat} ok, p50 {best[2]:.1f}s)")
    if len(good) > 1:
        others = ", ".join(r[0] for r in good if r is not best)
        print(f"Also reliable: {others}")
        print("Use the others as your 2nd and 3rd judges for the min-across-judges panel.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
