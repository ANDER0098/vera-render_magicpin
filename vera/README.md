# vera/ — The Grounding Layer

Pure, deterministic Python: **no LLM calls, no network I/O, no randomness, no
wall-clock reads.** Everything time-related arrives as an injected `now`
parameter. This layer is the bot's floor (it composes a sendable, fully
grounded, category-correct message for any `(category, merchant, trigger,
customer?)` tuple with every LLM provider down) and its immune system (it
decides what an LLM-composed message is *allowed* to say).

```
derive_facts → attach_conditionals → diagnose → build_whitelist
             → (LLM composes) → validate → repair | compose_fallback
```

## Modules

| Module | Responsibility |
|---|---|
| `vera/signals/derived.py` | Fact extraction (identity, performance-vs-peer gaps, cohort, category assets, trigger payload, customer, signals, review themes) + deterministic ranking. All human-facing phrases are pre-rewritten (scope slugs, month ranges, offer titles) so machine enums never enter copy. |
| `vera/signals/relevance.py` | Conditional-fact resolver: `select_offer` (offer precedence) + `attach_conditionals` (runway, personalisation points, quote salience, thin-merchant tenure). |
| `vera/signals/diagnose.py` | The diagnosis funnel: picks the ONE signal that drives the message and reconciles the numbers into a single-sentence causal story. `pick_primary` delegates to `diagnose(...).primary`. |
| `vera/voice.py` | Salutation / language / honorific / emoji / **recipient** resolution. `script` is always `"latin"` — romanised Hinglish only, never Devanagari. |
| `vera/text/normalize.py` | Normalisation, `(merchant_id, text)` fingerprinting (conversation-id-free — trap T2), conservative auto-reply detection, word n-grams. |
| `vera/grounding/whitelist.py` | `NUMERAL_RE` extraction + canonical variants + the context-sensitive effort/CTA constant rule. |
| `vera/grounding/validator.py` | Hard/soft checks (incl. `internal_vocab_leak`), deterministic idempotent `repair`, `grounding_map` (numeral → json_path provenance). |
| `vera/fallbacks/frames.py` | 26 per-trigger-kind frames + `GENERIC_FRAME`, `en` + `hi_en`. |
| `vera/fallbacks/compose.py` | `compose_fallback`: conditionals → diagnosis → offer selection → frame → **numeral budget (≥3 grounded numerals)** → validate/repair. |
| `harness/internal_vocab.py` | The internal-vocabulary defence and the safe-rewrite map. |
| `harness/case_corpus.py` | The anti-plagiarism oracle (8-grams of every case-study body). |
| `harness/sweep.py` | The golden corpus sweep runner (100 triggers × `now` values → `harness/out/fallback_sweep.jsonl`). |

## Internal-vocabulary defence (Defect A)

`harness/internal_vocab.py` scans `expanded/` and enumerates every machine-enum
value from the leak surface (category slug/voice/peer scope/offer audience and
type/digest kind and segment/month ranges; merchant subscription/signals/review
keys/aggregate keys; customer state/preferences/consent/services/conditions/age
bands; trigger kind/scope/source), expands each token to all surface forms
(raw, underscore→space, underscore→hyphen, title-cased, singular/plural for
bare words — generated only for words that are not themselves sanctioned), and
subtracts a *sanctioned* set: words that appear verbatim in merchant-authored
offer titles or `voice.vocab_allowed`, plan names, calendar words, service
history/condition values, structured humanizations of known payload keys, and
a small curated natural-word list.
`validate()` enforces it as the hard code **`internal_vocab_leak`**, together
with format-level checks (any `_ < > =` glyph, snake_case, 3+-part hyphen
slugs, analyst labels `Context:`/`Note:`/…, analyst words `payload`,
`benchmark`, `cohort`, `metric`, `delta`, `aggregate`, …, and id-shaped machine
values `d_2026W17_*`). `repair()` fixes it first via `rewrite_internal` (the
safe-rewrite map, which also handles mixed `-_` separators and month ranges),
then by dropping the leaking clause; `None` if anything still leaks. The
rewrite map renders known internal values as merchant language:
`metro_unisex_salons_2026` → "the metro unisex-salon average", `> ₹499` → "on
orders above Rs.499", `Nov-Feb` → "November to February", `lapsed_soft` →
"it's been a while", `high_risk_adults` → "your high-risk adult patients",
`summer_2026` → "this summer", slug labels → `dental`/`salon`/`gym`/…

