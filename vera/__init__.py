"""Vera grounding layer — pure, deterministic message composition machinery.

This package is the bot's floor and its immune system: it extracts verifiable
facts from pushed context, ranks them, resolves voice, composes deterministic
fallback messages, and validates that no ungrounded content escapes.

Invariants for every module in ``vera/``:
- pure Python, standard library only
- no LLM calls, no network I/O, no randomness, no wall-clock reads
- all time-dependence arrives as an injected ``now`` parameter
- deterministic: same inputs -> byte-identical outputs
"""
