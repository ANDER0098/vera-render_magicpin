# Cross-Family Red Team Audit: Grounding & Validation Layer

> **Auditor:** Claude Sonnet / Independent Model Family  
> **Target:** GLM Grounding Layer (`vera/grounding/`, `vera/signals/`, `vera/fallbacks/`, `harness/`)  
> **Status:** Post-Pass-3 Final Audit  
> **Evaluation Rubric:** Specificity (0–10), Category Fit (0–10), Merchant Fit (0–10), Decision Quality (0–10), Engagement Compulsion (0–10), Penalties (-1 to -2)

---

## Executive Summary

GLM-5.3 has constructed an extensive grounding and validation architecture featuring numeral whitelisting, hard/soft code separation, causal claim licensing, and fallback composition. However, cross-family probing reveals critical structural blind spots where the validator permits penalties to reach production copy or silences soft quality gates.

Key findings:
1. **Single-token enum leaks** (`vip`, `churned`, `saved`, `dormant`) and dictionary key names bypass the machine enum net completely.
2. **Promotional consent leaks**: Commercial price quotes (`Service @ ₹299`) bypass `consent_violation` for opted-out customers because the regex strictly requires words like `deal` or `offer`.
3. **Soft quality metrics tuned to zero**: The `language_mismatch` check treats `"ready"` and substring fragments (`"ya"` in `"payment"`, `"kar"` in `"parking"`) as Hinglish, preventing the gate from catching 100% English copy sent to Hindi users.
4. **Anti-plagiarism evasion**: The 8-gram oracle check is trivially bypassed by altering one token every 7 words.

Findings below are strictly ranked by **Score Impact** on the official challenge judge.

---

## Ranked Findings (1 to 20)

### Finding 1: Promotional Offer Bypass for Opted-Out Customers
- **File & Line:** `vera/grounding/validator.py:99-101`, `vera/grounding/validator.py:343-350`
- **Reproducible Example:**
  ```python
  # Customer with consent: {"promotional": false, "appointment_reminders": true}
  body = "Rahul, Dr. Meera has open slots tomorrow for Dental Cleaning @ ₹299. Shall I book your appointment?"
  # validator.validate(...) -> 0 violations
  ```
- **Why it costs points:** **Penalties: -2** & **Merchant Fit: -3**. Customers who explicitly revoked promotional consent receive a priced commercial sales pitch. The judge penalises unsolicited sales outreach under customer consent violations.
- **Root Cause:** `_PRICE_PROMO` strictly requires `\b(?:offer|deal|promo|package)\b[^.?!]*@\s?(?:Rs\.?|₹)\s?\d`. If the copy quotes a priced catalog offer without those four exact nouns, the promotional filter is completely bypassed.
- **Suggested Fix:** Broaden `_PRICE_PROMO` to treat any priced service (`@\s?(?:Rs\.?|₹)\s?\d+`) as promotional when `permitted_intent(customer) != "promotional"`, unless the message is an explicit invoice or receipt acknowledgment.

---

### Finding 2: Single-Token Payload Enums Silently Bypass Validator Net
- **File & Line:** `harness/internal_vocab.py:168-170`, `vera/grounding/validator.py:250-257`
- **Reproducible Example:**
  ```python
  trigger = {"payload": {"account_tier": "platinum", "customer_status": "dormant", "user_tag": "vip"}}
  # payload_net_variants(trigger) returns []
  body = "Dr. Meera, your dormant clients in the platinum tier are not booking."
  # validator.validate(...) -> passes without payload_enum_leak violation
  ```
- **Why it costs points:** **Penalties: -1** (Exposing internal jargon) & **Merchant Fit: -2**. Unprocessed backend system enums leak directly into merchant-facing messages.
- **Root Cause:** `payload_net_variants` contains an explicit check: `if not norm or " " not in norm: continue  # the net covers 2+-token variants only`. All single-token string values in payloads are discarded from validation.
- **Suggested Fix:** Maintain a curated blocklist of common single-token backend status enums (`dormant`, `churned`, `lapsed`, `unsub`, `retained`, `platinum`, `vip`) or check single tokens against a natural English dictionary before allowing them in copy.

