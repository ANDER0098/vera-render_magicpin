# Agent orchestration briefs — GLM-5.3 (grounding) and Antigravity (harness/voice)

This directory holds the task briefs that were dispatched to the parallel
coding agents, synced from the coordinator's working folder so the repo is
self-contained. They are historical coordination documents — read them to
understand WHY each lane exists, what its file ownership is, and what the
merge order is. The code itself lives in the lanes listed below.

| Brief | Lane | What it produced |
|---|---|---|
| `glm53-grounding-layer.md` | GLM-5.3 pass 1 | `vera/signals/`, `vera/grounding/`, `vera/fallbacks/`, `vera/voice.py`, `vera/text/`, `harness/case_corpus.py`, `harness/sweep.py`, `tools/check_contract_ran.py`, `conftest.py`, `pytest.ini`, `vera/README.md` |
| `glm53-pass2-rework.md` | GLM-5.3 pass 2 | internal-vocab defence, `vera/signals/diagnose.py`, `vera/signals/relevance.py`, offer selection, recipient resolution, numeral budget |
| `glm53-pass3-rework01.md` | GLM-5.3 pass 3 | `payload_enum_leak` net, causal-claim licensing, `fact_dump`, `offer_condition_dropped`, hi/hi_en split, widened citations |
| `antigravity-5-agents.md` | Antigravity ×5 | merge order, red-team brief, model-selection notes, `tools/verify_codex.sh` ritual |
| `antigravity-harness-and-voice.md` | Antigravity Phase A/B | `harness/local_judge.py`, `harness/runner.py`, `harness/eval_sets/`, `docs/voice/`, `tools/judge_doctor.py` |

## Post-merge verification ritual (from `antigravity-5-agents.md`)

```bash
pytest -q
python tools/check_contract_ran.py
bash tools/verify_codex.sh          # POSIX; on Windows: tools/verify_codex.ps1
```

`tools/verify_codex.sh` / `.ps1` are the full five-agent gate: frozen contract
suite, whole pytest run, judge-doctor live probe (skipped as PENDING when no
judge API key is configured — exit 2), the keyless `--no-llm` runner CI gate
over E1, and the artifact/constraint audit (voice playbooks, REDTEAM.md,
eval sets E1–E4, local judge files).
