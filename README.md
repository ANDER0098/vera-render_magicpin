# Vera — magicpin AI Challenge submission

**Team:** Magicpin Vera Challenge · **Model:** deterministic-fallback (LLM-optional)

## Approach

Vera is a **determinism-first** engagement bot. Every message is produced by a
pipeline of pure functions; an LLM (any provider) can only *enhance* a message,
never originate one, and nothing ships unless it passes the same grounding
validator the deterministic floor passes.

1. **Signal layer** (`vera/signals/derived.py`) — flattens the four context
   layers (category, merchant, customer, trigger) into typed `Fact` objects
   (14 kinds: performance, benchmarks, trends, offers, events, subscription,
   consent, …), each carrying the exact phrase it came from, its JSON path,
   and a salience score. Everything the bot can claim must exist as a Fact.
2. **Diagnosis** (`vera/signals/diagnose.py`) — maps fact patterns to one of
   nine diagnoses (milestone, review theme, seasonal, subscription lapse,
   funnel leakage/visibility/conversion, digest, asset pivot). The funnel
   diagnoses quantify **both** sides of a benchmark gap (merchant vs category
   average) so no causal claim is ever invented.
3. **Planning** (`vera/planner.py`) — scores candidate triggers (urgency,
   fact density, deadline, peer gap, novelty), enforces cooldowns, opt-outs,
   closed conversations and **customer consent scopes** (a customer scoped to
   promotional offers is never sent appointment/refill/recall outreach).
4. **Composition** (`vera/fallbacks/`) — 26 category-aware frames with a
   strict numeral budget (exactly ≥3 grounded numerals per body), voice
   control (merchant vs guardian/customer recipient, hi vs hi_en, Latin
   script only), and one CTA placed last.
5. **Grounding validator** (`vera/grounding/validator.py`) — 18 hard + 9 soft
   checks per body: every numeral must be attributed to a Fact (fabrication
   fails the send), internal vocabulary (payload enums, field names, rule ids)
   is rewritten into merchant language, no URLs, no jargon, no unsupported
   causal connectives, no plagiarism vs the case corpus, offers keep their
   conditions. Hard violations trigger idempotent repair, then suppression.
6. **LLM adapter** (`vera/llm/adapter.py`) — provider-agnostic, optional.
   With no key configured the deterministic path is the product; with a key,
   LLM drafts are grounded and repaired against the same validator before
   replacing the floor.

The HTTP surface (`app.py`, 5 endpoints) wraps this pipeline: versioned,
replay-safe context store (stale versions → 409), ≤8s tick with graceful
degradation to `{"actions": []}`, stateful multi-turn `/v1/reply` handling
auto-reply detection and confirm/cancel/slot flows, and state wipe on
`/v1/teardown`.

## Tradeoffs

- **Deterministic floor over LLM creativity.** Guarantees zero fabrication,
  zero jargon leakage and stable latency; costs some fluency versus a free
  LLM. LLM drafts must survive the identical validator, so a bad key or a
  slow provider can only reduce polish, never correctness.
- **Consent is a hard gate.** Four canonical pairs have customers whose
  consent scope (promotional offers) does not cover the trigger intent;
  these are deliberately **not** messaged (see `submission.jsonl` rationale
  fields) — respect-for-consent beats message count.
- **Latin-script Hinglish** for hi-language merchants: matches how the
  dataset's merchant voices read, and keeps the judge's script checks clean.

## What additional context would have helped most

- Real WhatsApp outcome data (open/reply/opt-out rates) to tune send timing
  and CTA shapes beyond synthetic benchmarks.
- Historical redemption per offer, and benchmark provenance (period, sample
  size, city tier) so trend claims could be dated instead of generic.
- Digest-item → performance-impact linkage; today digest copy stays
  descriptive rather than causal for lack of evidence.
- An explicit consent taxonomy for transactional (appointment/refill/recall)
  vs promotional intents — we inferred one from the dataset and enforced it
  strictly.

## Reproduce

```bash
pip install -r requirements-dev.txt
pytest -q                        # 325 tests + 11 frozen contract tests
python -m harness.runner --set E1 --no-llm   # CI gate over 30 canonical pairs
python tools/generate_submission.py          # regenerate submission.jsonl
uvicorn app:app --port 8080                  # serve the 5 endpoints
```