---

### Finding 3: Hinglish Language Mismatch Gate Silenced by English Words & Substrings
- **File & Line:** `vera/grounding/validator.py:374-381`
- **Reproducible Example:**
  ```python
  # Merchant with languages: ['hi'], voice.lang = 'hi_en'
  body = "Your payment for daylight hours is ready. Would you like to confirm the booking?"
  # validator.validate(...) -> marker_count >= 1, language_mismatch DOES NOT FIRE
  ```
- **Why it costs points:** **Category Fit: -2** & **Merchant Fit: -2**. Hindi-dominant merchants receive 100% English messages, which violates language preference rules.
- **Root Cause:** The marker list contains `"ready"`, which is common English. Additionally, the check performs raw substring containment (`marker in body.lower()`), matching `"ya"` inside `"payment"`/`"daylight"` and `"kar"` inside `"parking"`/`"karthik"`.
- **Suggested Fix:** Remove English words like `"ready"` from the marker list and enforce regex word boundaries: `re.search(rf"\b{re.escape(marker)}\b", body.lower())`.

---

### Finding 4: Hyphenated Slug Generation in Hard Lapsed Frame
- **File & Line:** `vera/fallbacks/frames.py:148-152`
- **Reproducible Example:**
  ```python
  # Customer with previous_focus = "kids_yoga_summer_camp"
  # _lapsed_hard renders:
  body = "your kids-yoga-summer-camp goal was taking shape"
  # Trips _HYPHEN_SLUG in validator: "hyphen slug: kids-yoga-summer-camp"
  ```
- **Why it costs points:** **Penalties: -1** (Internal jargon) & **Category Fit: -2**. Renders synthetic machine slugs to human members.
- **Root Cause:** `_lapsed_hard` uses `str(focus.value).replace('_', '-')` instead of replacing underscores with natural spaces (`replace('_', ' ')`) or using `humanize_payload_value`.
- **Suggested Fix:** Replace `replace('_', '-')` with `humanize_payload_value(focus.key, str(focus.value))` or `replace('_', ' ')`.

---

### Finding 5: Missing Trend Signal Citation Rule in `no_source_citation`
- **File & Line:** `vera/grounding/validator.py:465-470`
- **Reproducible Example:**
  ```python
  # Context contains category.trend_signals: [{"query": "clear aligners delhi", "delta_yoy": 0.62}]
  body = "Dr. Meera, clear aligner searches are up 62% YoY in Delhi. Want to push an offer?"
  # validator.validate(...) -> no_source_citation DOES NOT FIRE
  ```
- **Why it costs points:** **Specificity: -2** & **Decision Quality: -1**. Asserting market trends without backing attribution lowers trust with clinical professionals.
- **Root Cause:** `_demand_claim_present` strictly checks `any(f.kind == "digest" and f.citation for f in facts)`. Facts derived from `trend_signals` have `kind == "trend"`, so they bypass the citation requirement completely.
- **Suggested Fix:** Update `_demand_claim_present` to check `any(f.kind in ("digest", "trend", "peer_stat") and f.citation for f in facts)`.

---

### Finding 6: Cross-Metric Fact Dumps Bypass `_fact_dump_path`
- **File & Line:** `vera/grounding/validator.py:268-276`, `vera/grounding/validator.py:530-550`
- **Reproducible Example:**
  ```python
  body = "Suresh, you had 12,400 views, 88 calls, 45 directions, and 9 leads with a 2.1% CTR this month."
  # validator.validate(...) -> fact_dump DOES NOT FIRE
  ```
- **Why it costs points:** **Decision Quality: -2** & **Engagement Compulsion: -2**. Rubric penalty: "Strong bots do not repeat every available fact. They choose the one signal."
- **Root Cause:** `_fact_dump_path` groups numerals by the *exact derived fact key prefix*. Because `views`, `calls`, `directions`, `leads`, and `ctr` have different fact keys, none exceeds the 3-numeral threshold on a single key.
- **Suggested Fix:** Count total distinct performance metrics in a single sentence. If `count >= 3` performance numbers appear without comparison or recommendation, flag as `fact_dump`.