### The generic payload-enum net (D1)

Vocabulary alone cannot survive Phase 3's injected payload fields, so
`validate()` additionally runs the hard code **`payload_enum_leak`**: for the
trigger being composed, every string value in `trigger.payload` (recursed) and
every `customer.preferences.*` value contributes its 2+-token surface
variants; any variant appearing in the body is a hard fail. Values under keys
with a STRUCTURED humanizer (`season`, `trends`, `fee`, `verification_path`,
`service_due`, … — plus verbatim-by-design keys like `available_slots`,
`competitor_name`, `match`, `venue`, `festival`, `city`, `affected_batches`)
are exempt in their blessed humanized form; every OTHER key — including
brand-new Phase-3 fields — fires on a raw render.
`payload_net_variants(trigger, customer)` is the shared implementation.
Citation strings ("— Multi-pharmacy aggregate Apr 2026", "— Google Trends
Apr 2026") are verbatim dataset data and are excluded from the leak scan.

## Causal-claim licensing (D2)

`Diagnosis` carries `rule_id` and `licensed_claims: tuple[str, ...]` — the
causal connectives that rule may use. `validate()` enforces the hard code
**`unsupported_causal_claim`**: any causal connective (`because`, `isliye`,
`ki wajah se`, `effect dikh raha`, `leading to`, `due to`, `ke karan`,
`kyunki`, `matlab`, …) in a body whose compose call did not license it is a
hard fail. `compose_fallback` passes
`licensed_claims=diagnosis.licensed_claims`. The `subscription_lapse_with_dip`
rule licenses NOTHING: its story leads with the quantified funnel read (both
benchmark sides) and phrases the lapse as a neutral aside — a causal claim may
never cite a metric that is above its benchmark as evidence of damage.
(Standalone `karan` is deliberately not checked — it is a common Indian first
name; the phrase `ke karan` is.)

## Fact-dump guard (D3)

Hard code **`fact_dump`**: more than 2 numerals from ONE source list
(list-valued Fact, e.g. a payload trend list) rendered in a single body — the
category_seasonal frame therefore renders only the largest riser and largest
faller plus one recommendation. Date tokens are anchors, not dump material;
molecule lists (`chronic_refill_due`) and slot choices (`multi_choice_slot`)
are exempt because the list IS the point of the message.

## hi vs hi_en (D4)

`Voice.lang` distinguishes `hi` (Hindi-dominant nouns) from `hi_en`
(Hinglish). A lint test requires ≥ 6 distinct Hindi function words for
`lang == "hi"` bodies and bans ordinary-English nouns with natural Hindi
equivalents (`saved address` → "registered address", `order details`);
genuinely technical terms (molecule names, dose, brand, click rate) stay
English. `preferences.delivery_address = "saved"` is a raw enum — the rewrite
map renders `registered address`.

## Offer conditions & digest quantification (D5)

Hard code **`offer_condition_dropped`**: an offer whose title carries a
threshold/eligibility clause (`> ₹499`, `save ₹6,000`, …) may have it reworded
but never dropped — referencing the offer without its grounded threshold
fails. Soft code **`digest_quantification`**: a digest item with both
`trial_n` and a percentage in its summary must render both (the research frame
leads with the summary finding, e.g. "Headline number: 38%"), and trial sizes
render with comma grouping ("2,100-patient trial").

## Source citations widened (D6)

`no_source_citation` now fires on research/compliance claims AND
market-trend/demand claims (`demand`, `YoY`, `searches`, …) whenever a sourced
digest item exists in context. Trend-bearing evidence clauses render only when
the digest covers them, and then carry their source inline
(`'sugar free dessert' searches 52% YoY — Google Trends Apr 2026.`).

## Diagnosis rules (Defect B)

`diagnose(facts, *, merchant, category, trigger, now) -> Diagnosis` — first
match wins. `Diagnosis` carries `rule_id`, `licensed_claims`, `story` (en),
`story_hi` (hi_en), `primary` (the ONE signal), `supporting` (≤2),
`recommended_action`, `action_hi`, `json_paths`.

| # | Rule | Story shape | Action |
|---|---|---|---|
| 1 | `milestone` | frames the gap (`milestone_value − value_now`), not the milestone | push for the last N |
| 2 | `review_theme` | neg theme with `occurrences_30d ≥ 3`: `common_quote` verbatim + count | one concrete fix |
| 3 | `seasonal` | expected seasonal dip: reframe, do not alarm | hold spend, retention play |
| 4 | `subscription_lapse_with_dip` | perf dip while plan expired/trial: the quantified funnel read leads; the lapse is a neutral aside (no causal connective licensed) | priced offer to work + one-click reactivation |
| 5 | `funnel_leakage` | views ≥ peer, ctr ≥ peer, calls < peer: "X views and Y% click rate above the Z average of P and Q — but only N calls came off it, against an average of M" | CTA / offer refresh / walk-in tag |
| 6 | `funnel_visibility` | views < peer, ctr ≥ peer | photos / posts / verification |
| 7 | `funnel_conversion` | views ≥ peer, ctr < peer | photos / offer / review push |
| 8 | `funnel_both` | both sides below peer | full profile rebuild |
| 9 | `digest` | digest-kind triggers: the resolved/top digest item is the anchor | pull source, draft follow-up |
| 10 | `asset_pivot` | nothing material: the top in-season beat, trend signal, or digest item | knowledge / curiosity send |

Every funnel story quantifies BOTH sides of the benchmark. The
lapse-aside rule precedes the plain funnel but licenses no causal language;
the funnel never hijacks asset-kind triggers (a research digest for a dipping
merchant is still a digest message).
`pick_primary(facts, trigger, *, merchant, category, now)` delegates to
`diagnose(...).primary`.

## Offer-selection precedence (Defect C)

`select_offer(offers, offer_catalog, *, customer, trigger, merchant, category)`:

1. active merchant offer matching a customer attribute — senior status
   (`senior_citizen` / age band ≥ 60 → title contains senior/65/silver), a
   training/health/previous focus (matching service word), or received
   services (matching service word);
2. otherwise the active offer with the LOWEST price point (delivery-flavoured
   first for refill intents);
3. otherwise an expired merchant offer (reactivation hook);
4. otherwise the catalog filtered by audience (senior → senior; repeat-ish
   trigger → `repeat_user`; else `new_user`/`all`) and by type —
   `service_at_price` and `free_service`/`free_trial` before
   `percentage_discount` and `bogo` (brief §11: service+price beats discount).

`attach_conditionals` additionally surfaces: subscription runway (≤21 days in
either direction, salience 1.0), the full customer_aggregate (keys read
dynamically), `preferred_stylist` / `wedding_date` / focus / household size /
office-nearby, review `common_quote` at salience 1.0, and tenure for
otherwise-thin merchants.

## Numeral budget (Defect D)

Every composed body carries ≥ 3 grounded Tier-1 numerals, filled in this
order: trigger payload facts → diagnosis primary + supporting → selected offer
price → peer benchmark pair → digest number/date → trend `delta_yoy` → cohort
count → tenure → deadline date. `weak_specificity` is **hard** at 0–1 grounded
numerals and soft at 2; the sweep fails the build above 5% (target 0) and the
13 placeholder-payload canonical pairs (`expanded/test_pairs.json`) must each
render with ≥ 3.

## Voice.recipient (Defect E)

`Voice.recipient ∈ {customer, guardian, parent, merchant}` plus
`Voice.third_person_subject`. `preferences.channel == "whatsapp_via_son"` →
recipient `guardian`: the salutation greets the unnamed reader ("Namaste"),
the customer is referred to in the third person ("Sharma ji ki monthly
medicines…"), and the CTA addresses the reader (case study 10 pattern).
`whatsapp_via_parent` or a `"(parent: X)"` name → recipient `parent`
(addressed by name) with the child as third-person subject. Glyphs `_ < > =`,
analyst labels and raw enum values never reach a body (see Defect A), and a
lint test enforces ≥ 3 distinct Hinglish function words for `hi_en` bodies and
zero Hindi for `en` bodies.

## Fact taxonomy (`Fact.kind`)

`identity | metric | benchmark | delta | offer | digest | seasonal | trend |
content | subscription | customer | cohort | review | trigger`

Provenance (`json_path`): `merchant.*`, `category.*`, `trigger.payload.<key>`,
`customer.*`; `derived:<formula>` only for computed ratios (`ctr_gap_pct`,
`call_rate = calls/views`, `len(trigger.payload.…)`,
`milestone_value - value_now`, `days_since(last_visit, now)`). Digest facts
carry the source string in `Fact.citation` so frames append `— <source>`.

## Validator codes

**Hard (block the send):** `ungrounded_numeral`, `taboo_vocab`, `url_present`,
`multiple_ctas`, `cta_not_last`, `empty_body`, `repeat_body`, `plagiarism`
(≥8-gram overlap with the case-study corpus), `jargon_leak`,
`internal_vocab_leak`, `payload_enum_leak` (D1), `unsupported_causal_claim`
(D2), `fact_dump` (D3), `offer_condition_dropped` (D5), `send_as_mismatch`,
`consent_violation`, `non_latin_script`, `self_reintroduction`,
`missing_required_field`, `weak_specificity` (0–1 grounded numerals).

**Soft (warn / repairable):** `body_too_long` (>480), `body_too_short` (<60),
`no_preamble_check`, `pct_instead_of_service_price`, `no_owner_name`,
`language_mismatch`, `weak_specificity` (exactly 2), `digest_quantification`
(D5), `no_source_citation` (D6: research/compliance/trend/demand claims).

`repair` only deletes/strips/reorders/rewrites — it never introduces a numeral,
is idempotent, and returns `None` when a hard violation is not repairable
(plagiarism, repeat, consent, routing) so the caller falls back to
`compose_fallback`.

## How the LLM composer calls this layer

```python
from datetime import datetime

now = datetime.fromisoformat(trigger_now.replace("Z", "+00:00"))
facts = derive_facts(category, merchant, trigger, customer, now)
facts = attach_conditionals(facts, category=category, merchant=merchant,
                            trigger=trigger, customer=customer, now=now)
voice = resolve_voice(category, merchant, customer, trigger, history)
whitelist = build_whitelist(facts, voice)

# ... LLM composes body/cta/send_as from facts + voice + diagnosis ...
body = localize_numbers(llm_body, voice.lang)

violations = validate(body, cta, send_as, facts=facts, voice=voice,
                      category=category, merchant=merchant, customer=customer,
                      prior_bodies=prior_bodies,              # bodies already sent
                      case_corpus_ngrams=load_case_ngrams(),  # anti-plagiarism oracle
                      is_first_touch=(turn_number == 1),
                      scope=trigger.get("scope"))             # pass scope when known!
if has_hard(violations):
    body = repair(body, violations, facts, voice)
    if body is None or has_hard(validate(body, ...)):
        message = compose_fallback(trigger["kind"], category=category,
                                   merchant=merchant, trigger=trigger,
                                   customer=customer, facts=facts,
                                   voice=voice, now=now,
                                   prior_bodies=prior_bodies)
```

**Pass `scope=` to `validate`** when the trigger's scope is known — otherwise
it is inferred from customer presence. **Pass `prior_bodies`** to both
`compose_fallback` and `validate` — a repeated body is a −2 penalty and compose
skips candidates that would repeat.

## Interface changes the server lane must know

- `Voice` gained `recipient: str = "merchant"` and
  `third_person_subject: str = ""` (defaults keep old call sites working).
- `pick_primary` gained optional keyword args `(facts, trigger, *, merchant,
  category, now)` and now delegates to the diagnosis.
- `compose_fallback` and `validate` accept optional keywords
  (`prior_bodies`, `scope`) beyond the pinned signatures.
- `validate` may import `harness.internal_vocab` (the vocabulary lives with
  the dataset tooling by design); keep `harness/` importable from the repo root.

## Handoff to the server lane

The grounding lane's root `conftest.py` skips `tests/test_contract.py` while
`bot.py`/`app.py` does not exist, and prints a loud warning on every pytest run:

```
SKIPPED tests/test_contract.py — no bot.py/app.py yet; this MUST run once the server lane lands
```

`pytest.ini` sets `addopts = -rs --strict-markers`, so skips always appear in
the summary line. **After landing the FastAPI module, run the final gate:**

```
python tools/check_contract_ran.py
```

It re-runs the contract suite and exits non-zero if anything is skipped or
failing. `pytest -q` fully green (including `tests/test_contract.py`) plus this
gate is the merged definition of done.

## Tests & sweep

`pytest -q` — 240 passed. `tests/test_corpus_sweep.py` renders **all 100
expanded triggers at two `now` values** with **zero hard violations, zero
`internal_vocab_leak`, zero duplicate bodies per merchant, and 0%
`weak_specificity`**, prints the violation histogram, asserts no machine glyph
in any body, lints `hi_en`/`en` language purity, and checks the 13
placeholder-payload canonical pairs render with ≥ 3 grounded numerals.
Output: `harness/out/fallback_sweep.jsonl` (100 lines) for human copy review.

## Server and composer integration

The HTTP service is exposed as both `app:app` and `bot:app`. Contexts are versioned in memory and journaled to `VERA_STATE_DIR/journal.jsonl`; conversation fingerprints, opt-outs, suppression keys, and send timestamps are journaled alongside. The default state directory is `vera_state/` under the project root. Run one Uvicorn worker because context state is process-local.

The server consumes the checked-in interfaces directly: `Diagnosis.rule_id` and `licensed_claims`; `validate(..., scope=..., trigger=..., prior_bodies=..., case_corpus_ngrams=..., licensed_claims=...)`; `compose_fallback(..., prior_bodies=...)`; and the `Voice.recipient`/`third_person_subject` fields. The referenced `prompts/glm53-grounding-layer.md` was not present in this package, so `vera/README.md` and the imported function signatures are the integration source of truth. The composer always builds the deterministic floor, supplies the diagnosis license to strict validation, retries fallback composition with source-backed fact subsets if the current strict validator rejects its first floor, and omits an action if it cannot prove a hard-violation-free result.

Two documentation conflicts are resolved in favor of the more specific, judge-facing contract:

- Context pushes at the same or lower version return HTTP 409. This follows `examples/api-call-examples.md` §1.5 and `challenge-testing-brief.md` §2.1, even though the sample skeleton described in the server task prompt returned 200.
- URLs hard-fail an action. `examples/api-call-examples.md` §F.4 says a URL is a hard fail, even though `challenge-brief.md` §9 says URLs may be allowed when useful; this implementation follows the validator and judge-example rule and does not ship URLs.

## Runtime configuration

Keyless mode is the default. The supported environment variables are:

| Variable | Purpose | Default |
|---|---|---|
| `VERA_STATE_DIR` | Context/conversation/cache state directory | `<project>/vera_state` |
| `VERA_TEAM_NAME`, `VERA_TEAM_MEMBERS`, `VERA_CONTACT_EMAIL` | Submission metadata; members are comma-separated | Magicpin Vera Challenge, empty member list, placeholder email |
| `VERA_VERSION`, `VERA_SUBMITTED_AT`, `VERA_APPROACH` | Submission metadata | `0.1.0`, `not submitted`, substantive server description |
| `VERA_LLM_PROVIDER` | `noop`, `openai_compat`, `anthropic`, `gemini`, `ollama`, `vllm`, or a comma-separated provider chain | `noop` |
| `VERA_LLM_BASE_URL`, `VERA_LLM_API_KEY`, `VERA_LLM_MODEL` | OpenAI-compatible endpoint/key/model | empty |
| `VERA_LLM_FALLBACK_PROVIDER`, `VERA_LLM_FALLBACK_MODEL` | Provider failover chain after primary failures | empty |
| `VERA_LLM_TIMEOUT`, `VERA_COMPOSE_BUDGET` | Per-call and overall composer bounds, seconds | `5.5`, `6.0` |
| `VERA_ACTION_LIMIT`, `VERA_COOLDOWN_SECONDS` | Per-tick top K (hard-capped at 20) and merchant cooldown | `5`, `86400` |
| `VERA_SCORE_URGENCY`, `VERA_SCORE_FACT_DENSITY`, `VERA_SCORE_DEADLINE`, `VERA_SCORE_PEER_GAP`, `VERA_SCORE_CATEGORY_FIT`, `VERA_SCORE_NOVELTY`, `VERA_SCORE_RECENCY_PENALTY` | Deterministic planner weights | `1.0`, `0.7`, `1.0`, `0.8`, `0.6`, `0.5`, `0.7` |
| `VERA_LOG_LEVEL` | Python log verbosity | `INFO` |

To enable one hosted OpenAI-compatible provider later: `export VERA_LLM_PROVIDER=openai_compat VERA_LLM_BASE_URL=... VERA_LLM_API_KEY=... VERA_LLM_MODEL=...`. Local `ollama` and `vllm` providers can run without an API key; defaults are `http://localhost:11434/v1` and `http://localhost:8000/v1` respectively, and Ollama defaults to `llama3.1`. No provider SDK is required. Anthropic and Gemini use `VERA_LLM_API_KEY`; provider keys are never written to logs or cache.

The render contract for editable placeholder packs is documented in `vera/prompts/README.md`. The keyless CI path needs no network access: `pytest -q`, `python tools/check_contract_ran.py`, and `python tools/replay_scenarios.py --bot-url http://localhost:8080` are the gates; `python deploy/soak.py` runs the longer API workload.
