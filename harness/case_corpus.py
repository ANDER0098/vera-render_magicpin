"""Extract the case-study corpus for the anti-plagiarism check (trap T10).

Parses the composed-message bodies out of ``examples/case-studies.md`` and
Appendix A/B of ``challenge-brief.md``, then exposes word 8-grams (default) of
every body. The validator hard-fails any candidate body sharing an 8-gram with
this corpus — the judge runs the same similarity check, so our own fallback
frames must pass it too, which forces original wording.
"""

from __future__ import annotations

import re
from pathlib import Path

from vera.text.normalize import ngrams

__all__ = ["load_case_bodies", "load_case_ngrams"]

_ROOT = Path(__file__).resolve().parents[1]
_SOURCES = (
    _ROOT / "examples" / "case-studies.md",
    _ROOT / "challenge-brief.md",
)

_FENCE_RE = re.compile(r"```[a-zA-Z]*\n(.*?)```", re.DOTALL)
_COMPOSED_HINT = re.compile(r"composed message", re.IGNORECASE)


def _extract_bodies(md_text: str) -> list[str]:
    bodies: list[str] = []
    lines = md_text.splitlines()
    for match in _FENCE_RE.finditer(md_text):
        body = match.group(1).strip()
        if not body:
            continue
        # confirm the fence sits under a "Composed message" heading within the
        # previous few lines (keeps json/yaml blocks out)
        fence_start_line = md_text[: match.start()].count("\n")
        context = "\n".join(lines[max(0, fence_start_line - 5): fence_start_line])
        if not _COMPOSED_HINT.search(context):
            continue
        if body.startswith("{") or body.startswith("["):
            continue
        bodies.append(body)
    return bodies


def load_case_bodies() -> list[str]:
    """Every composed message body found in the two source documents."""
    bodies: list[str] = []
    for path in _SOURCES:
        if not path.exists():
            continue
        bodies.extend(_extract_bodies(path.read_text(encoding="utf-8")))
    return bodies


def load_case_ngrams(n: int = 8) -> set[tuple[str, ...]]:
    """Word n-grams over every case-study body (the plagiarism oracle set)."""
    corpus: set[tuple[str, ...]] = set()
    for body in load_case_bodies():
        corpus |= ngrams(body, n=n)
    return corpus