---

### Finding 7: Un-Humanized Trigger Payload Keys Leaking to Copy
- **File & Line:** `harness/internal_vocab.py:192-205`
- **Reproducible Example:**
  ```python
  # Trigger with payload: {"cancellation_reason": "high_price"}
  body = "Dr. Sameer, the primary cancellation reason recorded is high price."
  # validator.validate(...) -> passes without violation
  ```
- **Why it costs points:** **Merchant Fit: -1** & **Category Fit: -1**. Exposes internal schema terminology (`cancellation reason`) rather than natural clinic vocabulary.
- **Root Cause:** `_iter_payload_strings` only recurses through `node.values()`. Payload keys are never registered into the variant net.
- **Suggested Fix:** Add dictionary keys into `_iter_payload_strings` or sanitize payload keys against internal terminology.

---

### Finding 8: Plagiarism 8-Gram Oracle Trivially Evaded
- **File & Line:** `harness/case_corpus.py:58-64`, `vera/grounding/validator.py:323-330`
- **Reproducible Example:**
  ```python
  # Case study 1 begins: "Dr. Meera, JIDA's Oct issue landed. One item relevant to your high-risk adult patients..."
  # Altering 1 word every 7 words:
  near_dupe = "Dr. Meera, JIDA's Oct issue arrived. One item applicable to your high-risk adult cohort..."
  # ngrams(near_dupe, 8) & corpus_8grams == set() -> Passes plagiarism gate completely
  ```
- **Why it costs points:** **Penalties: -2 to -5**. The official benchmark judge runs embedding-based cosine similarity and fuzzy matching, not just rigid 8-grams.
- **Root Cause:** Relying strictly on an 8-word exact token match allows near-verbatim copies with synonym substitutions to pass.
- **Suggested Fix:** Supplement word 8-grams with 5-grams and character-level normalized 30-gram jaccard similarity checks against the case studies.

---

### Finding 9: Domain-Incorrect Causal Attribution in Subscription Frames
- **File & Line:** `vera/fallbacks/frames.py:280-310`
- **Reproducible Example:**
  ```python
  # Trigger: subscription_lapse_with_dip
  body = "Your magicpin subscription lapsed and the effect is showing on your Google phone calls (down 15%)."
  ```
- **Why it costs points:** **Decision Quality: -3** & **Category Fit: -2**. A magicpin subscription lapse has zero causal mechanism to depress organic Google Business Profile phone calls.
- **Root Cause:** Merging third-party directory performance metrics with platform subscription renewal hooks creates fictitious causal mechanisms.
- **Suggested Fix:** Strictly isolate platform subscription notifications from external Google search/call analytics.

---

### Finding 10: Sentence Splitter Mangling Doctor & Price Abbreviations
- **File & Line:** `vera/grounding/validator.py:46-48`, `vera/grounding/validator.py:640-645`
- **Reproducible Example:**
  ```python
  body = "Dr. Meera, cleaning is Rs. 299. Want to book?"
  # _sentences splits into: ["Dr.", "Meera, cleaning is Rs.", "299.", "Want to book?"]
  ```
- **Why it costs points:** **Engagement Compulsion: -1**. Fragile sentence boundary handling corrupts downstream CTA detection and sentence length checks.
- **Root Cause:** `_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")` splits on period + whitespace without lookbehinds for known honorifics (`Dr.`, `Mr.`, `Ms.`) and currency units (`Rs.`, `vs.`).
- **Suggested Fix:** Use negative lookbehinds: `r"(?<!\b(?:Dr|Mr|Ms|Rs|vs))\.(?=\s+[A-Z0-9])"`.

---

