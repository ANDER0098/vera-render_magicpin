# Prompt pack render contract

These files are clearly marked placeholders owned by the copy lane. They are hot-reloaded by `PromptRegistry`.

Each pack may contain `## SYSTEM` followed by system instructions, and `## KIND: <trigger.kind>` blocks. A `## KIND: generic` block is used for unknown kinds. User-template placeholders are exactly `Fact.key` values plus `{salutation}`, `{owner}`, `{business}`, `{locality}`, `{city}`, `{lang}`, `{emoji}`, `{cta}`, `{citation}`, `{now_iso}`, and `{kind}`. Values are substituted from the grounded fact and voice inputs; unknown placeholders remain unchanged and the resulting draft must still pass the validator.

Packs request one JSON object with `body`, `cta`, and `send_as`. A pack is never trusted: the deterministic fallback is built first, every model result is validated, and invalid or malformed output falls back.