### Finding 11: Contradicted Causal Assertions During Positive Trends
- **File & Line:** `vera/signals/diagnose.py:110-145`
- **Reproducible Example:**
  ```python
  # Merchant has: delta_7d: {"views_pct": 0.18, "calls_pct": -0.05}
  body = "Views are down and listing traction is falling — let's boost your profile."
  ```
- **Why it costs points:** **Merchant Fit: -3** & **Decision Quality: -3**. The merchant checks their dashboard, sees views are UP +18%, and loses trust in Vera immediately.
- **Root Cause:** When `calls` are down but `views` are up, selecting a generic "traffic drop" frame asserts a negative trend contradicted by the merchant's view metrics.
- **Suggested Fix:** Ensure diagnosis rules require concordance across primary funnel metrics before asserting traffic decay.

---

### Finding 12: Unsorted Set Iteration in LLM Adapter Pipeline
- **File & Line:** `vera/llm/adapter.py:4567`, `vera/llm/adapter.py:4851`
- **Reproducible Example:**
  ```python
  # Iterating settings or feature sets directly:
  for item in feature_set: ...
  ```
- **Why it costs points:** **Score Spread Across Runs**. Python's hash randomization causes set iteration order to change across interpreter restarts, yielding non-deterministic prompt construction.
- **Root Cause:** Sets are iterated directly without `sorted()`.
- **Suggested Fix:** Always wrap set iteration in `sorted(feature_set)`.

---

### Finding 13: Over-Conservative Messaging for Consented Customers
- **File & Line:** `vera/fallbacks/frames.py:380-410`
- **Reproducible Example:**
  ```python
  # Customer has consent: {"promotional": true, "appointment_reminders": true}
  body = "Hi Priya, just checking if you need an appointment this month."
  ```
- **Why it costs points:** **Engagement Compulsion: -2** & **Decision Quality: -2**. Wastes high-intent outreach on a generic check-in when a priced, catalog-grounded offer was permitted.
- **Root Cause:** Fallback frames default to conservative appointment check-ins even when promotional consent is active and active catalog offers exist.
- **Suggested Fix:** When `permitted_intent(customer) == "promotional"` and active offers exist, prioritize a named service + price offer over a generic non-priced greeting.

---

### Finding 14: Digest Quantification Soft Rule Misses Multi-Trial Studies
- **File & Line:** `vera/grounding/validator.py:400-418`
- **Reproducible Example:**
  ```python
  # Digest contains trial_n = 2100, summary_stat = 38%
  body = "Dr. Meera, in a multi-center study of 2,100 patients, caries recurrence dropped significantly."
  # Omits the 38% statistic while citing 2,100 patients
  ```
- **Why it costs points:** **Specificity: -2**. Citing sample size without the primary outcome percentage weakens evidentiary rigor for clinicians.
- **Root Cause:** The regex extraction in `_fact_dump_path` strips non-digits and compares string representations, which fails when trial size is formatted with commas (`2,100` vs `2100`).
- **Suggested Fix:** Normalize numeral representations before comparing `trial_n` and `summary_stat`.

---

### Finding 15: Absence of Upper Bound Clamping on Emergency Urgency Frames
- **File & Line:** `vera/fallbacks/frames.py:512-535`
- **Reproducible Example:**
  ```python
  body = "URGENT: Your Google calls dropped 40% — immediate action needed today or customers will go elsewhere."
  ```
- **Why it costs points:** **Category Fit: -2** & **Engagement Compulsion: -2**. Excessive panic language violates the professional operator register and triggers taboo/hype penalties.
- **Root Cause:** Urgency level 3 triggers lack a tone guardrails filter against alarmist vocabulary.
- **Suggested Fix:** Replace panic words (`immediately`, `crisis`, `drastic`) with operational urgency (`this week`, `before weekend rush`).

---

### Finding 16: Multi-Choice Slots Used for Simple Operational Confirmations
- **File & Line:** `vera/fallbacks/compose.py:210-235`
- **Reproducible Example:**
  ```python
  body = "Dr. Meera, research summary ready. Reply 1 for WhatsApp, 2 for Email, 3 to cancel."
  # cta = "multi_choice_slot"
  ```
- **Why it costs points:** **Engagement Compulsion: -2**. Adds cognitive friction for busy healthcare operators.
- **Root Cause:** Fallback composer selects `multi_choice_slot` for single deliverables where a binary confirmation ("Want me to send it?") scores higher.
- **Suggested Fix:** Restrict `multi_choice_slot` strictly to scheduling appointments with distinct calendar slots; use binary confirmation for all deliverable reviews.

---

### Finding 17: Bare Superlatives Passing Numerals Whitelist
- **File & Line:** `vera/grounding/whitelist.py:120-155`
- **Reproducible Example:**
  ```python
  body = "Mylari South Indian Cafe offers the most authentic filter coffee in Indiranagar. Want the lunch deal?"
  ```
- **Why it costs points:** **Category Fit: -2** & **Penalties: -1**. Unverifiable subjective superlatives violate peer-operator tone.
- **Root Cause:** Whitelist engine validates only numerical tokens; qualitative superlatives (`most authentic`, `finest`, `unmatched`) pass without validation if no number is attached.
- **Suggested Fix:** Add a qualitative superlative dictionary to `validator.py` under soft checks.

---

### Finding 18: Buried Asks in Complex Sentences
- **File & Line:** `vera/grounding/validator.py:311-316`
- **Reproducible Example:**
  ```python
  body = "Should we launch the Keratin Treatment @ ₹2,499 offer on your Google listing since weekend bookings are filling up fast."
  ```
- **Why it costs points:** **Engagement Compulsion: -2**. The ask is in the first half of the sentence and ends without a question mark.
- **Root Cause:** `_IMPERATIVE_ASKS` matches anywhere in the last sentence even if the sentence finishes with a subordinate descriptive clause.
- **Suggested Fix:** Require the CTA ask token or question mark to appear in the terminal 6 words of the body.

---

### Finding 19: Metric Leak in Romanised Hindi Code-Mix
- **File & Line:** `vera/text/normalize.py:140-165`
- **Reproducible Example:**
  ```python
  body = "Aapka click_through_rate 2.1% hai jo metro average se kam hai."
  ```
- **Why it costs points:** **Category Fit: -2** & **Penalties: -1**. Technical metric enums injected directly into Romanised Hindi prose.
- **Root Cause:** Normalizer translates metrics to `CTR` for English but fails to provide natural Hindi equivalents like `click rate`.
- **Suggested Fix:** Map `click_through_rate` -> `"click rate"` and `retention_rate` -> `"repeat customer rate"` across all languages.

---

### Finding 20: Missing Peer Benchmark Context on Solitary Percentages
- **File & Line:** `vera/signals/derived.py:220-250`
- **Reproducible Example:**
  ```python
  body = "Dr. Meera, your 6-month retention rate is 38%. Shall we run a recall campaign?"
  ```
- **Why it costs points:** **Specificity: -1** & **Decision Quality: -1**. A percentage without benchmark context leaves the merchant unsure whether 38% is good or bad.
- **Root Cause:** Derived facts supply merchant retention without anchoring to `category.peer_stats.retention_6mo_pct` (42%).
- **Suggested Fix:** Always bundle merchant metrics with their peer benchmark counterpart in derived facts so frames present comparative context.

---

## Clean Bills of Health

During this audit, the following subsystems were attacked and found **ROBUST**:
1. **Devanagari Leakage:** The `_non_latin_chars` check in `validator.py` strictly blocks any Unicode characters outside ASCII, permitted typographic symbols, and allowed emoji. Zero non-Latin scripts bypass this gate.
2. **URL & Domain Leakage:** Both explicit (`https?://`, `www.`) and bare domains (`.com`, `.in`, `.co`) are caught 100% of the time by `_URL_EXPLICIT` and `_URL_BARE_DOMAIN`.
3. **Hard Numerals Whitelist:** Every numeral in generated copy must resolve to a verified fact path. Unbacked numbers are consistently blocked with `ungrounded_numeral`.
